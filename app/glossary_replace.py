"""인증·성분 용어집 교체 CLI. 일반 적재(`app.glossary_ingest`)와 분리한다.

    python -m app.glossary_replace plan    --run-id R --code-sha S --certification C.xlsx --ingredient I.xlsx
    python -m app.glossary_replace apply   --run-id R --code-sha S --spec-sha H --certification C.xlsx --ingredient I.xlsx
    python -m app.glossary_replace status  --run-id R --spec-sha H
    python -m app.glossary_replace restore --run-id R --spec-sha H

방식: 같은 PK 갱신 + 빠진 행 retired_at 표시 + 변경 전 전체 값 보존. 문구는 대상이 아니다.
모든 과거 번역의 공급 내용을 재현하지는 않는다(공급 내용은 시도에 저장되지 않는다). 교체 직전 값과 이번 변경 내역을 보존한다.

순서
- plan: 파일 검증 · DB 비교로 명세를 만든다. 오브젝트 저장소에 put_if_absent 로 저장하고 되읽어 대조한다. DB 쓰기 없음.
  같은 키에 다른 내용이 있으면 덮어쓰지 않고 중단한다.
- apply: 명세를 되읽어 해시를 대조하고, 잠금 뒤 같은 입력으로 명세를 다시 만들어 저장본과 같을 때만 쓴다.
  DB 변경과 감사 기록은 한 트랜잭션이다. 감사 기록이 실패하면 커밋하지 않는다.
  커밋 뒤 결과 파일을 저장한다. 실패하면 종료 코드 4(DB 적용 완료 · 결과 파일 저장 미완료)이고, 다시 실행하면 DB 를 바꾸지 않고
  저장만 이어 한다. 결과 내용은 감사 기록에서 만들므로 다시 실행해도 같다.
- 커밋 응답을 받지 못하면 종료 코드 3 이다. 추정하지 않는다. 다시 실행하면 잠금을 잡고 감사 기록 · 행 값으로 판정한다.
  같은 실행 ID 의 동시 실행은 표 잠금으로 줄을 세우며, 뒤에 온 쪽은 DB 를 바꾸지 않는다.
- restore: 적용 뒤 값과 DB 전체 열이 같을 때만 되돌린다. 삽입 행은 지우지 않고 retired_at 을 표시한다.
  복구한 실행은 다시 적용하지 않는다.

종료 코드: 0 완료 · 1 중단(이번 실행으로 DB 변경 없음) · 2 환경 오류 · 3 완료 불명 · 4 DB 완료 · 파일 저장 미완료

예외: 아래 두 가지만 코드에 고정한다. 파일 전체 SHA-256 과 대상 · 변경 내용이 맞을 때만 허용하고, 다른 파일에는 적용되지 않는다.
일반 적재의 version · 자연키 · 원본 ID 검사는 바꾸지 않는다.

대상 고정: 명세는 실제 대상에 묶는다. 기존 배포 설정의 DATABASE_URL(host · port · DB 이름)과 S3_BUCKET 으로 식별값을 만들고,
접속 위치는 해시로만 남긴다. 사용자 · 비밀번호는 넣지 않는다. S3_BUCKET 이 명시되지 않으면 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from typing import Any, Optional

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

from app.glossary_ingest import DB_FIELDS, NATURAL_KEY, SPECS, print_issues, print_summary, validate_files
from app.storage import get_store

KINDS = ("certification", "ingredient")
SPEC_SCHEMA = "glossary-replace-spec@1"
RESULT_SCHEMA = "glossary-replace-result@1"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# 데이터 담당 회신(2026-11-07): 성분 v3.1 의 verified_at 일괄 기입은 version 증가 대상이 아니다.
APPROVED_VERIFIED_AT_ONLY = {"ingredient": "bd7aae6adde3075d8072bd4a65affc893b4ae7c86320f7d0d2c98b6fa46fb46f"}
# 인증 v5 전달 메모: 두 행의 분류가 common 으로 바뀐다.
APPROVED_CATEGORY_CHANGES = {
    "certification": {
        "sha256": "1d4c68125719739200429f4fc8e731b61db7c5438b0d0444db40dbdf6eb4a53c",
        "changes": {
            "GL-C010": ("Sunscreens & Tanning Products", "common"),
            "GL-C013": ("Sunscreens & Tanning Products", "common"),
        },
    },
}

ACT_APPLIED = "glossary_replace_applied"
ACT_RESTORED = "glossary_replace_restored"
ACT_ROW = "glossary_row_replaced"

EXIT_OK, EXIT_STOP, EXIT_ENV, EXIT_UNKNOWN, EXIT_RESULT_PENDING = 0, 1, 2, 3, 4


class Stop(Exception):
    """이번 실행으로 DB 를 바꾸지 않고 중단한다."""


class Unknown(Exception):
    """완료 여부를 확인할 수 없다. 추정하지 않는다."""


def _norm(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc).isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _key(run_id: str, name: str) -> str:
    return f"seed/glossary-replace/{run_id}/{name}.json"


def _fields(row: Any) -> dict[str, Any]:
    return {c: _norm(row[c]) for c in DB_FIELDS}


def _store_json(store: Any, key: str, data: bytes) -> None:
    """같은 키가 있으면 내용을 대조한다. 다른 내용이면 덮어쓰지 않는다. 저장 뒤 되읽어 확인한다."""
    if not store.put_if_absent(key, data, "application/json") and store.get(key) != data:
        raise Stop(f"같은 키에 다른 내용이 있어 덮어쓰지 않음: {key}")
    if store.get(key) != data:
        raise Stop(f"저장 뒤 되읽은 내용이 다름: {key}")


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


def _validate(paths: dict[str, str], max_errors: int = 50):
    vr = validate_files(paths)
    print_summary(vr)
    if vr.issues:
        print_issues(vr.issues, max_errors)
        raise Stop("파일 검증 실패")
    return vr


def _all_rows(conn: Connection) -> list[Any]:
    return conn.execute(text(f"SELECT id, retired_at, {', '.join(DB_FIELDS)} FROM glossary ORDER BY id")).mappings().all()


def _kind_of(row: Any) -> Optional[str]:
    eid = row["external_id"] or ""
    for kind in KINDS:
        if eid.startswith(SPECS[kind].id_prefix) or row["term_kind"] == SPECS[kind].term_kind:
            return kind
    return None


def _snapshot(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "retired_at": _norm(row["retired_at"]), **_fields(row)}


def _split(rows: list[Any], kinds: list[str]) -> tuple[list[dict[str, Any]], str]:
    target, other = [], []
    for r in rows:
        (target if _kind_of(r) in kinds else other).append(_snapshot(r))
    return target, _sha(_canon(other))


def _flag_refs(conn: Connection, pks: list[int]) -> list[dict[str, Any]]:
    """대상 PK 를 가리키는 강제 용어 flag 항목(블록 · revision · flag 내용). 현행 코드는 쓰지 않지만 실제 DB 값은 실행 때 본다.

    glossary_id 는 계약 예시의 숫자형과 공급 목록의 문자열이 모두 쓰이므로 둘 다 같은 PK 로 식별한다. 항목의 flag 는 DB 에서 읽은 JSON 값을 변환하지 않고 그대로 둔다.
    """
    wanted = {str(p) for p in pks}

    def refers(value: Any) -> bool:
        return str(int(value) if isinstance(value, float) and value.is_integer() else value) in wanted

    rows = conn.execute(text(
        "SELECT t.id AS block_id, t.revision, f AS flag FROM text_block t CROSS JOIN LATERAL "
        "jsonb_array_elements(CASE WHEN jsonb_typeof(t.compliance_flags) = 'array' THEN t.compliance_flags "
        "ELSE '[]'::jsonb END) AS f WHERE f->>'glossary_id' IS NOT NULL"
    )).mappings().all()
    items = [{"block_id": r["block_id"], "revision": r["revision"], "flag": r["flag"]}
             for r in rows if refers(r["flag"]["glossary_id"])]
    return sorted(items, key=_canon)


def _target(conn: Connection, url: str) -> dict[str, Any]:
    """실제 대상 식별값. 비밀값은 넣지 않는다. 접속 위치는 해시로만 남긴다."""
    u = make_url(url)
    db_name = conn.execute(text("SELECT current_database()")).scalar()
    if not u.host or u.database != db_name:
        raise Stop("접속 설정으로 대상 DB 를 식별할 수 없음")
    bucket = os.environ["S3_BUCKET"]
    locator = {"host": u.host.lower(), "port": u.port or 5432, "database": u.database, "s3_bucket": bucket}
    return {"db_name": db_name, "s3_bucket": bucket, "locator_sha256": _sha(_canon(locator))}


def _check_target(conn: Connection, url: str, spec: dict[str, Any]) -> None:
    if spec.get("target") != _target(conn, url):
        raise Stop("명세의 대상과 현재 접속 대상이 다름 — 이 명세를 쓰지 않음")


def _classify(kind: str, file_sha: str, cur: Any, rec: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(변경 종류, 위반 사유). 내용이 같으면 (None, None)."""
    changed = [c for c in DB_FIELDS if _norm(cur[c]) != _norm(rec[c])]
    if not changed:
        return None, None
    if "term_ko" in changed or "target_lang" in changed:
        return None, "같은 ID 의 원문·대상 언어가 바뀜"
    if cur["version"] is None or rec["version"] < cur["version"]:
        return None, f"version 이 낮아지거나 없음(DB {cur['version']} → 파일 {rec['version']})"
    if rec["version"] == cur["version"]:
        if changed == ["verified_at"] and APPROVED_VERIFIED_AT_ONLY.get(kind) == file_sha:
            return "verified_at_only", None
        return None, f"같은 version 인데 내용이 다름 {changed}"
    if "internal_category" in changed:
        rule = APPROVED_CATEGORY_CHANGES.get(kind)
        allowed = rule["changes"].get(rec["external_id"]) if rule and rule["sha256"] == file_sha else None
        if allowed != (cur["internal_category"], rec["internal_category"]):
            return None, f"승인되지 않은 분류 변경({cur['internal_category']} → {rec['internal_category']})"
        return "category_change", None
    return "version_up", None


