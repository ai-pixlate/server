"""규제사전·현지부적합 마이그레이션(0006)·적재 — 격리된 테스트 DB에서만 실행.

용어집 DB 테스트와 같은 GLOSSARY_TEST_DATABASE_URL(CREATE DATABASE 권한이 있는 접속)이 있을 때만 돌고,
테스트마다 전용 DB를 만들고 지운다. DATABASE_URL 만 설정된 경우에는 실행하지 않는다.
운영 주소인지 자동으로 판별하지 않으므로, 이 환경변수에 운영 DB 를 지정하지 않는다.
"""
import pytest
from alembic import command

from app.dictionary_ingest import main
from tests.dictionary_fixtures import basic_regulatory, ev_row, local_row, reg_row, write_local, write_regulatory
from tests.test_dictionary_ingest import CORRECTED, _delivery
from tests.test_glossary_db import ADMIN_URL, _alembic, _q, _revision, db_url  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="GLOSSARY_TEST_DATABASE_URL 없음")


def _insert_dict(url, ext, dict_type="regulatory", status="regulated", expr="표현", cls="cosmetic"):
    return _q(url, "INSERT INTO expression_dictionary (external_id, source_expression, target_country, "
                   "regulatory_class, dict_type, verdict_status) VALUES (:e, :x, 'US', :c, :t, :s) RETURNING id",
              e=ext, x=expr, c=cls, t=dict_type, s=status)[0][0]


# ── 마이그레이션 ─────────────────────────────────────────────────────────

def test_upgrade_adds_constraints_and_columns(db_url):
    command.upgrade(_alembic(), "head")
    assert _revision(db_url) == "0006"
    cons = {r[0] for r in _q(db_url, "SELECT conname FROM pg_constraint WHERE conrelid IN "
                                     "('expression_dictionary'::regclass, 'expression_dictionary_evidence'::regclass)")}
    assert {"uq_expr_dict_external_id", "uq_expr_dict_content", "ck_expr_dict_dict_type",
            "ck_expr_dict_verdict_status", "uq_expr_evidence_link"} <= cons
    idx = {r[0] for r in _q(db_url, "SELECT indexname FROM pg_indexes WHERE tablename='expression_dictionary_evidence'")}
    assert "uq_expr_evidence_primary" in idx
    # dict_type별 판정 CHECK
    with pytest.raises(Exception):
        _insert_dict(db_url, "RG-900", status="irrelevant")
    with pytest.raises(Exception):
        _insert_dict(db_url, "LC-90", dict_type="local", status="regulated")
    assert _insert_dict(db_url, "LC-91", dict_type="local", status="cultural", cls="common")


@pytest.mark.parametrize("setup,needle", [
    ("dup_id", "external_id 중복"),
    ("dup_content", "중복"),
    ("bad_verdict", "허용값 밖"),
    ("two_primaries", "대표 근거가 2건 이상"),
])
def test_upgrade_stops_on_existing_violations(db_url, setup, needle):
    command.upgrade(_alembic(), "0005")
    if setup == "dup_id":
        _insert_dict(db_url, "RG-001", expr="a")
        _insert_dict(db_url, "RG-001", expr="b")
    elif setup == "dup_content":
        _insert_dict(db_url, "RG-001")
        _insert_dict(db_url, "RG-002")
    elif setup == "bad_verdict":
        _insert_dict(db_url, "RG-001", status="rewritable")
    else:
        d = _insert_dict(db_url, "RG-001")
        for _ in range(2):
            _q(db_url, "INSERT INTO expression_dictionary_evidence (dictionary_id, is_primary) VALUES (:d, true)", d=d)
    with pytest.raises(Exception) as e:
        command.upgrade(_alembic(), "0006")
    assert needle in str(e.value)
    assert _revision(db_url) == "0005"


def test_precheck_matches_unique_null_semantics(db_url):
    # source_expression 이 NULL 인 행 둘은 UNIQUE 위반이 아니므로 사전 검사도 통과해야 한다
    command.upgrade(_alembic(), "0005")
    _insert_dict(db_url, "RG-001", expr=None)
    _insert_dict(db_url, "RG-002", expr=None)
    command.upgrade(_alembic(), "0006")
    assert _revision(db_url) == "0006"


def test_downgrade_to_0005(db_url):
    command.upgrade(_alembic(), "head")
    command.downgrade(_alembic(), "0005")
    assert _revision(db_url) == "0005"
    cols = {r[0] for r in _q(db_url, "SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name='expression_dictionary'")}
    assert "exclusion_context" not in cols


# ── 적재 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def head(db_url):
    command.upgrade(_alembic(), "head")
    return db_url


def _run(url, mode, reg=None, loc=None):
    args = [mode] + (["--regulatory", str(reg)] if reg else []) + (["--local", str(loc)] if loc else [])
    return main(args, database_url=url)


def _dict(url):
    return {r[0]: r[1:] for r in _q(
        url, "SELECT external_id, id, verdict_status, reason, source_verdict_status FROM expression_dictionary")}


