"""최종 사전 전체 교체 — 현지 v9 파서 · 카테고리 파서 · 0014 마이그레이션 · 전체 교체 CLI(plan · apply · restore).

파일 파서 시험은 DB 없이 돈다. DB 시험은 GLOSSARY_TEST_DATABASE_URL(CREATE DATABASE 권한)이 있을 때만 돌고, 시험마다 전용 DB 를 만든다.
CLI 시험은 작은 합성 파일을 쓰려고 고정 해시 · 기대 건수 · v9 고정 목록을 시험 안에서만 바꾼다(실제 최종본 고정값은 실제 파일 시험이 본다).
실제 최종본 시험은 FINAL_DICT_DIR 에 최종 파일 6개가 있을 때만 돈다.
"""
import hashlib
import os
from pathlib import Path

import openpyxl
import pytest
from alembic import command

from app import category_load
from app import dictionary_full_reload as fr
from app.dictionary_ingest import CHANNEL_EVIDENCE_TYPE, V9_COLUMNS, validate_files
from app.storage import MemoryStore, set_store
from tests.dictionary_fixtures import basic_regulatory, local_row, write_local
from tests.glossary_fixtures import base_row, write_book
from tests.test_glossary_db import ADMIN_URL, _alembic, _q, db_url  # noqa: F401 (fixture)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="GLOSSARY_TEST_DATABASE_URL 없음")


# ── 합성 파일 ────────────────────────────────────────────────────────────
def v9_row(eid, **over):
    channel = eid.startswith("CH")
    row = {"id": eid, "항목": f"항목 {eid}", "패턴": f"패턴{eid}; 다른{eid}", "판정": "policy" if channel else "must_fix",
           "site_key": "amazon" if channel else None, "구역": "A 금지" if channel else None,
           "제외하는 맥락": "제외 맥락", "제외하지 않는 맥락": "유지 맥락", "셀러 문장": "셀러 문장", "판단 근거": f"근거 {eid}",
           "규정 원문": "policy text" if channel else None, "evidence_article": "G200390640" if channel else None,
           "evidence_url": "https://sellercentral.example/policy" if channel else None,
           "상태": "적재 · 3차", "kr_freq": 3, "kr_corpus": "코퍼스", "verified_at": "2026-10-29" if channel else "2026-09-22"}
    row.update(over)
    return row


def write_v9(path: Path, country: list[dict], channel: list[dict]) -> Path:
    wb = openpyxl.Workbook()
    wb.active.title = "00_읽는법"
    for name, rows in (("현지부적합_국가", country), ("현지부적합_채널", channel)):
        ws = wb.create_sheet(name)
        ws.append(["★ 안내문 — 1행은 헤더가 아니다"])
        ws.append(V9_COLUMNS)
        for r in rows:
            ws.append([r.get(h) for h in V9_COLUMNS])
    wb.save(path)
    return path


CAT_HEADER = ["display_category_code", "level1", "level2", "level3", "display_name", "full_path", "glossary_category_code",
              "glossary_category_name"]


def cat_row(code, l1, l2, l3, g="G01", **over):
    levels = [x for x in (l1, l2, l3) if x]
    row = dict(zip(CAT_HEADER, [code, l1, l2, l3, levels[-1], " > ".join(levels), g, "이름"]))
    row.update(over)
    return row


def write_category(path: Path, rows: list[dict], g_codes=("G01", "G02", "G11")) -> Path:
    wb = openpyxl.Workbook()
    wb.active.title = "00_README"
    ws = wb.create_sheet("01_용어집카테고리")
    ws.append(["glossary_category_code", "glossary_category_name"])
    for g in g_codes:
        ws.append([g, f"이름 {g}"])
    ws = wb.create_sheet("02_표시카테고리")
    ws.append(CAT_HEADER)
    for r in rows:
        ws.append([r.get(h) for h in CAT_HEADER])
    wb.save(path)
    return path


CATS = [cat_row("SK-01", "스킨케어", "스킨/토너", None), cat_row("SK-03", "스킨케어", "크림", "크림"),
        cat_row("SK-04", "스킨케어", "크림", "아이크림", "G02"), cat_row("TL-01", "뷰티소품", "퍼프", None, "G11")]