def _build_spec(conn: Connection, url: str, vr: Any, paths: dict[str, str], run_id: str, code_sha: str) -> dict[str, Any]:
    kinds = [f.kind for f in vr.files]
    rows = _all_rows(conn)
    by_ext = {r["external_id"]: r for r in rows if r["external_id"] is not None}
    by_key = {tuple(r[k] for k in NATURAL_KEY): r for r in rows}
    problems: list[str] = []
    legacy = sum(1 for r in rows if r["external_id"] is None)
    if legacy:
        problems.append(f"원본 ID 없는 행 {legacy}개 — 대상을 판정할 수 없음")
    for r in rows:
        eid = r["external_id"] or ""
        for kind in kinds:
            if eid.startswith(SPECS[kind].id_prefix) and r["term_kind"] != SPECS[kind].term_kind:
                problems.append(f"{eid}: 원본 ID 대역과 term_kind({r['term_kind']})가 다름")
    updates: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    retires: list[dict[str, Any]] = []
    unchanged = 0
    files: dict[str, Any] = {}
    for f in vr.files:
        sha = _file_sha(paths[f.kind])
        files[f.kind] = {"sha256": sha, "rows": len(f.records), "sheet": SPECS[f.kind].sheet}
        if not f.records:
            problems.append(f"{f.kind}: 적재 대상 행이 0개 — 전체 비활성화로 이어지므로 진행하지 않음")
        if f.excluded_ids:
            problems.append(f"{f.kind}: 파일에 적재 제외 행 {len(f.excluded_ids)}개 — 교체 입력은 적재 대상 행만 담는다")
        incoming = set()
        for rec in f.records:
            eid = rec["external_id"]
            incoming.add(eid)
            key = tuple(rec[k] for k in NATURAL_KEY)
            cur = by_ext.get(eid)
            other = by_key.get(key)
            if other is not None and other["external_id"] != eid:
                problems.append(f"{eid}: 자연키가 다른 행({other['external_id']})과 같음")
                continue
            if cur is None:
                inserts.append(_fields(rec))
                continue
            if cur["retired_at"] is not None:
                problems.append(f"{eid}: 활성에서 제외된 원본 ID 가 파일에 있음")
                continue
            change, why = _classify(f.kind, sha, cur, rec)
            if why:
                problems.append(f"{eid}: {why}")
            elif change is None:
                unchanged += 1
            else:
                updates.append({"id": cur["id"], "external_id": eid, "change": change, "after": _fields(rec)})
        prefix = SPECS[f.kind].id_prefix
        for r in rows:
            eid = r["external_id"] or ""
            if eid.startswith(prefix) and eid not in incoming and r["retired_at"] is None:
                retires.append({"id": r["id"], "external_id": eid})
    if problems:
        print_stop_list(problems)
        raise Stop(f"교체 규칙 위반 {len(problems)}건")
    target, other_sha = _split(rows, kinds)
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    updates.sort(key=lambda u: u["id"])
    retires.sort(key=lambda r: r["id"])
    return {
        "schema": SPEC_SCHEMA, "run_id": run_id, "code_sha": code_sha,
        "target": _target(conn, url), "alembic_revision": revision,
        "kinds": kinds, "files": files, "before": target, "other_rows_sha256": other_sha,
        "updates": updates, "retires": retires, "inserts": inserts, "unchanged": unchanged,
        "flag_refs": _flag_refs(conn, [r["id"] for r in target]),
    }