def _evidence(url):
    return sorted(tuple(r) for r in _q(
        url, "SELECT d.external_id, v.external_id, v.is_primary, v.id, v.evidence_quote "
             "FROM expression_dictionary_evidence v JOIN expression_dictionary d ON d.id = v.dictionary_id"))


def test_load_twice_is_idempotent(head, tmp_path, capsys):
    reg = basic_regulatory(tmp_path / "r.xlsx")
    loc = write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2)])
    assert _run(head, "load", reg, loc) == 0
    out = capsys.readouterr().out
    assert "사전: 삽입 7" in out and "대표 근거: 삽입 5" in out
    before = (_dict(head), _evidence(head))
    first = [l for l in out.splitlines() if "PK 지문" in l]
    assert _run(head, "load", reg, loc) == 0
    out = capsys.readouterr().out
    assert "변경 없음 7" in out and "대표 근거: 삽입 0 · 갱신 0 · 변경 없음 5" in out
    assert (_dict(head), _evidence(head)) == before
    assert [l for l in out.splitlines() if "PK 지문" in l] == first
    # 공유 근거 MN-001 은 RG-003·RG-004 에 각각 연결, 현지 항목은 근거 없음
    assert [e[:3] for e in before[1] if e[1] == "MN-001"] == [("RG-003", "MN-001", True), ("RG-004", "MN-001", True)]
    assert not [e for e in before[1] if e[0].startswith("LC-")]


def test_plan_writes_nothing(head, tmp_path):
    assert _run(head, "plan", basic_regulatory(tmp_path / "r.xlsx")) == 0
    assert _dict(head) == {} and _evidence(head) == []


def test_primary_change_keeps_old_link_as_non_primary(head, tmp_path, capsys):
    ev = [ev_row("WL-001", "RG-001"), ev_row("WL-002", "RG-001", primary="N")]
    _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1, "WL-001")], ev))
    pk = _dict(head)["RG-001"][0]
    old = _evidence(head)[0]
    # 대표를 WL-002 로 교체
    ev2 = [ev_row("WL-001", "RG-001", primary="N"), ev_row("WL-002", "RG-001")]
    assert _run(head, "load", write_regulatory(tmp_path / "b.xlsx", [reg_row(1, "WL-002")], ev2)) == 0
    assert "대표 해제(비대표로 보존) 1" in capsys.readouterr().out
    rows = _evidence(head)
    assert [(r[1], r[2]) for r in rows] == [("WL-001", False), ("WL-002", True)]
    assert rows[0][3] == old[3] and _dict(head)["RG-001"][0] == pk  # 이전 연결 행·사전 PK 유지
    # 다시 WL-001 로 되돌리면 새 행 없이 기존 연결을 대표로
    assert _run(head, "load", write_regulatory(tmp_path / "c.xlsx", [reg_row(1, "WL-001")], ev)) == 0
    rows = _evidence(head)
    assert len(rows) == 2 and [(r[1], r[2]) for r in rows] == [("WL-001", True), ("WL-002", False)]
    assert rows[0][3] == old[3]


def test_evidence_content_change_updates_same_link(head, tmp_path):
    _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1)], [ev_row("WL-001", "RG-001")]))
    before = _evidence(head)[0]
    _run(head, "load", write_regulatory(tmp_path / "b.xlsx", [reg_row(1)],
                                        [ev_row("WL-001", "RG-001", quote="정정된 인용문")]))
    after = _evidence(head)
    assert len(after) == 1 and after[0][3] == before[3] and after[0][4] == "정정된 인용문"


def test_dictionary_update_keeps_pk(head, tmp_path):
    _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1)], [ev_row("WL-001", "RG-001")]))
    pk = _dict(head)["RG-001"][0]
    assert _run(head, "load", write_regulatory(tmp_path / "b.xlsx", [reg_row(1, reason="고친 사유", version="2")],
                                               [ev_row("WL-001", "RG-001")])) == 0
    assert _dict(head)["RG-001"] == (pk, "regulated", "고친 사유", "regulated")


def test_source_verdict_is_kept_and_its_change_is_an_update(head, tmp_path, capsys):
    reg = basic_regulatory(tmp_path / "r.xlsx")
    loc = write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2, 판정="needs_fix")])
    _run(head, "load", reg, loc)
    d = _dict(head)
    assert d["RG-002"][1:4:2] == ("regulated", "rewritable")  # 서비스 값 · 원값
    assert d["LC-02"][1:4:2] == ("needs_fix", "needs_fix")
    pk = d["RG-001"][0]
    capsys.readouterr()
    # 정규화 값(regulated)은 같고 원값만 regulated → rewritable 로 바뀐 경우도 갱신
    ev = [ev_row("WL-001", "RG-001")]
    assert _run(head, "load", write_regulatory(tmp_path / "b.xlsx", [reg_row(1, verdict_status="rewritable")], ev)) == 0
    assert "갱신 1" in capsys.readouterr().out
    assert _dict(head)["RG-001"] == (pk, "regulated", "사유 문장", "rewritable")