# ── 현지 v9 파서 ─────────────────────────────────────────────────────────
def test_v9_loads_country_and_channel_a_only(tmp_path):
    p = write_v9(tmp_path / "v9.xlsx", [v9_row("LC-01"), v9_row("LC-02", 판정="needs_check")],
                 [v9_row("CH-01"), v9_row("CH-08", 구역="B 실증", 상태="미적재 · 이후")])
    vr = validate_files(None, str(p))
    assert not vr.issues
    f = vr.files[0]
    assert f.form == "v9" and f.not_loaded == [("CH-08", "미적재 · 이후")]
    by = {r["external_id"]: r for r in f.records}
    assert set(by) == {"LC-01", "LC-02", "CH-01"}
    assert (by["LC-01"]["dict_type"], by["LC-01"]["verdict_status"], by["LC-01"]["source_verdict_status"]) == ("local", "must_fix", "must_fix")
    assert by["LC-02"]["verdict_status"] == "needs_check"  # 옛 enum 으로 바꾸지 않는다
    assert (by["CH-01"]["dict_type"], by["CH-01"]["verdict_status"], by["CH-01"]["site_key"]) == ("channel", "policy", "amazon")
    assert by["LC-01"]["site_key"] is None and by["LC-01"]["judgment_basis"] == "근거 LC-01"
    assert by["CH-01"]["exclusion_context"] == "제외 맥락"  # 채널 행의 맥락도 받는다
    ev = f.evidence["CH-01"]
    assert (ev["external_id"], ev["evidence_source_type"], ev["evidence_quote"], ev["is_primary"]) == (
        "CH-01", CHANNEL_EVIDENCE_TYPE, "policy text", True)
    assert "LC-01" not in f.evidence


@pytest.mark.parametrize("country,channel,needle", [
    ([v9_row("LC-01", 판정="irrelevant")], [], "판정"),  # 옛 값 · 다른 값은 거부(치환하지 않음)
    ([], [v9_row("CH-09", 구역="B 실증")], "구역"),  # 「적재」 로 표시된 B 행
    ([v9_row("LC-01", site_key="amazon")], [], "site_key"),
    ([v9_row("LC-01", **{"규정 원문": "x"})], [], "규정 원문"),  # 저장할 곳이 없는 값은 버리지 않고 멈춤
    ([], [v9_row("CH-01", evidence_url=None)], "evidence_url"),
    ([v9_row("LC-01", **{"판단 근거": None})], [], "판단 근거"),
    ([v9_row("LC-01", 상태=None)], [], "상태"),
])
def test_v9_rejects(tmp_path, country, channel, needle):
    vr = validate_files(None, str(write_v9(tmp_path / "v9.xlsx", country, channel)))
    assert any(i.column == needle for i in vr.issues), [str(i) for i in vr.issues]
    assert not vr.records


def test_v8_local_format_still_parses(tmp_path):
    vr = validate_files(None, str(write_local(tmp_path / "v8.xlsx", [local_row(1), local_row(2, 판정="needs_fix")])))
    assert not vr.issues and vr.files[0].form == "v8"
    assert all(r["site_key"] is None and r["judgment_basis"] is None for r in vr.records)


# ── 카테고리 파서 ────────────────────────────────────────────────────────
def test_category_tree_and_mapping(tmp_path):
    cf = category_load.parse(str(write_category(tmp_path / "c.xlsx", CATS)))
    assert not cf.issues
    assert [(n.path, n.is_leaf, n.code) for n in cf.nodes] == [
        ("스킨케어", False, None), ("스킨케어 > 스킨/토너", True, "SK-01"), ("스킨케어 > 크림", False, None),
        ("스킨케어 > 크림 > 크림", True, "SK-03"), ("스킨케어 > 크림 > 아이크림", True, "SK-04"),
        ("뷰티소품", False, None), ("뷰티소품 > 퍼프", True, "TL-01")]
    assert [(n.code, n.mapping) for n in cf.leaves] == [("SK-01", "G01"), ("SK-03", "G01"), ("SK-04", "G02"), ("TL-01", "G11")]
    assert cf.nodes[3].name == "크림" and cf.nodes[3].parent_path == "스킨케어 > 크림"