def print_stop_list(problems: list[str], limit: int = 30) -> None:
    for p in problems[:limit]:
        print(f"  - {p}")
    if len(problems) > limit:
        print(f"  … 외 {len(problems) - limit}건")


def _counts(spec: dict[str, Any]) -> dict[str, int]:
    by_change: dict[str, int] = {}
    for u in spec["updates"]:
        by_change[u["change"]] = by_change.get(u["change"], 0) + 1
    return {"inserts": len(spec["inserts"]), "retires": len(spec["retires"]), "unchanged": spec["unchanged"],
            "updates": len(spec["updates"]), **{f"updates_{k}": v for k, v in sorted(by_change.items())}}


def _lock(conn: Connection) -> None:
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text("LOCK TABLE glossary IN SHARE ROW EXCLUSIVE MODE"))


def _run_detail(conn: Connection, action: str, run_id: str) -> Optional[dict[str, Any]]:
    rows = conn.execute(text("SELECT detail FROM audit_log WHERE action_type = :a AND detail->>'run_id' = :r ORDER BY id"),
                        {"a": action, "r": run_id}).scalars().all()
    if len(rows) > 1:
        raise Unknown(f"같은 실행 ID 의 감사 기록이 {len(rows)}건")
    return rows[0] if rows else None


def _insert_audit(conn: Connection, rows: list[dict[str, Any]]) -> None:
    conn.execute(
        text("INSERT INTO audit_log (actor_type, action_type, target_type, target_id, detail) "
             "VALUES ('system', :action, :target_type, :target_id, :detail)")
        .bindparams(bindparam("detail", type_=JSONB)),
        rows,
    )


