"""규제사전·현지부적합 마이그레이션(0006 · 0009)·적재 — 격리된 테스트 DB에서만 실행.

용어집 DB 테스트와 같은 GLOSSARY_TEST_DATABASE_URL(CREATE DATABASE 권한이 있는 접속)이 있을 때만 돌고,
테스트마다 전용 DB를 만들고 지운다. DATABASE_URL 만 설정된 경우에는 실행하지 않는다.
운영 주소인지 자동으로 판별하지 않으므로, 이 환경변수에 운영 DB 를 지정하지 않는다.
"""
import pytest
from alembic import command

from app.dictionary_ingest import main
from tests.dictionary_fixtures import basic_regulatory, ev_row, local_row, reg_row, write_local, write_regulatory
from tests.test_dictionary_ingest import v12_delivery
from tests.test_glossary_db import ADMIN_URL, _alembic, _q, _revision, db_url  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="GLOSSARY_TEST_DATABASE_URL 없음")


def _insert_dict(url, ext, dict_type="regulatory", status="regulated", expr="표현", cls="cosmetic", key=None):
    """key 를 주면 monograph_key 도 넣는다(0009 이후). 0009 전 리비전에서는 key 없이 쓴다."""
    col, val = (", monograph_key", ", :k") if key else ("", "")
    return _q(url, f"INSERT INTO expression_dictionary (external_id, source_expression, target_country, "
                   f"regulatory_class, dict_type, verdict_status{col}) VALUES (:e, :x, 'US', :c, :t, :s{val}) "
                   f"RETURNING id", e=ext, x=expr, c=cls, t=dict_type, s=status, k=key)[0][0]


# ── 마이그레이션 ─────────────────────────────────────────────────────────

def test_upgrade_adds_constraints_and_columns(db_url):
    command.upgrade(_alembic(), "head")
    assert _revision(db_url) == "0009"
    cons = {r[0] for r in _q(db_url, "SELECT conname FROM pg_constraint WHERE conrelid IN "
                                     "('expression_dictionary'::regclass, 'expression_dictionary_evidence'::regclass)")}
    assert {"ck_expr_dict_dict_type", "ck_expr_dict_verdict_status", "uq_expr_evidence_link",
            "fk_expr_dict_monograph", "ck_expr_dict_monograph_key"} <= cons
    idx = {r[0] for r in _q(db_url, "SELECT indexname FROM pg_indexes WHERE tablename IN "
                                    "('expression_dictionary', 'expression_dictionary_evidence')")}
    assert {"uq_expr_evidence_primary", "uq_expr_dict_external_id", "uq_expr_dict_content"} <= idx
    # dict_type별 판정 CHECK
    with pytest.raises(Exception):
        _insert_dict(db_url, "RG-900", status="irrelevant")
    with pytest.raises(Exception):
        _insert_dict(db_url, "LC-90", dict_type="local", status="regulated")
    assert _insert_dict(db_url, "LC-91", dict_type="local", status="cultural", cls="common")


def test_0009_seeds_otc_master_matching_loader_keys(db_url):
    from app.dictionary_ingest import MONOGRAPH_KEYS
    command.upgrade(_alembic(), "0009")
    rows = _q(db_url, "SELECT country_code, monograph_key, name_ko, name_en_short FROM master_otc_monograph "
                      "ORDER BY display_order")
    assert [(c, k) for c, k, *_ in rows] == [("US", k) for k in MONOGRAPH_KEYS["US"]]
    assert rows[2][2:] == ("비듬·지루성 피부염·건선", "Dandruff · Seborrheic Dermatitis · Psoriasis")


@pytest.mark.parametrize("cls,key,ok", [
    ("common", None, True), ("cosmetic", "M006", True), ("otc", "M020", True),
    ("common", "M020", False),     # common 은 열쇠를 비운다
    ("cosmetic", None, False),     # cosmetic · otc 는 열쇠 필수
    ("otc", None, False),
    ("otc", "M999", False),        # 마스터에 없는 열쇠(외래키)
])
def test_0009_key_rule_for_new_rows(db_url, cls, key, ok):
    command.upgrade(_alembic(), "0009")
    if ok:
        assert _insert_dict(db_url, "RG-001", cls=cls, key=key)
    else:
        with pytest.raises(Exception):
            _insert_dict(db_url, "RG-001", cls=cls, key=key)


def test_0009_keeps_existing_rows_without_key(db_url):
    # 현재 적재본처럼 열쇠 없는 cosmetic 행이 있어도 업그레이드는 멈추지 않는다(NOT VALID — 기존 행 일괄 검사만 미룸)
    command.upgrade(_alembic(), "0008")
    _insert_dict(db_url, "RG-008", expr="주름")
    command.upgrade(_alembic(), "0009")
    assert _q(db_url, "SELECT monograph_key, retired_at FROM expression_dictionary")[0] == (None, None)