@pytest.mark.parametrize("rows,needle", [
    (CATS + [cat_row("SK-01", "스킨케어", "로션", None)], "display_category_code"),
    ([cat_row("SK-01", "스킨케어", "스킨/토너", None, display_name="다른 이름")], "display_name"),
    ([cat_row("SK-01", "스킨케어", "스킨/토너", None, full_path="스킨케어>스킨/토너")], "full_path"),
    ([cat_row("SK-01", "스킨케어", "스킨/토너", None, "G09")], "glossary_category_code"),
    ([cat_row("SK-01", "스킨케어", "크림", None), cat_row("SK-02", "스킨케어", "크림", "크림")], "full_path"),  # 리프 아래에 행
])
def test_category_rejects(tmp_path, rows, needle):
    cf = category_load.parse(str(write_category(tmp_path / "c.xlsx", rows)))
    assert any(i.column == needle for i in cf.issues), [str(i) for i in cf.issues]


# ── 0014 마이그레이션 ─────────────────────────────────────────────────────
@needs_db
def test_0014_accepts_v9_values_and_downgrade_stops_when_used(db_url):
    command.upgrade(_alembic(), "head")
    _q(db_url, "INSERT INTO expression_dictionary (external_id, source_expression, target_country, regulatory_class, dict_type, "
               "verdict_status, site_key, judgment_basis) VALUES ('LC-01', '항목', 'US', 'common', 'local', 'must_fix', NULL, '근거'), "
               "('CH-01', '채널', 'US', 'common', 'channel', 'policy', 'amazon', '근거')")
    with pytest.raises(Exception):
        _q(db_url, "INSERT INTO expression_dictionary (external_id, source_expression, target_country, regulatory_class, dict_type, "
                   "verdict_status) VALUES ('LC-02', '항목2', 'US', 'common', 'local', 'policy')")
    _q(db_url, "INSERT INTO category_master (name, is_leaf, code) VALUES ('a', true, 'SK-01')")
    with pytest.raises(Exception):
        _q(db_url, "INSERT INTO category_master (name, is_leaf, code) VALUES ('b', true, 'SK-01')")  # 코드 UNIQUE
    with pytest.raises(Exception):
        command.downgrade(_alembic(), "0013")  # 새 값을 쓰는 행이 있으면 멈춘다
    _q(db_url, "DELETE FROM expression_dictionary")
    _q(db_url, "DELETE FROM category_master")
    command.downgrade(_alembic(), "0013")
    command.upgrade(_alembic(), "head")


# ── 전체 교체 CLI(합성 파일) ─────────────────────────────────────────────
@pytest.fixture
def small(tmp_path, monkeypatch):
    paths = {
        "certification": write_book(tmp_path / "c.xlsx", "certification", [base_row("certification", 1)]),
        "ingredient": write_book(tmp_path / "i.xlsx", "ingredient", [base_row("ingredient", 1), base_row("ingredient", 2)]),
        "phrase": write_book(tmp_path / "p.xlsx", "phrase", [base_row("phrase", 1, internal_category="G01")]),
        "regulatory": basic_regulatory(tmp_path / "r.xlsx"),
        "local": write_v9(tmp_path / "v9.xlsx", [v9_row("LC-01")], [v9_row("CH-01"), v9_row("CH-08", 구역="B 실증", 상태="미적재 · 이후")]),
        "category": write_category(tmp_path / "cat.xlsx", CATS),
    }
    paths = {k: str(v) for k, v in paths.items()}
    monkeypatch.setattr(fr, "FINAL_FILES", {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in paths.items()})
    monkeypatch.setattr(fr, "V9_LOAD_IDS", ("LC-01", "CH-01"))
    n_reg = len([r for r in validate_files(paths["regulatory"], None).records])
    monkeypatch.setattr(fr, "EXPECTED", {"glossary": 4, "expression_dictionary": n_reg + 2,
                                         "expression_dictionary_evidence": n_reg + 1, "category_master": 7, "category_mapping": 4})
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    store = MemoryStore()
    set_store(store)
    yield {"paths": paths, "store": store, "args": [x for k, p in paths.items() for x in (f"--{k}", p)]}
    set_store(None)