@pytest.mark.parametrize("row,needle", [
    (reg_row(1, regulatory_class="otc"), "번호 재사용"),
    (reg_row(2, source_expression="표현1"), "다른 항목 ID"),
])
def test_reload_policy_violations_write_nothing(head, tmp_path, capsys, row, needle):
    _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1)], [ev_row("WL-001", "RG-001")]))
    before = (_dict(head), _evidence(head))
    ev = [ev_row("WL-001", row["id"])]
    assert _run(head, "load", write_regulatory(tmp_path / "b.xlsx", [row], ev)) == 1
    assert needle in capsys.readouterr().out
    assert (_dict(head), _evidence(head)) == before


def test_failure_after_partial_writes_rolls_back_everything(head, tmp_path, capsys):
    # 1차 적재: RG-001 의 대표 근거 WL-001
    ev = [ev_row("WL-001", "RG-001"), ev_row("WL-002", "RG-001", primary="N")]
    _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1, "WL-001")], ev))
    before = (_dict(head), _evidence(head))
    # 근거 WL-002 삽입만 DB가 거부하도록(사전 갱신·기존 대표 해제가 먼저 실행된 뒤 실패)
    _q(head, "CREATE FUNCTION test_block_evidence() RETURNS trigger AS $$ "
             "BEGIN RAISE EXCEPTION 'test: evidence insert blocked'; END $$ LANGUAGE plpgsql")
    _q(head, "CREATE TRIGGER test_block BEFORE INSERT ON expression_dictionary_evidence FOR EACH ROW "
             "WHEN (NEW.external_id = 'WL-002') EXECUTE FUNCTION test_block_evidence()")
    # 2차: 사유 변경(사전 갱신) + 대표를 WL-002 로 교체(WL-001 해제 → WL-002 삽입에서 실패)
    ev2 = [ev_row("WL-001", "RG-001", primary="N"), ev_row("WL-002", "RG-001")]
    rows2 = [reg_row(1, "WL-002", reason="바뀐 사유"), reg_row(2, "WL-003")]
    ev2.append(ev_row("WL-003", "RG-002"))
    assert _run(head, "load", write_regulatory(tmp_path / "b.xlsx", rows2, ev2)) == 1
    out = capsys.readouterr().out
    assert "갱신 1" in out and "대표 해제(비대표로 보존) 1" in out  # 쓰기 계획이 실제로 있었음
    assert "test: evidence insert blocked" in out and "전체 롤백" in out
    # 사전 값·신규 항목·근거 행·기존 대표 상태가 모두 이전 그대로
    assert (_dict(head), _evidence(head)) == before
    assert [(r[1], r[2]) for r in _evidence(head)] == [("WL-001", True)]


def test_legacy_primary_without_source_id_stops(head, tmp_path, capsys):
    d = _insert_dict(head, "RG-001", expr="표현1")
    _q(head, "INSERT INTO expression_dictionary_evidence (dictionary_id, is_primary) VALUES (:d, true)", d=d)
    assert _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1)], [ev_row("WL-001", "RG-001")])) == 1
    assert "원본 근거 ID 없는 기존 대표 근거" in capsys.readouterr().out


def test_rows_missing_from_file_are_kept(head, tmp_path, capsys):
    loc = write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2)])
    _run(head, "load", None, loc)
    assert _run(head, "load", None, write_local(tmp_path / "l2.xlsx", [local_row(1)])) == 0
    assert "이번 파일에 없는 기존 행(유지) 1" in capsys.readouterr().out
    assert set(_dict(head)) == {"LC-01", "LC-02"}


@pytest.mark.skipif(_delivery(CORRECTED) is None, reason="DICT_XLSX_DIR 에 2026-09-30 정정본 없음")
def test_corrected_delivery_loads_twice(head, capsys):
    reg, loc = _delivery(CORRECTED)
    assert _run(head, "load", reg, loc) == 0
    before = (_dict(head), _evidence(head))
    assert _run(head, "load", reg, loc) == 0
    out = capsys.readouterr().out
    assert "사전: 삽입 0 · 갱신 0 · 변경 없음 24" in out and "대표 근거: 삽입 0 · 갱신 0 · 변경 없음 16" in out
    assert (_dict(head), _evidence(head)) == before
    prim = _q(head, "SELECT d.external_id, count(*) FROM expression_dictionary d JOIN expression_dictionary_evidence v "
                    "ON v.dictionary_id = d.id AND v.is_primary GROUP BY 1")
    assert len(prim) == 16 and all(n == 1 for _, n in prim)
    rw = _q(head, "SELECT external_id FROM expression_dictionary WHERE source_verdict_status = 'rewritable' ORDER BY 1")
    assert [r[0] for r in rw] == ["RG-008", "RG-030"]
    assert _q(head, "SELECT count(*) FROM expression_dictionary WHERE source_verdict_status IS NULL")[0][0] == 0