def test_0009_active_unique_allows_retired_duplicate_and_downgrade_stops(db_url):
    command.upgrade(_alembic(), "0009")
    old = _insert_dict(db_url, "RG-001", expr="표현1", key="M006")
    with pytest.raises(Exception):  # 살아 있는 같은 항목 ID 는 하나
        _insert_dict(db_url, "RG-001", expr="표현1b", cls="common")
    _q(db_url, "UPDATE expression_dictionary SET retired_at = now() WHERE id = :i", i=old)
    assert _insert_dict(db_url, "RG-001", expr="표현1", cls="common")  # 빠진 행과 같은 ID · 내용 키 허용
    with pytest.raises(Exception) as e:
        command.downgrade(_alembic(), "0008")
    assert "교체로 빠진 사전 행" in str(e.value)
    assert _revision(db_url) == "0009"


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
    d = _insert_dict(head, "RG-001", expr="표현1", key="M006")
    _q(head, "INSERT INTO expression_dictionary_evidence (dictionary_id, is_primary) VALUES (:d, true)", d=d)
    assert _run(head, "load", write_regulatory(tmp_path / "a.xlsx", [reg_row(1)], [ev_row("WL-001", "RG-001")])) == 1
    assert "원본 근거 ID 없는 기존 대표 근거" in capsys.readouterr().out


def test_rows_missing_from_file_are_kept(head, tmp_path, capsys):
    loc = write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2)])
    _run(head, "load", None, loc)
    assert _run(head, "load", None, write_local(tmp_path / "l2.xlsx", [local_row(1)])) == 0
    assert "이번 파일에 없는 기존 행(유지) 1" in capsys.readouterr().out
    assert set(_dict(head)) == {"LC-01", "LC-02"}


def test_key_missing_from_db_master_is_rejected(head, tmp_path, capsys):
    _q(head, "DELETE FROM master_otc_monograph WHERE monograph_key = 'M016'")
    row = reg_row(1, monograph_key="M016")
    assert _run(head, "plan", write_regulatory(tmp_path / "a.xlsx", [row], [ev_row("WL-001", "RG-001")])) == 1
    assert "DB OTC 종류 마스터에 없는 열쇠" in capsys.readouterr().out


# ── 교체(--replace) ──────────────────────────────────────────────────────

def _state(url):
    return sorted(tuple(r) for r in _q(
        url, "SELECT external_id, id, regulatory_class, monograph_key, retired_at IS NOT NULL "
             "FROM expression_dictionary"))


def _replace_files(tmp_path):
    v1 = write_regulatory(tmp_path / "v1.xlsx", [reg_row(1, "WL-001"), reg_row(2, "WL-002"),
                                                 reg_row(3, "MN-001", regulatory_class="otc")],
                          [ev_row("WL-001", "RG-001"), ev_row("WL-002", "RG-002"), ev_row("MN-001", "RG-003")])
    # RG-001 같은 번호로 cosmetic → common(RG-008 등과 같은 경우), RG-002 빠짐(RG-030 과 같은 경우)
    v2 = write_regulatory(tmp_path / "v2.xlsx", [reg_row(1, "WL-001", regulatory_class="common"),
                                                 reg_row(3, "MN-001", regulatory_class="otc")],
                          [ev_row("WL-001", "RG-001"), ev_row("MN-001", "RG-003")])
    return v1, v2