def _seed_old(url, tmp_path):
    from app.dictionary_ingest import main as dict_main
    from app.glossary_ingest import main as gl_main

    command.upgrade(_alembic(), "head")
    assert gl_main(["--ingredient", str(write_book(tmp_path / "old_i.xlsx", "ingredient", [base_row("ingredient", 9)])), "load"],
                   database_url=url) == 0
    assert dict_main(["load", "--local", str(write_local(tmp_path / "old_l.xlsx", [local_row(1), local_row(2)]))], database_url=url) == 0
    _q(url, "INSERT INTO category_master (name, is_leaf) VALUES ('Face', true)")
    seller = _q(url, "INSERT INTO seller (cognito_sub, email) VALUES ('local:t', 't@example.com') RETURNING id")[0][0]
    brand = _q(url, "INSERT INTO brand (seller_id, name_ko, name_en) VALUES (:s, 'b', 'b') RETURNING id", s=seller)[0][0]
    job = _q(url, "INSERT INTO job (seller_id, brand_id, product_name, target_country, regulatory_class, target_lang) "
                  "VALUES (:s, :b, 'p', 'US', 'cosmetic', 'en') RETURNING id", s=seller, b=brand)[0][0]
    sec = _q(url, "INSERT INTO section (job_id, source_image_id, section_order) VALUES (:j, 0, 1) RETURNING id", j=job)[0][0]
    did = _q(url, "SELECT min(id) FROM expression_dictionary")[0][0]
    _q(url, "INSERT INTO section_verdict (section_id, verdict_status, verdict_type, dictionary_id) VALUES (:s, 'regulated', 'regulatory', :d)",
       s=sec, d=did)
    return job, did


def _state(url):
    return {t: _q(url, f"SELECT count(*), md5(coalesce(string_agg(to_jsonb(x)::text, ',' ORDER BY x.id), '')) FROM {t} x")[0]
            for t in fr.TABLES + ("job", "section_verdict")}


def _spec_sha(store, run):
    return hashlib.sha256(store.get(fr._key(run, "spec"))).hexdigest()


@needs_db
def test_full_reload_apply_restore_and_stops(db_url, small, tmp_path):
    job, did = _seed_old(db_url, tmp_path)
    s0 = _state(db_url)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 0
    assert _state(db_url) == s0
    sha = _spec_sha(small["store"], "r1")
    apply = ["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", sha, *small["args"]]
    # 작업이 카테고리를 가리키면 쓰지 않는다
    _q(db_url, "UPDATE job SET display_category_id = (SELECT min(id) FROM category_master) WHERE id = :j", j=job)
    assert fr.main(apply, database_url=db_url) == 1
    _q(db_url, "UPDATE job SET display_category_id = NULL WHERE id = :j", j=job)
    assert _state(db_url) == s0
    # 다른 코드 SHA · 해시 불일치 명세는 쓰지 않는다
    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "other", "--spec-sha", sha, *small["args"]], database_url=db_url) == 1
    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", "0" * 64, *small["args"]], database_url=db_url) == 1
    assert _state(db_url) == s0
    assert fr.main(apply, database_url=db_url) == 0
    s1 = _state(db_url)
    assert s1["job"] == s0["job"] and s1["section_verdict"] == s0["section_verdict"]
    assert {r[0] for r in _q(db_url, "SELECT external_id FROM expression_dictionary")} >= {"LC-01", "CH-01"}
    assert not _q(db_url, "SELECT 1 FROM expression_dictionary WHERE external_id IN ('LC-02', 'CH-08')")  # 옛 행 · B 행 없음
    assert _q(db_url, "SELECT verdict_status, site_key, judgment_basis FROM expression_dictionary WHERE external_id = 'CH-01'")[0] == (
        "policy", "amazon", "근거 CH-01")
    assert not _q(db_url, "SELECT 1 FROM expression_dictionary WHERE id = :d", d=did)  # 판정이 가리키던 PK 재사용 없음
    assert _q(db_url, "SELECT min(id) FROM expression_dictionary")[0][0] > s0["expression_dictionary"][0]
    assert fr.main(apply, database_url=db_url) == 0 and _state(db_url) == s1  # 반복 — 무변경
    # 이미 최종본이면 새 plan 을 만들지 않는다
    assert fr.main(["plan", "--run-id", "r2", "--code-sha", "c", *small["args"]], database_url=db_url) == 1
    assert fr.main(["restore", "--run-id", "r1", "--spec-sha", sha], database_url=db_url) == 0
    s2 = _state(db_url)
    assert all(s2[t] == s0[t] for t in fr.TABLES)  # 원래 PK 그대로
    assert _q(db_url, "SELECT 1 FROM expression_dictionary WHERE id = :d", d=did)  # 판정이 가리키던 PK 복구
    assert fr.main(apply, database_url=db_url) == 1 and _state(db_url) == s2  # 복구한 실행은 다시 적용하지 않음


