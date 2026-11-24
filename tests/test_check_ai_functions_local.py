"""실제 AI 함수의 로컬 시험(격리 통합 11-10) — BE 연결 시험이 아니다.

- A1 `pipeline.handoff.translation.text_check_input_keys` 를 BE 가 만든 검사 요청(text_check.build_request)에 그대로 불러 본다.
  BE 는 아직 이 함수를 공급자로 연결하지 않았다(기본 공급자는 미연결 — 켜면 CheckConfigError). 여기서 보는 것은 형식 호환뿐이다.
- 용어집 선별의 term_kind: 인증 ↔ 문구 같은 구간은 term_kind 를 넘기면 인증 우선 제외(certification_over_phrase)로 풀리고,
  넘기지 않으면 GlossaryDataError 다. BE 번역 공급(select_glossary)은 11-10 부터 행의 term_kind 를 그대로 넘긴다(없으면 None).
  검사 공급(M3)은 정책 미정 · 미연결이라 BE 경로로 확인할 수 없다 — 시험 대역(FakeSupply)은 번역 공급을 그대로 쓴다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.flows import downstream, text_check
from pipeline import glossary as gl
from pipeline.handoff import translation
from tests.exec_db import requires_db
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401
from tests.test_text_check_connect import rule_on  # noqa: F401


# ── A1 실제 함수 × BE 요청 ────────────────────────────────────────────────
@requires_db
def test_real_input_keys_accept_be_request_and_ignore_layout(env, rule_on):
    from app import db as app_db

    ids = _to_n3(env)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    s = app_db.SessionLocal()
    try:
        run = dict(s.execute(text("SELECT * FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream'"),
                             {"j": ids["job"]}).mappings().one())
        sid = _q("SELECT unit_id FROM job_async_task WHERE job_id = :j AND stage = 'text_check' AND is_current ORDER BY id LIMIT 1",
                 j=ids["job"])[0]["unit_id"]

        def real_keys():
            rows = downstream._block_rows(s, sid)
            targets = text_check.checkable(rows)
            g, _ = text_check.split_supply(rule_on.glossary(s, run, sid, targets, origin="test"))
            return targets, translation.text_check_input_keys(text_check.build_request(s, run, sid, rows, g))

        targets, k1 = real_keys()
        assert set(k1) == {f"blk_{t['id']}" for t in targets} and all(isinstance(v, str) and v for v in k1.values())
        assert real_keys()[1] == k1  # 결정적
        b = targets[0]["id"]
        _q("UPDATE text_block SET revision = revision + 1, bbox = jsonb_set(bbox, '{x}', to_jsonb((bbox->>'x')::int + 3)) WHERE id = :b", b=b)
        s.expire_all()
        assert real_keys()[1] == k1  # 배치 · revision 은 키 재료가 아니다
        _q("UPDATE text_block SET trans_1 = trans_1 || ' x' WHERE id = :b", b=b)
        s.expire_all()
        k3 = real_keys()[1]
        assert k3[f"blk_{b}"] != k1[f"blk_{b}"] and {k: v for k, v in k3.items() if k != f"blk_{b}"} == {k: v for k, v in k1.items() if k != f"blk_{b}"}
    finally:
        s.close()


def test_default_providers_not_connected(monkeypatch):
    text_check.set_providers()
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    with pytest.raises(text_check.CheckConfigError):  # 실제 공급(M3) · A1 공급자 미연결 — 켤 수 없다
        text_check.configured_active_checks()
    with pytest.raises(text_check.SupplyBasisUnresolved):
        text_check._supply.glossary(None, {}, 0, [], origin="test")


# ── term_kind: 인증 ↔ 문구 같은 구간 ────────────────────────────────────────
CERT = ("10", "비건", "Vegan", "enforced", "certification")
PHRASE = ("20", "비건", "vegan-friendly", "reference", gl.PHRASE_KIND)  # 문구 용어집 term_kind = marketing
SRC = [("blk_1", "이 제품은 비건 제품입니다")]


def test_selection_with_term_kind_resolves_same_span_by_certification():
    sel = gl.select_by_block(SRC, [gl.Entry(*CERT), gl.Entry(*PHRASE)])
    assert sel.blocks[0].glossary_ids == ("10",)
    assert [(x.reference_id, x.enforced_ids, x.rule) for x in sel.exclusions] == [("20", ("10",), "certification_over_phrase")]


def test_selection_without_term_kind_fails_same_span():
    with pytest.raises(gl.GlossaryDataError, match="정규화 후 같은 원문의 서로 다른 용어"):
        gl.select_by_block(SRC, [gl.Entry(*CERT[:4]), gl.Entry(*PHRASE[:4])])


def test_be_translation_supply_passes_term_kind_from_row():
    rows = [{"id": int(e[0]), "term_ko": e[1], "term_target": e[2], "enforcement": e[3], "internal_category": "G05", "version": 1,
             "term_kind": e[4]} for e in (CERT, PHRASE)]
    out = downstream.select_glossary(rows, [{"id": 1, "source_ko": SRC[0][1]}], {"rules": "t", "categories": ["G05", "common"]})
    assert out["status"] == "ok" and [t["glossary_id"] for t in out["terms"]] == ["10"]  # 인증 우선 · 같은 구간 문구 제외
    no_kind = [{k: v for k, v in r.items() if k != "term_kind"} for r in rows]  # 행에 term_kind 가 없으면 None — 예외 미적용
    out = downstream.select_glossary(no_kind, [{"id": 1, "source_ko": SRC[0][1]}], {"rules": "t", "categories": ["G05", "common"]})
    assert out["status"] == "failed" and "정규화 후 같은 원문의 서로 다른 용어" in out["reason"]
