"""최종 사전 전체 교체 CLI — 용어집(성분 v3.1 · 인증 v5 · 문구 v5) · 규제사전 v12 · 현지부적합 v9 · 카테고리 v3.

    python -m app.dictionary_full_reload plan    --run-id R --code-sha S <파일 6개>
    python -m app.dictionary_full_reload apply   --run-id R --code-sha S --spec-sha H <파일 6개>
    python -m app.dictionary_full_reload status  --run-id R --spec-sha H
    python -m app.dictionary_full_reload restore --run-id R --spec-sha H

    파일: --certification --ingredient --phrase --regulatory --local --category

방식(2026-11-09 사용자 지시 — 옛 사전과의 공존이 아니다): 사전 다섯 표의 행을 모두 지우고 최종본만 넣는다.
  glossary · expression_dictionary · expression_dictionary_evidence · category_master · category_mapping.
  작업 · 섹션 · 블록 · 판정 · 시도 · 인계(작업별 사전 사본) · 감사 기록은 쓰지 않는다.
- 삭제는 DELETE 로만 한다. 시퀀스를 되돌리지 않는다(TRUNCATE … RESTART IDENTITY 금지) → 새 PK 는 옛 PK 와 겹치지 않는다.
  판정의 dictionary_id 처럼 옛 PK 를 가리키는 기록이 다른 항목을 가리키지 않게 커밋 전에 확인한다.
- 작업이 카테고리를 가리키면(job.display_category_id, FK ON DELETE SET NULL) 중단한다 — 삭제가 작업 값을 바꾸므로.
- 입력 파일은 아래 FINAL_FILES 의 SHA-256 과 같아야 한다. 다른 파일에는 쓰지 않는다.

순서
- plan: 파일 검증 · 현재 다섯 표의 전체 행 · 작업 참조 · PK 최대값으로 명세를 만든다. 오브젝트 저장소에 put_if_absent 로 저장하고
  되읽어 대조한다(같은 키에 다른 내용이면 덮어쓰지 않음). DB 쓰기 없음. 명세의 적용 전 전체 행은 restore 의 원본이다.
- apply: 명세를 되읽어 해시 · 코드 · 대상 · 파일을 대조하고, 표를 잠근 뒤 현재 행 · 작업 참조 · PK 를 다시 만들어 명세와 같을 때만
  쓴다(다르면 쓰기 없이 중단). 삭제 · 삽입 · 감사 기록이 한 트랜잭션이다. 커밋 전에 전체 저장값(파일과 열 단위) · 원본 ID 집합 ·
  최종본 밖 행 0 · 새 PK 최소값 > 옛 PK 최대값 · 판정이 가리키던 옛 사전 PK 와 겹침 없음을 확인한다. 하나라도 다르면 전체 롤백.
- restore: 적용 뒤 값과 DB 가 같을 때만, 명세의 적용 전 행을 **원래 PK 그대로** 되돌린다(판정이 가리키던 사전 PK 복구).
  시퀀스는 되돌리지 않는다(되돌린 뒤 새로 생기는 행이 적용 때 쓴 PK 와도 겹치지 않음).

종료 코드: 0 완료 · 1 중단(이번 실행으로 DB 변경 없음) · 2 환경 오류 · 3 완료 불명 · 4 DB 완료 · 결과 파일 저장 미완료

이 도구가 하지 않는 것(3차 · 별도): 분석 묶음의 현지 v9 판정값 · 채널 행 사용, 판정 API enum, 판매 사이트 마스터, 공급 term_kind 전달.
현재 코드에서는 v9 가 저장돼도 분석 묶음이 현지 행을 skipped 로 두고 채널 행은 조회하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from app import category_load
from app import dictionary_ingest as di
from app import glossary_ingest as gi
from app.glossary_replace import (EXIT_ENV, EXIT_OK, EXIT_RESULT_PENDING, EXIT_STOP, EXIT_UNKNOWN, RUN_ID, SHA256, Stop,
                                  Unknown, _canon, _check_target, _file_sha, _norm, _rollback, _sha, _store_json, _target)
from app.storage import get_store

SPEC_SCHEMA = "dictionary-full-reload-spec@1"
RESULT_SCHEMA = "dictionary-full-reload-result@1"
ACT_APPLIED = "dictionary_full_reload_applied"
ACT_RESTORED = "dictionary_full_reload_restored"

# 사용자가 확인한 최종본(2026-11-09). 다른 파일로는 실행하지 않는다.
FINAL_FILES = {
    "certification": "1d4c68125719739200429f4fc8e731b61db7c5438b0d0444db40dbdf6eb4a53c",
    "ingredient": "bd7aae6adde3075d8072bd4a65affc893b4ae7c86320f7d0d2c98b6fa46fb46f",
    "phrase": "fd0f22198a498c98a2f92a7349c7dcd184b5570472eec0cf90f08a5742499f48",
    "regulatory": "472ee17586ba88007eb5be68c506aa1ed8d4bee94c407f6755c303da034c4cee",
    "local": "b4eb3b282aa559be00e32d23d13a059fdf53b0792a4b78e08f985987c25b7689",
    "category": "0bf2303dc0155c486bc34bb5d7237e4148ad86fa70ab4d9caeb4473301be7fb6",
}
KINDS = tuple(FINAL_FILES)
GLOSSARY_KINDS = ("certification", "ingredient", "phrase")
# v9 적재 대상(국가 8 + 채널 A 4). 로더의 「상태」 규칙과 이중 대조 — 채널 B 3행이 섞이면 중단
V9_LOAD_IDS = tuple(f"LC-0{i}" for i in range(1, 9)) + ("CH-01", "CH-02", "CH-04", "CH-06")
EXPECTED = {"glossary": 21144, "expression_dictionary": 38, "expression_dictionary_evidence": 30,
            "category_master": 200, "category_mapping": 148}
TABLES = ("glossary", "expression_dictionary", "expression_dictionary_evidence", "category_master", "category_mapping")
DELETE_ORDER = ("category_mapping", "category_master", "expression_dictionary_evidence", "expression_dictionary", "glossary")


# ── 입력 ─────────────────────────────────────────────────────────────────
class Inputs:
    def __init__(self, paths: dict[str, str], max_errors: int = 50):
        got = {k: _file_sha(p) for k, p in paths.items()}
        wrong = sorted(k for k in KINDS if got.get(k) != FINAL_FILES[k])
        if wrong:
            raise Stop(f"최종본 파일 해시와 다름 {wrong}")
        self.paths, self.shas = paths, got
        self.gvr = gi.validate_files({k: paths[k] for k in GLOSSARY_KINDS})
        self.dvr = di.validate_files(paths["regulatory"], paths["local"])
        self.cf = category_load.parse(paths["category"])
        issues = self.gvr.issues + self.dvr.issues + self.cf.issues
        if issues:
            gi.print_issues(issues, max_errors)
            raise Stop(f"파일 검증 오류 {len(issues)}건")
        local = [r["external_id"] for f in self.dvr.files if f.kind == "local" for r in f.records]
        if tuple(sorted(local)) != tuple(sorted(V9_LOAD_IDS)):
            raise Stop(f"현지 v9 적재 행이 고정 목록과 다름 {sorted(local)}")
        n_ev = sum(len(f.evidence) for f in self.dvr.files)
        counts = {"glossary": len(self.gvr.records), "expression_dictionary": len(self.dvr.records),
                  "expression_dictionary_evidence": n_ev, "category_master": len(self.cf.nodes),
                  "category_mapping": len(self.cf.leaves)}
        if counts != EXPECTED:
            raise Stop(f"파일 건수가 기대값과 다름 {counts}")

    def files(self) -> dict[str, Any]:
        return {k: {"name": os.path.basename(self.paths[k]), "sha256": self.shas[k]} for k in KINDS}


# ── 현재 상태 ─────────────────────────────────────────────────────────────
def _columns(conn: Connection, table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = :t ORDER BY ordinal_position"), {"t": table}).all()]


def _rows(conn: Connection, table: str) -> list[dict[str, Any]]:
    return [{k: _norm(v) for k, v in r.items()} for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()]


def _refs(conn: Connection) -> dict[str, Any]:
    """사전을 가리키는 작업 쪽 기록(쓰지 않는다). 잠금 뒤 다시 만들어 명세와 대조한다."""
    jobs = [r[0] for r in conn.execute(text("SELECT id FROM job WHERE display_category_id IS NOT NULL ORDER BY id")).all()]
    vids = [r[0] for r in conn.execute(text(
        "SELECT DISTINCT dictionary_id FROM section_verdict WHERE dictionary_id IS NOT NULL ORDER BY 1")).all()]
    nv = conn.execute(text("SELECT count(*) FROM section_verdict WHERE dictionary_id IS NOT NULL")).scalar()
    flags = conn.execute(text(
        "SELECT count(*) FILTER (WHERE f ? 'glossary_id'), count(*) FILTER (WHERE f ? 'dictionary_id') FROM text_block t "
        "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.compliance_flags) = 'array' "
        "THEN t.compliance_flags ELSE '[]'::jsonb END) f")).one()
    return {"job_display_category": jobs, "verdict_dictionary_ids": vids, "verdict_rows": nv,
            "flag_glossary": flags[0], "flag_dictionary": flags[1]}


def _snapshot(conn: Connection) -> tuple[dict[str, Any], dict[str, list]]:
    summary, rows = {}, {}
    for t in TABLES:
        rs = _rows(conn, t)
        rows[t] = rs
        summary[t] = {"count": len(rs), "max_id": max((r["id"] for r in rs), default=0), "sha256": _sha(_canon(rs))}
    return summary, rows


def _alembic(conn: Connection) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _is_final(conn: Connection, inp: Inputs) -> bool:
    counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}
    return (counts == EXPECTED and not gi.verify_loaded(conn, inp.gvr) and not di.verify_loaded(conn, inp.dvr)
            and not category_load.verify(conn, inp.cf))


def _build_spec(conn: Connection, url: str, inp: Inputs, run_id: str, code_sha: str) -> dict[str, Any]:
    if _is_final(conn, inp):
        raise Stop("DB 가 이미 최종본과 같다 — 다시 지우고 넣지 않음(PK 만 바뀐다)")
    summary, rows = _snapshot(conn)
    refs = _refs(conn)
    if refs["job_display_category"]:
        raise Stop(f"카테고리를 가리키는 작업 {refs['job_display_category']} — 삭제가 작업 값을 바꾸므로 중단")
    return {"schema": SPEC_SCHEMA, "run_id": run_id, "code_sha": code_sha, "target": _target(conn, url),
            "alembic_revision": _alembic(conn), "files": inp.files(), "expected": EXPECTED,
            "v9_load_ids": list(V9_LOAD_IDS), "before": summary, "refs": refs, "before_rows": rows}


def _key(run_id: str, name: str) -> str:
    return f"seed/dictionary-full-reload/{run_id}/{name}.json"


def _load_spec(store: Any, run_id: str, spec_sha: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = store.get(_key(run_id, "spec"))
    except Exception as e:
        raise Stop(f"명세를 읽을 수 없음: {e.__class__.__name__}") from e
    if _sha(raw) != spec_sha:
        raise Stop("저장된 명세의 해시가 승인된 값과 다름")
    spec = json.loads(raw)
    if spec.get("schema") != SPEC_SCHEMA or spec.get("run_id") != run_id:
        raise Stop("명세 형식 또는 실행 ID 가 다름")
    return spec, raw


def _lock(conn: Connection) -> None:
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE"))
    conn.execute(text("LOCK TABLE job, section_verdict, text_block IN SHARE MODE"))  # 작업 쪽 참조가 그동안 바뀌지 않게


def _run_detail(conn: Connection, action: str, run_id: str) -> Optional[dict[str, Any]]:
    rows = conn.execute(text("SELECT detail FROM audit_log WHERE action_type = :a AND detail->>'run_id' = :r ORDER BY id"),
                        {"a": action, "r": run_id}).scalars().all()
    if len(rows) > 1:
        raise Unknown(f"같은 실행 ID 의 감사 기록이 {len(rows)}건")
    return rows[0] if rows else None


def _audit(conn: Connection, action: str, detail: dict[str, Any]) -> None:
    conn.execute(text("INSERT INTO audit_log (actor_type, action_type, target_type, target_id, detail) "
                      "VALUES ('system', :a, 'dictionary', NULL, CAST(:d AS jsonb))"),
                 {"a": action, "d": json.dumps(detail, ensure_ascii=False, sort_keys=True)})


def _after_summary(conn: Connection) -> dict[str, Any]:
    out = {}
    for t in TABLES:
        rs = _rows(conn, t)
        ids = [r["id"] for r in rs]
        out[t] = {"count": len(rs), "min_id": min(ids, default=None), "max_id": max(ids, default=None),
                  "sha256": _sha(_canon(rs))}
    return out


# ── 쓰기 ─────────────────────────────────────────────────────────────────
def _delete_all(conn: Connection, before: dict[str, Any]) -> None:
    for t in DELETE_ORDER:
        n = conn.execute(text(f"DELETE FROM {t}")).rowcount
        if n != before[t]["count"]:
            raise Stop(f"{t} 삭제 건수 {n} ≠ 잠금 뒤 건수 {before[t]['count']}")


def _post_checks(conn: Connection, inp: Inputs, spec: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """커밋 전 사후 조건. 문제 목록(빈 목록이면 통과)."""
    probs = [f"{i.external_id}: {i.message}" for i in gi.verify_loaded(conn, inp.gvr) + di.verify_loaded(conn, inp.dvr)
             + category_load.verify(conn, inp.cf)]
    for t in TABLES:
        if after[t]["count"] != EXPECTED[t]:
            probs.append(f"{t} 건수 {after[t]['count']} ≠ {EXPECTED[t]}(최종본 밖 행 또는 누락)")
    g_ids = {r[0] for r in conn.execute(text("SELECT external_id FROM glossary")).all()}
    if g_ids != {r["external_id"] for r in inp.gvr.records}:
        probs.append("용어집 원본 ID 집합이 최종본과 다름")
    if conn.execute(text("SELECT count(*) FROM glossary WHERE retired_at IS NOT NULL")).scalar():
        probs.append("용어집에 활성 제외 행이 있음")
    d_ids = {r[0] for r in conn.execute(text("SELECT external_id FROM expression_dictionary")).all()}
    if d_ids != {r["external_id"] for r in inp.dvr.records}:
        probs.append("사전 원본 ID 집합이 최종본과 다름")
    for t in TABLES:
        old_max = spec["before"][t]["max_id"]
        if after[t]["min_id"] is not None and after[t]["min_id"] <= old_max:
            probs.append(f"{t} 새 PK 최소값 {after[t]['min_id']} ≤ 옛 PK 최대값 {old_max} — PK 재사용")
    reused = conn.execute(text("SELECT id FROM expression_dictionary WHERE id = ANY(:v)"),
                          {"v": spec["refs"]["verdict_dictionary_ids"]}).scalars().all()
    if reused:
        probs.append(f"판정이 가리키던 옛 사전 PK {reused} 를 새 행이 씀")
    return probs


def _write(conn: Connection, inp: Inputs, spec: dict[str, Any], spec_sha: str) -> dict[str, Any]:
    _delete_all(conn, spec["before"])
    gplan = gi.build_plan(conn, inp.gvr)
    if gplan.issues or gplan.updates or gplan.unchanged or len(gplan.inserts) != EXPECTED["glossary"]:
        raise Stop(f"용어집 계획이 빈 표 삽입이 아님(삽입 {len(gplan.inserts)} · 갱신 {len(gplan.updates)} · 문제 {len(gplan.issues)})")
    gi.apply_plan(conn, gplan)
    dplan = di.build_plan(conn, inp.dvr)
    if (dplan.issues or dplan.updates or dplan.unchanged or len(dplan.inserts) != EXPECTED["expression_dictionary"]
            or len(dplan.ev_insert) != EXPECTED["expression_dictionary_evidence"] or dplan.ev_update or dplan.ev_demote):
        raise Stop(f"사전 계획이 빈 표 삽입이 아님(삽입 {len(dplan.inserts)} · 근거 {len(dplan.ev_insert)} · 문제 {len(dplan.issues)})")
    di.apply_plan(conn, dplan)
    category_load.apply(conn, inp.cf)
    after = _after_summary(conn)
    probs = _post_checks(conn, inp, spec, after)
    if probs:
        for p in probs[:30]:
            print(f"  - {p}")
        raise Stop(f"적용 뒤 확인 실패 {len(probs)}건 — 전체 롤백")
    detail = {"run_id": spec["run_id"], "spec_sha256": spec_sha, "code_sha": spec["code_sha"], "files": spec["files"],
              "before": spec["before"], "after": after, "refs": spec["refs"],
              "applied_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    _audit(conn, ACT_APPLIED, detail)
    return detail


def _insert_rows(conn: Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = _columns(conn, table)
    names = [c for c, _ in cols]
    self_parent = table == "category_master"
    vals = ", ".join(f"CAST(:{c} AS {t})" for c, t in cols)
    params = []
    for r in rows:
        p = {c: (json.dumps(r[c], ensure_ascii=False) if t == "jsonb" and r[c] is not None else r[c]) for c, t in cols}
        if self_parent:
            p["parent_id"] = None  # 상위를 먼저 넣지 않아도 되게 두 단계로
        params.append(p)
    conn.execute(text(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({vals})"), params)
    parents = [{"p": r["parent_id"], "i": r["id"]} for r in rows if self_parent and r["parent_id"] is not None]
    if parents:  # 빈 목록이면 실행하지 않는다(부모 없는 트리)
        conn.execute(text("UPDATE category_master SET parent_id = CAST(:p AS bigint) WHERE id = :i"), parents)


# ── 명령 ─────────────────────────────────────────────────────────────────
def _commit(trans: Any) -> None:
    trans.commit()


def _finish(store: Any, run_id: str, name: str, payload: dict[str, Any], label: str) -> int:
    try:
        _store_json(store, _key(run_id, name), _canon({"schema": RESULT_SCHEMA, **payload}))
    except Exception as e:
        print(f"{label} · 결과 파일 저장 미완료: {e}")
        print("같은 명령을 다시 실행하면 DB 를 바꾸지 않고 결과 저장만 이어 한다.")
        return EXIT_RESULT_PENDING
    print(f"{label} · 결과 파일 저장 완료: {_key(run_id, name)}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace, url: str, paths: dict[str, str]) -> int:
    inp = Inputs(paths, args.max_errors)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin() as trans:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                spec = _build_spec(conn, url, inp, args.run_id, args.code_sha)
                trans.rollback()
    finally:
        engine.dispose()
    raw = _canon(spec)
    try:
        _store_json(get_store(), _key(args.run_id, "spec"), raw)
    except Stop:
        raise
    except Exception as e:
        raise Stop(f"명세 저장 실패: {e.__class__.__name__}") from e
    b, r = spec["before"], spec["refs"]
    print(f"- 대상 DB {spec['target']['db_name']} · 대상 식별 {spec['target']['locator_sha256'][:12]} · revision {spec['alembic_revision']}")
    print("- 삭제(현재): " + " · ".join(f"{t} {b[t]['count']}(PK 최대 {b[t]['max_id']})" for t in TABLES))
    print("- 삽입(최종본): " + " · ".join(f"{t} {EXPECTED[t]}" for t in TABLES))
    print(f"- 작업 참조: 카테고리 {len(r['job_display_category'])} · 판정 dictionary_id {r['verdict_rows']}행"
          f"(서로 다른 {len(r['verdict_dictionary_ids'])}개) · flag 용어집 {r['flag_glossary']} · 사전 {r['flag_dictionary']}")
    print(f"- 명세 {_key(args.run_id, 'spec')} · SHA-256 {_sha(raw)}")
    print("결과: plan 완료 — DB 에 쓰지 않음. 실행 ID 와 명세 SHA-256 을 승인받은 뒤 apply")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, url: str, paths: dict[str, str]) -> int:
    store = get_store()
    spec, _raw = _load_spec(store, args.run_id, args.spec_sha)
    if spec["code_sha"] != args.code_sha:
        raise Stop("명세의 코드 SHA 와 실행 코드 SHA 가 다름")
    inp = Inputs(paths, args.max_errors)
    if inp.files() != spec["files"]:
        raise Stop("입력 파일이 명세와 다름")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                _check_target(conn, url, spec)
                _lock(conn)
                if _run_detail(conn, ACT_RESTORED, args.run_id) is not None:
                    raise Stop("이미 복구한 실행 — 같은 실행 ID 로 다시 적용하지 않음")
                detail = _run_detail(conn, ACT_APPLIED, args.run_id)
                if detail is not None:
                    if detail["spec_sha256"] != args.spec_sha:
                        raise Unknown("감사 기록의 명세 해시가 다름")
                    if {t: v["sha256"] for t, v in _after_summary(conn).items()} != {t: v["sha256"] for t, v in detail["after"].items()}:
                        raise Unknown("적용 기록은 있으나 DB 값이 적용 결과와 다름")
                    trans.rollback()
                    print("이미 적용된 실행 — DB 를 바꾸지 않음")
                else:
                    summary, _rows_now = _snapshot(conn)
                    if (summary != spec["before"] or _refs(conn) != spec["refs"]
                            or _alembic(conn) != spec["alembic_revision"]):
                        raise Stop("잠금 뒤 현재 행 · 작업 참조 · PK 가 명세와 다름 — 쓰지 않음")
                    if spec["refs"]["job_display_category"]:  # plan 의 확인과 이중 — 삭제가 작업 값을 바꾸면 쓰지 않는다
                        raise Stop("카테고리를 가리키는 작업이 있음 — 쓰지 않음")
                    detail = _write(conn, inp, spec, args.spec_sha)
                    try:
                        _commit(trans)
                    except Exception as e:
                        raise Unknown(f"커밋 응답 확인 실패({e.__class__.__name__})") from e
                    a = detail["after"]
                    print("적용 완료 — " + " · ".join(f"{t} {a[t]['count']}(PK {a[t]['min_id']}~{a[t]['max_id']})" for t in TABLES))
            except BaseException:
                _rollback(trans)
                raise
    finally:
        engine.dispose()
    return _finish(store, args.run_id, "result", {"status": "applied", **detail}, "DB 적용 완료")


def cmd_restore(args: argparse.Namespace, url: str) -> int:
    store = get_store()
    spec, _raw = _load_spec(store, args.run_id, args.spec_sha)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                _check_target(conn, url, spec)
                _lock(conn)
                restored = _run_detail(conn, ACT_RESTORED, args.run_id)
                if restored is not None:
                    summary, _ = _snapshot(conn)
                    if summary != spec["before"]:
                        raise Unknown("복구 기록은 있으나 DB 값이 적용 전과 다름")
                    trans.rollback()
                    print("이미 복구된 실행 — DB 를 바꾸지 않음")
                    detail = restored
                else:
                    applied = _run_detail(conn, ACT_APPLIED, args.run_id)
                    if applied is None or applied["spec_sha256"] != args.spec_sha:
                        raise Stop("이 명세의 적용 기록이 없음 — 복구하지 않음")
                    now = _after_summary(conn)
                    if {t: v["sha256"] for t, v in now.items()} != {t: v["sha256"] for t, v in applied["after"].items()}:
                        raise Stop("적용 뒤 사전 값이 바뀜 — 복구하지 않음(차이를 먼저 확인)")
                    if _refs(conn)["job_display_category"]:
                        raise Stop("새 카테고리를 가리키는 작업이 생김 — 복구가 작업 값을 바꾸므로 중단")
                    _delete_all(conn, applied["after"])
                    for t in TABLES:
                        _insert_rows(conn, t, spec["before_rows"][t])
                    summary, _ = _snapshot(conn)
                    if summary != spec["before"]:
                        raise Stop("복구 뒤 값이 적용 전과 다름 — 전체 롤백")
                    missing = [v for v in spec["refs"]["verdict_dictionary_ids"]
                               if not conn.execute(text("SELECT 1 FROM expression_dictionary WHERE id = :i"), {"i": v}).first()]
                    if missing:
                        raise Stop(f"판정이 가리키던 사전 PK {missing} 복구 안 됨 — 전체 롤백")
                    detail = {"run_id": args.run_id, "spec_sha256": args.spec_sha, "restored": spec["before"],
                              "restored_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                    _audit(conn, ACT_RESTORED, detail)
                    try:
                        _commit(trans)
                    except Exception as e:
                        raise Unknown(f"커밋 응답 확인 실패({e.__class__.__name__})") from e
                    print("복구 완료 — 적용 전 행 · PK 그대로(판정이 가리키던 사전 PK 포함)")
            except BaseException:
                _rollback(trans)
                raise
    finally:
        engine.dispose()
    return _finish(store, args.run_id, "restore-result", {"status": "restored", **detail}, "DB 복구 완료")


def cmd_status(args: argparse.Namespace, url: str) -> int:
    """적용 · 복구 상태(읽기 전용). 먼저 저장된 명세의 해시(승인값) · 현재 접속 대상(DB · 버킷) · 감사 기록의 명세 해시를 대조한다.
    종료 0 = 기록과 현재 DB 값이 맞음(applied: 적용 결과와 같음, restored · not_applied: 적용 전과 같음).
    1 = 명세 · 대상이 다름(판정하지 않음). 3 = 기록과 DB 값이 맞지 않거나 기록이 모순 — 상태를 단정하지 않음."""
    spec, _raw = _load_spec(get_store(), args.run_id, args.spec_sha)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin() as trans:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                _check_target(conn, url, spec)
                applied = _run_detail(conn, ACT_APPLIED, args.run_id)
                restored = _run_detail(conn, ACT_RESTORED, args.run_id)
                for d in (applied, restored):
                    if d is not None and d.get("spec_sha256") != args.spec_sha:
                        raise Unknown("감사 기록의 명세 해시가 승인된 값과 다름")
                if restored is not None and applied is None:
                    raise Unknown("복구 기록만 있고 적용 기록이 없음")
                now = _after_summary(conn)
                if applied is not None and restored is None:
                    state, basis = "applied", "적용 결과와"
                    match = {t: v["sha256"] for t, v in now.items()} == {t: v["sha256"] for t, v in applied["after"].items()}
                else:
                    state, basis = ("restored" if restored is not None else "not_applied"), "적용 전과"
                    match = _snapshot(conn)[0] == spec["before"]
                trans.rollback()
    finally:
        engine.dispose()
    print(f"- 대상 일치: DB {spec['target']['db_name']} · 대상 식별 {spec['target']['locator_sha256'][:12]} · 명세 SHA-256 일치")
    print(f"status {state} · 현재 값이 {basis} 같음 {match}")
    print("현재: " + " · ".join(f"{t} {now[t]['count']}" for t in TABLES))
    if not match:
        print("결과: 완료 불명 — 감사 기록과 현재 DB 값이 맞지 않음. 상태를 단정하지 않음")
        return EXIT_UNKNOWN
    return EXIT_OK


def main(argv: Optional[list[str]] = None, database_url: Optional[str] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.dictionary_full_reload", description="최종 사전 전체 교체")
    p.add_argument("mode", choices=["plan", "apply", "status", "restore"])
    p.add_argument("--run-id", required=True, help="실행 ID(영문·숫자·. _ -)")
    p.add_argument("--code-sha", help="실행 코드 SHA. plan·apply 필수")
    p.add_argument("--spec-sha", help="승인된 명세 SHA-256. plan 외 필수")
    for kind in KINDS:
        p.add_argument(f"--{kind}", metavar="XLSX")
    p.add_argument("--max-errors", type=int, default=50)
    args = p.parse_args(argv)
    if not RUN_ID.match(args.run_id):
        p.error("실행 ID 형식이 맞지 않음")
    if args.mode != "plan" and not (args.spec_sha and SHA256.match(args.spec_sha)):
        p.error("--spec-sha 는 SHA-256 전체 값이어야 함")
    paths = {k: getattr(args, k) for k in KINDS if getattr(args, k)}
    if args.mode in ("plan", "apply"):
        if not args.code_sha:
            p.error("--code-sha 필요")
        if set(paths) != set(KINDS):
            p.error("파일 6개(--certification --ingredient --phrase --regulatory --local --category)가 모두 필요")
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL 환경변수가 없음")
        return EXIT_ENV
    if not os.getenv("S3_BUCKET"):
        print("S3_BUCKET 환경변수가 없음 — 대상을 식별할 수 없어 실행하지 않음")
        return EXIT_ENV
    try:
        if args.mode == "plan":
            return cmd_plan(args, url, paths)
        if args.mode == "apply":
            return cmd_apply(args, url, paths)
        if args.mode == "restore":
            return cmd_restore(args, url)
        return cmd_status(args, url)
    except Stop as e:
        print(f"결과: 중단 — {e}. 이번 실행으로 DB 를 바꾸지 않음")
        return EXIT_STOP
    except Unknown as e:
        print(f"결과: 완료 불명 — {e}. 추정하지 않음. status 로 확인하거나 같은 명령을 다시 실행해 판정")
        return EXIT_UNKNOWN
    except (DBAPIError, RuntimeError, ValueError, KeyError, OSError) as e:
        print(f"결과: 중단 — {e.__class__.__name__}: {str(e).splitlines()[0][:200]}. 커밋 전이므로 이번 실행의 변경은 남지 않음")
        return EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