@needs_db
def test_full_reload_rolls_back_when_a_step_fails(db_url, small, tmp_path, monkeypatch):
    _seed_old(db_url, tmp_path)
    s0 = _state(db_url)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 0
    sha = _spec_sha(small["store"], "r1")

    def boom(conn, cf):
        raise RuntimeError("주입")

    monkeypatch.setattr(category_load, "apply", boom)
    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", sha, *small["args"]], database_url=db_url) == 1
    assert _state(db_url) == s0
    assert not _q(db_url, "SELECT 1 FROM audit_log WHERE action_type LIKE 'dictionary_full%'")


@needs_db
def test_full_reload_stops_when_rows_change_after_plan(db_url, small, tmp_path):
    _seed_old(db_url, tmp_path)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 0
    sha = _spec_sha(small["store"], "r1")
    _q(db_url, "UPDATE glossary SET note = 'changed' WHERE id = (SELECT min(id) FROM glossary)")
    s = _state(db_url)
    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", sha, *small["args"]], database_url=db_url) == 1
    assert _state(db_url) == s


@needs_db
def test_status_checks_spec_target_audit_and_db(db_url, small, tmp_path, monkeypatch, capsys):
    """status 는 명세 해시 · 접속 대상(DB · 버킷) · 감사 기록의 명세 해시를 먼저 대조하고, 기록과 DB 값이 맞을 때만 0 을 돌려준다."""
    from sqlalchemy.engine import make_url

    _seed_old(db_url, tmp_path)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 0
    sha = _spec_sha(small["store"], "r1")
    status = ["status", "--run-id", "r1", "--spec-sha", sha]

    def run(argv=status, url=db_url):
        capsys.readouterr()
        rc = fr.main(argv, database_url=url)
        return rc, capsys.readouterr().out

    rc, out = run()
    assert rc == 0 and "status not_applied · 현재 값이 적용 전과 같음 True" in out
    # 명세 해시 · 버킷 · 접속 대상이 다르면 판정하지 않는다
    assert run(["status", "--run-id", "r1", "--spec-sha", "0" * 64])[0] == 1
    u = make_url(db_url)
    other_host = u.set(host="127.0.0.1" if u.host != "127.0.0.1" else "localhost").render_as_string(hide_password=False)
    rc, out = run(url=other_host)
    assert rc == 1 and "대상이 다름" in out
    monkeypatch.setenv("S3_BUCKET", "other-bucket")
    rc, out = run()
    assert rc == 1 and "대상이 다름" in out
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    # 적용 전인데 DB 가 명세와 다르면 not_applied 로 단정하지 않는다
    _q(db_url, "UPDATE glossary SET note = 'x' WHERE id = (SELECT min(id) FROM glossary)")
    rc, out = run()
    assert rc == 3 and "같음 False" in out
    _q(db_url, "UPDATE glossary SET note = NULL WHERE id = (SELECT min(id) FROM glossary)")
    assert run()[0] == 0

    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", sha, *small["args"]], database_url=db_url) == 0
    rc, out = run()
    assert rc == 0 and "status applied · 현재 값이 적용 결과와 같음 True" in out
    # 적용 뒤 값이 바뀌면 3
    old = _q(db_url, "SELECT note FROM glossary WHERE id = (SELECT min(id) FROM glossary)")[0][0]
    _q(db_url, "UPDATE glossary SET note = 'changed' WHERE id = (SELECT min(id) FROM glossary)")
    rc, out = run()
    assert rc == 3 and "status applied · 현재 값이 적용 결과와 같음 False" in out
    _q(db_url, "UPDATE glossary SET note = :n WHERE id = (SELECT min(id) FROM glossary)", n=old)
    assert run()[0] == 0
    # 감사 기록의 명세 해시가 승인값과 다르면 3
    _q(db_url, "UPDATE audit_log SET detail = jsonb_set(detail, '{spec_sha256}', to_jsonb(:s ::text)) "
               "WHERE action_type = 'dictionary_full_reload_applied'", s="f" * 64)
    rc, out = run()
    assert rc == 3 and "감사 기록의 명세 해시" in out
    _q(db_url, "UPDATE audit_log SET detail = jsonb_set(detail, '{spec_sha256}', to_jsonb(:s ::text)) "
               "WHERE action_type = 'dictionary_full_reload_applied'", s=sha)
    assert fr.main(["restore", "--run-id", "r1", "--spec-sha", sha], database_url=db_url) == 0
    rc, out = run()
    assert rc == 0 and "status restored · 현재 값이 적용 전과 같음 True" in out
    # status 는 읽기만 한다
    assert _q(db_url, "SELECT count(*) FROM audit_log WHERE action_type LIKE 'dictionary_full_reload_%'")[0][0] == 2