def test_replace_retires_reclassed_and_missing_rows(head, tmp_path, capsys):
    v1, v2 = _replace_files(tmp_path)
    loc = write_local(tmp_path / "l.xlsx", [local_row(1)])
    assert _run(head, "load", v1, loc) == 0
    before = _state(head)
    old_pk = {r[0]: r[1] for r in before}
    capsys.readouterr()
    # 보통 적재는 분류 변경을 거부하고 --replace 를 안내한다
    assert _run(head, "load", v2) == 1
    assert "규제사전 교체는 --replace" in capsys.readouterr().out
    # plan --replace: 빠질 행만 보여 주고 쓰지 않는다
    assert main(["plan", "--replace", "--regulatory", str(v2)], database_url=head) == 0
    out = capsys.readouterr().out
    assert "교체로 빠짐(retired_at, 행 보존) 2: RG-001(분류 변경 cosmetic → common), RG-002(파일에 없음)" in out
    assert _state(head) == before
    # load --replace
    assert main(["load", "--replace", "--regulatory", str(v2)], database_url=head) == 0
    out = capsys.readouterr().out
    assert "교체로 빠짐 2" in out and "사전 삽입 1" in out
    rows = _state(head)
    active = {r[0]: r for r in rows if not r[4]}
    retired = {r[0]: r for r in rows if r[4]}
    assert set(active) == {"RG-001", "RG-003", "LC-01"}  # 현지 행은 교체 대상 아님
    assert active["RG-001"][2:4] == ("common", None) and active["RG-001"][1] != old_pk["RG-001"]
    assert active["RG-003"][1] == old_pk["RG-003"]  # 바뀌지 않은 행은 PK 유지
    assert {k: v[1] for k, v in retired.items()} == {"RG-001": old_pk["RG-001"], "RG-002": old_pk["RG-002"]}
    # 빠진 행의 근거 연결은 그대로 남고, 새 RG-001 에는 대표 근거가 새로 연결된다
    links = _q(head, "SELECT d.id, v.external_id, v.is_primary FROM expression_dictionary_evidence v "
                     "JOIN expression_dictionary d ON d.id = v.dictionary_id WHERE d.external_id IN ('RG-001', 'RG-002')")
    assert sorted(links) == sorted([(old_pk["RG-001"], "WL-001", True), (old_pk["RG-002"], "WL-002", True),
                                    (active["RG-001"][1], "WL-001", True)])
    # 다시 돌리면 바뀌는 것 없음
    assert main(["load", "--replace", "--regulatory", str(v2)], database_url=head) == 0
    out = capsys.readouterr().out
    assert "교체로 빠짐(retired_at, 행 보존) 0" in out and "변경 없음 2" in out
    assert _state(head) == rows
    # 살아 있는 규제 행이 모두 새로 쓴 행이면 열쇠 CHECK 전체 검사(VALIDATE)도 통과한다
    _q(head, "ALTER TABLE expression_dictionary VALIDATE CONSTRAINT ck_expr_dict_monograph_key")


def test_replace_failure_rolls_back_retirement(head, tmp_path, capsys):
    v1, v2 = _replace_files(tmp_path)
    _run(head, "load", v1)
    before = _state(head)
    _q(head, "CREATE FUNCTION test_block_dict() RETURNS trigger AS $$ "
             "BEGIN RAISE EXCEPTION 'test: dictionary insert blocked'; END $$ LANGUAGE plpgsql")
    _q(head, "CREATE TRIGGER test_block BEFORE INSERT ON expression_dictionary FOR EACH ROW "
             "EXECUTE FUNCTION test_block_dict()")
    assert main(["load", "--replace", "--regulatory", str(v2)], database_url=head) == 1
    assert "전체 롤백" in capsys.readouterr().out
    assert _state(head) == before  # 빠짐 표시(retired_at)도 함께 롤백


@pytest.mark.skipif(v12_delivery() is None, reason="DICT_XLSX_DIR 에 v12 최종 시드 없음")
def test_v12_final_replace_over_0922_like_rows(head, capsys):
    # 현재 적재본처럼 열쇠 없는 cosmetic 행(RG-008 · RG-030)이 있는 DB를 v12 로 교체한다
    # — 빠짐 표시 UPDATE 가 NOT VALID 열쇠 CHECK 를 통과하는지도 여기서 본다
    command.downgrade(_alembic(), "0008")
    _insert_dict(head, "RG-008", expr="주름")
    _insert_dict(head, "RG-030", expr="모공")
    command.upgrade(_alembic(), "head")
    reg = v12_delivery()
    assert main(["plan", "--replace", "--regulatory", reg], database_url=head) == 0
    out = capsys.readouterr().out
    assert "교체로 빠짐(retired_at, 행 보존) 2: RG-008(분류 변경 cosmetic → common), RG-030(파일에 없음)" in out
    assert main(["load", "--replace", "--regulatory", reg], database_url=head) == 0
    assert "교체로 빠짐 2 · 사전 삽입 26" in capsys.readouterr().out
    active = _q(head, "SELECT count(*), count(*) FILTER (WHERE monograph_key IS NOT NULL) FROM expression_dictionary "
                      "WHERE retired_at IS NULL")
    assert active == [(26, 15)]
    prim = _q(head, "SELECT d.external_id, count(*) FROM expression_dictionary d JOIN expression_dictionary_evidence v "
                    "ON v.dictionary_id = d.id AND v.is_primary WHERE d.retired_at IS NULL GROUP BY 1")
    assert len(prim) == 26 and all(n == 1 for _, n in prim)
    assert main(["load", "--replace", "--regulatory", reg], database_url=head) == 0
    out = capsys.readouterr().out
    assert "교체로 빠짐(retired_at, 행 보존) 0" in out and "변경 없음 26" in out
    _q(head, "ALTER TABLE expression_dictionary VALIDATE CONSTRAINT ck_expr_dict_monograph_key")