def _expected_after(spec: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    rows = {r["id"]: dict(r) for r in spec["before"]}
    for u in spec["updates"]:
        rows[u["id"]].update(u["after"])
    for r in spec["retires"]:
        rows[r["id"]]["retired_at"] = detail["retired_at"]
    for rec in spec["inserts"]:
        pk = detail["inserted"][rec["external_id"]]
        rows[pk] = {"id": pk, "retired_at": None, **rec}
    return [rows[k] for k in sorted(rows)]


def _expected_restored(spec: dict[str, Any], applied: dict[str, Any], restored_at: str) -> list[dict[str, Any]]:
    rows = {r["id"]: dict(r) for r in spec["before"]}
    for rec in spec["inserts"]:
        pk = applied["inserted"][rec["external_id"]]
        rows[pk] = {"id": pk, "retired_at": restored_at, **rec}
    return [rows[k] for k in sorted(rows)]


def _diff_ids(want: list[dict[str, Any]], got: list[dict[str, Any]]) -> list[str]:
    w, g = {r["id"]: r for r in want}, {r["id"]: r for r in got}
    out = []
    for pk in sorted(set(w) | set(g)):
        if w.get(pk) != g.get(pk):
            r = w.get(pk) or g.get(pk) or {}
            out.append(f"id={pk} {r.get('external_id')}")
    return out


def _matches(conn: Connection, spec: dict[str, Any], want: list[dict[str, Any]]) -> list[str]:
    got, other = _split(_all_rows(conn), spec["kinds"])
    diff = _diff_ids(want, got)
    if other != spec["other_rows_sha256"]:
        diff.append("대상 밖 행 지문이 다름")
    return diff


def _write_changes(conn: Connection, spec: dict[str, Any], spec_sha: str) -> dict[str, Any]:
    now = conn.execute(text("SELECT now()")).scalar()
    sets = ", ".join(f"{c} = :{c}" for c in DB_FIELDS if c != "external_id")
    if spec["updates"]:
        conn.execute(text(f"UPDATE glossary SET {sets} WHERE id = :pk AND external_id = :external_id AND retired_at IS NULL"),
                     [{**_to_db(u["after"]), "pk": u["id"]} for u in spec["updates"]])
    if spec["retires"]:
        conn.execute(text("UPDATE glossary SET retired_at = :t WHERE id = :pk AND retired_at IS NULL"),
                     [{"t": now, "pk": r["id"]} for r in spec["retires"]])
    inserted: dict[str, int] = {}
    cols, vals = ", ".join(DB_FIELDS), ", ".join(f":{c}" for c in DB_FIELDS)
    for rec in spec["inserts"]:
        inserted[rec["external_id"]] = conn.execute(
            text(f"INSERT INTO glossary ({cols}) VALUES ({vals}) RETURNING id"), _to_db(rec)).scalar_one()
    detail = {
        "run_id": spec["run_id"], "spec_sha256": spec_sha, "spec_key": _key(spec["run_id"], "spec"),
        "retired_at": _norm(now), "inserted": inserted, "counts": _counts(spec), "code_sha": spec["code_sha"],
        "files": {k: v["sha256"] for k, v in spec["files"].items()},
    }
    before = {r["id"]: r for r in spec["before"]}
    audit = [{"action": ACT_APPLIED, "target_type": "glossary_replace", "target_id": None, "detail": detail}]
    for u in spec["updates"]:
        if u["change"] != "verified_at_only":  # 20,540행은 실행 기록의 건수와 명세로 남긴다
            audit.append({"action": ACT_ROW, "target_type": "glossary", "target_id": u["id"], "detail": {
                "run_id": spec["run_id"], "external_id": u["external_id"], "change": u["change"],
                "before": before[u["id"]], "after": u["after"]}})
    for r in spec["retires"]:
        audit.append({"action": ACT_ROW, "target_type": "glossary", "target_id": r["id"], "detail": {
            "run_id": spec["run_id"], "external_id": r["external_id"], "change": "retired", "before": before[r["id"]]}})
    for rec in spec["inserts"]:
        audit.append({"action": ACT_ROW, "target_type": "glossary", "target_id": inserted[rec["external_id"]], "detail": {
            "run_id": spec["run_id"], "external_id": rec["external_id"], "change": "inserted", "after": rec}})
    _insert_audit(conn, audit)
    return detail


def _to_db(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    if out.get("verified_at"):
        out["verified_at"] = dt.date.fromisoformat(out["verified_at"])
    return out


def _commit(trans: Any) -> None:
    trans.commit()


def _rollback(trans: Any) -> None:
    """연결이 끊겨 롤백이 실패해도 원래 판정(중단 · 완료 불명)을 바꾸지 않는다."""
    try:
        if trans.is_active:
            trans.rollback()
    except Exception:
        pass


def _save_result(store: Any, run_id: str, name: str, payload: dict[str, Any]) -> None:
    _store_json(store, _key(run_id, name), _canon({"schema": RESULT_SCHEMA, **payload}))


def _finish_result(store: Any, run_id: str, name: str, payload: dict[str, Any], label: str) -> int:
    try:
        _save_result(store, run_id, name, payload)
    except Exception as e:
        print(f"{label} · 결과 파일 저장 미완료: {e}")
        print("같은 명령을 다시 실행하면 DB 를 바꾸지 않고 결과 저장만 이어 한다.")
        return EXIT_RESULT_PENDING
    print(f"{label} · 결과 파일 저장 완료: {_key(run_id, name)}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace, url: str, paths: dict[str, str]) -> int:
    vr = _validate(paths, args.max_errors)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin() as trans:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                spec = _build_spec(conn, url, vr, paths, args.run_id, args.code_sha)
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
    print(f"- 대상 DB {spec['target']['db_name']} · 버킷 {spec['target']['s3_bucket']} · 대상 식별 {spec['target']['locator_sha256'][:12]}"
          f" · revision {spec['alembic_revision']} · 건수 {_counts(spec)}")
    print(f"- 기존 flag 참조 {len(spec['flag_refs'])}건")
    print(f"- 명세 {_key(args.run_id, 'spec')}")
    print(f"- 명세 SHA-256 {_sha(raw)}")
    print("결과: plan 완료 — DB 에 쓰지 않음. 실행 ID 와 명세 SHA-256 을 승인받은 뒤 apply")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, url: str, paths: dict[str, str]) -> int:
    store = get_store()
    spec, raw = _load_spec(store, args.run_id, args.spec_sha)
    if spec["code_sha"] != args.code_sha:
        raise Stop("명세의 코드 SHA 와 실행 코드 SHA 가 다름")
    vr = _validate(paths, args.max_errors)
    if {k: _file_sha(p) for k, p in paths.items()} != {k: v["sha256"] for k, v in spec["files"].items()}:
        raise Stop("입력 파일 해시가 명세와 다름")
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
                    if _matches(conn, spec, _expected_after(spec, detail)):
                        raise Unknown("적용 기록은 있으나 DB 값이 적용 결과와 다름")
                    trans.rollback()
                    print("이미 적용된 실행 — DB 를 바꾸지 않음")
                else:
                    fresh = _canon(_build_spec(conn, url, vr, paths, args.run_id, args.code_sha))
                    if fresh != raw:
                        raise Stop("잠금 뒤 재대조 결과가 명세와 다름 — 쓰지 않음")
                    detail = _write_changes(conn, spec, args.spec_sha)
                    diff = _matches(conn, spec, _expected_after(spec, detail))
                    if diff:
                        print_stop_list(diff)
                        raise Stop("적용 뒤 값이 명세와 다름 — 전체 롤백")
                    try:
                        _commit(trans)
                    except Exception as e:
                        raise Unknown(f"커밋 응답 확인 실패({e.__class__.__name__})") from e
                    print(f"적용 완료 — 건수 {detail['counts']}")
            except BaseException:
                _rollback(trans)
                raise
    finally:
        engine.dispose()
    return _finish_result(store, args.run_id, "result", {"status": "applied", **detail}, "DB 적용 완료")


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
                applied = _run_detail(conn, ACT_APPLIED, args.run_id)
                if applied is None:
                    raise Stop("적용 기록이 없는 실행 — 복구하지 않음")
                restored = _run_detail(conn, ACT_RESTORED, args.run_id)
                if restored is not None:
                    if _matches(conn, spec, _expected_restored(spec, applied, restored["restored_at"])):
                        raise Unknown("복구 기록은 있으나 DB 값이 복구 결과와 다름")
                    trans.rollback()
                    print("이미 복구된 실행 — DB 를 바꾸지 않음")
                else:
                    diff = _matches(conn, spec, _expected_after(spec, applied))
                    if diff:
                        print_stop_list(diff)
                        raise Stop(f"적용 뒤 수정된 값 {len(diff)}건 — 복구하지 않음, 사람이 판단")
                    pks = [r["id"] for r in spec["before"]] + list(applied["inserted"].values())
                    flags = _flag_refs(conn, pks)
                    if _canon(flags) != _canon(spec["flag_refs"]):  # DB 에서 읽은 JSON 값의 정규 직렬화 비교: 파이썬 == 는 3 과 3.0, 1 과 true 를 같게 본다
                        raise Stop(f"flag 참조가 교체 때와 달라 복구하지 않음 — 현재 {len(flags)}건, 명세 {len(spec['flag_refs'])}건")
                    now = conn.execute(text("SELECT now()")).scalar()
                    before = {r["id"]: r for r in spec["before"]}
                    sets = ", ".join(f"{c} = :{c}" for c in DB_FIELDS if c != "external_id")
                    if spec["updates"]:
                        conn.execute(text(f"UPDATE glossary SET {sets} WHERE id = :pk AND external_id = :external_id"),
                                     [{**_to_db({c: before[u["id"]][c] for c in DB_FIELDS}), "pk": u["id"]} for u in spec["updates"]])
                    if spec["retires"]:
                        conn.execute(text("UPDATE glossary SET retired_at = NULL WHERE id = :pk"),
                                     [{"pk": r["id"]} for r in spec["retires"]])
                    if applied["inserted"]:
                        conn.execute(text("UPDATE glossary SET retired_at = :t WHERE id = :pk"),
                                     [{"t": now, "pk": pk} for pk in applied["inserted"].values()])
                    restored = {"run_id": args.run_id, "spec_sha256": args.spec_sha, "restored_at": _norm(now),
                                "counts": _counts(spec)}
                    _insert_audit(conn, [{"action": ACT_RESTORED, "target_type": "glossary_replace", "target_id": None,
                                          "detail": restored}])
                    diff = _matches(conn, spec, _expected_restored(spec, applied, restored["restored_at"]))
                    if diff:
                        print_stop_list(diff)
                        raise Stop("복구 뒤 값이 명세와 다름 — 전체 롤백")
                    try:
                        _commit(trans)
                    except Exception as e:
                        raise Unknown(f"커밋 응답 확인 실패({e.__class__.__name__})") from e
                    print("복구 완료")
            except BaseException:
                _rollback(trans)
                raise
    finally:
        engine.dispose()
    return _finish_result(store, args.run_id, "restore-result", {"status": "restored", **restored}, "DB 복구 완료")


def cmd_status(args: argparse.Namespace, url: str) -> int:
    spec, _raw = _load_spec(get_store(), args.run_id, args.spec_sha)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin() as trans:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                _check_target(conn, url, spec)
                applied = _run_detail(conn, ACT_APPLIED, args.run_id)
                restored = _run_detail(conn, ACT_RESTORED, args.run_id)
                if restored is not None and applied is not None:
                    state = "restored" if not _matches(conn, spec, _expected_restored(spec, applied, restored["restored_at"])) else "unknown"
                elif applied is not None:
                    state = "applied" if not _matches(conn, spec, _expected_after(spec, applied)) else "unknown"
                else:
                    state = "not_applied" if not _matches(conn, spec, spec["before"]) else "unknown"
                trans.rollback()
    finally:
        engine.dispose()
    print(f"상태: {state}")
    return EXIT_UNKNOWN if state == "unknown" else EXIT_OK


def main(argv: Optional[list[str]] = None, database_url: Optional[str] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.glossary_replace", description="인증·성분 용어집 교체")
    p.add_argument("mode", choices=["plan", "apply", "status", "restore"])
    p.add_argument("--run-id", required=True, help="실행 ID(영문·숫자·. _ -)")
    p.add_argument("--code-sha", help="실행 코드 SHA. plan·apply 필수")
    p.add_argument("--spec-sha", help="승인된 명세 SHA-256. plan 외 필수")
    for kind in KINDS:
        p.add_argument(f"--{kind}", metavar="XLSX", help=f"{SPECS[kind].sheet} 시트가 있는 파일")
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
        if not paths:
            p.error("--certification / --ingredient 중 하나 이상 필요")
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
        print(f"결과: 중단 — {e.__class__.__name__}. 커밋 전이므로 이번 실행의 변경은 남지 않음")
        return EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