def test_rejects_other_files(tmp_path, small, monkeypatch):
    monkeypatch.setattr(fr, "FINAL_FILES", {**fr.FINAL_FILES, "local": "0" * 64})
    with pytest.raises(fr.Stop):
        fr.Inputs(small["paths"])


# ── 실제 최종본 ──────────────────────────────────────────────────────────
FINAL_NAMES = {"certification": "용어집_인증_v5_적재본.xlsx", "ingredient": "용어집_성분_v3.1_적재용.xlsx",
               "phrase": "용어집_문구_v5_552행_2026-11-07.xlsx", "regulatory": "규제사전_v12_시드_2026-10-30_최종.xlsx",
               "local": "현지부적합_v9.xlsx", "category": "category_master_v3_2026-11-07.xlsx"}


def _final_paths():
    d = os.getenv("FINAL_DICT_DIR")
    if not d:
        return None
    paths = {k: os.path.join(d, n) for k, n in FINAL_NAMES.items()}
    return paths if all(os.path.exists(p) for p in paths.values()) else None


@pytest.mark.skipif(_final_paths() is None, reason="FINAL_DICT_DIR 에 최종 파일 없음")
def test_real_final_files_match_pinned_counts():
    inp = fr.Inputs(_final_paths())
    local = [f for f in inp.dvr.files if f.kind == "local"][0]
    assert sorted(r["external_id"] for r in local.records) == sorted(fr.V9_LOAD_IDS)
    assert sorted(i for i, _ in local.not_loaded) == ["CH-08", "CH-09", "CH-10"]
    assert len(inp.cf.nodes) == 200 and len(inp.cf.leaves) == 148


@needs_db
def test_plan_stops_when_a_job_points_to_a_category(db_url, small, tmp_path):
    job, _ = _seed_old(db_url, tmp_path)
    _q(db_url, "UPDATE job SET display_category_id = (SELECT min(id) FROM category_master) WHERE id = :j", j=job)
    s = _state(db_url)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 1
    assert not small["store"].keys("seed/dictionary-full-reload/")  # 명세도 만들지 않는다
    assert _state(db_url) == s


@needs_db
@pytest.mark.parametrize("table", ["glossary", "expression_dictionary"])
def test_apply_stops_if_new_pk_would_reuse_old_pk(db_url, small, tmp_path, table):
    """시퀀스가 되돌려진 DB(예: 잘못된 복원)에서는 새 PK 가 옛 PK 와 겹친다 — 커밋하지 않는다."""
    _seed_old(db_url, tmp_path)
    assert fr.main(["plan", "--run-id", "r1", "--code-sha", "c", *small["args"]], database_url=db_url) == 0
    sha = _spec_sha(small["store"], "r1")
    _q(db_url, f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 1, false)")
    s = _state(db_url)
    assert fr.main(["apply", "--run-id", "r1", "--code-sha", "c", "--spec-sha", sha, *small["args"]], database_url=db_url) == 1
    assert _state(db_url) == s
    assert not _q(db_url, "SELECT 1 FROM audit_log WHERE action_type LIKE 'dictionary_full%'")
