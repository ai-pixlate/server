"""③-1 채널 정책 맥락 판정 — 운영 인계 계약 테스트(합성 묶음 · 가짜 모델. 실제 채널 사전 · 모델 · BE 연결 아님).

채널 항목 대응표와 정책 규칙의 채널 행은 저장소 운영 파일에 아직 없다(BE 연결 전에 채운다). 여기서는 합성 대응표를
끼워 넣고 합성 묶음의 rules에 채널 행을 넣어 판정 동작만 고정한다."""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from pipeline.handoff import bundle as bundle_mod, examples as ex, judgment, validate as validate_mod
from pipeline.handoff.bundle import BundleError, LocalType, bundle_content_sha256, check_bundle
from pipeline.vlm import LlmReply, VlmError

CHANNEL_RULE = {"dict_type": "channel", "verdict_status": "policy", "alternative": "any", "emit_verdict_status": "policy", "bucket": "exclude"}
CHANNEL_TYPES = (LocalType("channel_synthetic_price", "CH-901", False), LocalType("channel_synthetic_review", "CH-902", False))


def _row(eid: str, pattern: str) -> dict:
    return {"external_id": eid, "item": f"합성 채널 항목 {eid}", "patterns": [pattern], "verdict_status": "policy",
            "exclusion_context": f"합성 채널 제외 맥락 {eid}", "keep_context": f"합성 채널 유지 맥락 {eid}",
            "seller_message": f"합성 채널 셀러 안내 {eid}", "confirmed_date": None}


def channel_bundle(*, rows: bool = True, unavailable: bool = False, rule: bool = True) -> dict:
    """합성 묶음 + 채널 부분. sec_1_01의 「가격 3만원」에 CH-901, sec_1_02의 「연구원 추천」에 CH-902 패턴이 걸린다."""
    b = ex.bundle()
    if rows:
        b["channel"] = {"site_key": "amazon", "version": "channel@synthetic.1", "entries": [_row("CH-901", "가격"), _row("CH-902", "추천")]}
    if unavailable:
        b["channel_unavailable"] = {"reason": "합성: 채널 사전 조회 실패"}
    if rule:
        b["rules"] = copy.deepcopy(b["rules"])
        b["rules"]["verdict_map"].append(dict(CHANNEL_RULE))
    b["bundle_sha256"] = bundle_content_sha256(b)
    return b


@pytest.fixture()
def types(monkeypatch):
    """합성 채널 대응표를 판정과 검증에 끼운다(운영 대응표 파일은 건드리지 않는다)."""
    tm = dataclasses.replace(bundle_mod.load_type_map(), channel=CHANNEL_TYPES)
    monkeypatch.setattr(judgment, "load_type_map", lambda: tm)
    monkeypatch.setattr(validate_mod, "load_type_map", lambda: tm)
    return tm


@pytest.fixture()
def base_req(tmp_path):
    return next(r for n, r, _ in ex.cases(Path(tmp_path).resolve() / "ch") if n == "judge.completed")


class ScriptedJudge:
    """현지 호출은 ex.FakeJudge 그대로. 채널 호출(항목 ID가 CH-)만 mode대로 답한다. 받은 페이로드를 기록한다."""

    config = {"model": "fake", "temperature": 0, "timeout_s": 1}

    def __init__(self, channel: str = "candidates", local_fails: bool = False):
        self.channel, self.local_fails, self.payloads = channel, local_fails, []
        self._local = ex.FakeJudge()

    def __call__(self, prompt, payload, image):
        p = json.loads(payload)
        self.payloads.append(p)
        if not p["items"][0]["id"].startswith("CH-"):
            if self.local_fails:
                raise VlmError("합성: 현지 호출 실패")
            return self._local(prompt, payload, image)
        if self.channel == "call_fails":
            raise VlmError("합성: 채널 호출 시간 초과")
        if self.channel == "missing_item":
            return LlmReply(text=json.dumps({"items": []}), usage=None)
        items = []
        for it in p["items"]:
            if it["candidate_blocks"]:
                st = "uncertain" if self.channel == "uncertain" else "present"
                items.append({"id": it["id"], "status": st, "evidence_source": "text", "evidence": it["candidate_blocks"],
                              "reason": "합성: 후보 블록"})
            else:
                items.append({"id": it["id"], "status": "absent", "evidence_source": "image", "evidence": [], "reason": "합성: 해당 없음"})
        return LlmReply(text=json.dumps({"items": items}, ensure_ascii=False), usage=None)


def _run(req: dict, llm) -> tuple:
    rep = judgment.run_judgment(req, ex.config(), llm=llm)
    return rep, validate_mod.validate_report(rep, req)


def _sections(rep) -> dict:
    return {s["section_key"]: s for s in rep.payload["sections"]}


# ---------------------------------------------------------------------------
# 채널 부분이 없는 묶음 — 이전과 같다
# ---------------------------------------------------------------------------
def test_without_channel_part_report_has_no_channel_keys(base_req):
    rep, problems = _run(base_req, ex.FakeJudge())
    assert problems == []
    assert "channel_failed" not in rep.payload["summary"]
    assert "channel_site_key" not in rep.payload["bundle"]
    for s in rep.payload["sections"]:
        assert "channel_status" not in s and "channel" not in s["audit"]["inspection"]


# ---------------------------------------------------------------------------
# 채널 판정
# ---------------------------------------------------------------------------
def test_channel_items_are_judged_in_every_section_one_finding_each(base_req, types):
    llm = ScriptedJudge()
    req = {**base_req, "bundle": channel_bundle()}
    rep, problems = _run(req, llm)
    assert problems == [] and rep.outcome == "completed"
    assert rep.payload["bundle"]["channel_site_key"] == "amazon"
    secs = _sections(rep)
    for s in secs.values():
        ct = [f["content_type"] for f in s["content_findings"]["findings"] if f["content_type"].startswith("channel_")]
        assert sorted(ct) == ["channel_synthetic_price", "channel_synthetic_review"]  # 섹션 × 항목 1건 — 후보가 없는 항목도 판정
        assert s["channel_status"] == "completed" and s["channel_failure"] is None
        assert s["audit"]["inspection"]["channel"] == {"status": "completed", "site_key": "amazon", "items_checked": ["CH-901", "CH-902"],
                                                        "failure": None}
        assert sum(f["content_type"].startswith("local_") for f in s["content_findings"]["findings"]) == 8  # 현지 결과는 그대로
    # 해당(present) → 채널 판정 행 · 제외 권고. 근거 블록 원문이 문제 문구
    v = [x for x in secs["sec_1_01"]["verdicts"] if x["dict_type"] == "channel"]
    assert [(x["external_id"], x["verdict_status"], x["finding_status"], x["bucket"], x["problem_text"]) for x in v] == [
        ("CH-901", "policy", "present", "exclude", "가격 3만원")]
    assert v[0]["reason"] == "합성 채널 셀러 안내 CH-901" and v[0]["basis_article"] is None and v[0]["alternative_expression"] == []
    assert v[0]["verdict_key"] in secs["sec_1_01"]["recommendation_basis"]
    snap = {x["external_id"]: x for x in secs["sec_1_01"]["audit"]["dictionary_snapshots"]}
    assert snap["CH-901"]["dict_type"] == "channel" and snap["CH-901"]["snapshot"]["verdict_status"] == "policy"
    assert any(m["external_id"] == "CH-901" and m["match_key"].startswith("cm_") for m in secs["sec_1_01"]["audit"]["matches"])


def test_channel_call_is_separate_and_sends_no_policy_fields(base_req, types):
    llm = ScriptedJudge()
    _run({**base_req, "bundle": channel_bundle()}, llm)
    ch = [p for p in llm.payloads if p["items"][0]["id"].startswith("CH-")]
    lc = [p for p in llm.payloads if p["items"][0]["id"].startswith("LC-")]
    assert len(ch) == 2 and len(lc) == 2  # 섹션 2개 × (현지 1회 + 채널 1회)
    assert all(len(p["items"]) == 8 for p in lc)  # 현지 입력에 채널 항목이 섞이지 않는다
    for p in ch:
        assert [it["id"] for it in p["items"]] == ["CH-901", "CH-902"]
        for it in p["items"]:
            assert set(it) == {"id", "name", "exclude_when", "keep_when", "candidate_blocks", "matched_patterns"}
        text = json.dumps(p, ensure_ascii=False)
        assert "policy" not in text and "셀러 안내" not in text  # 판정값 · 셀러 문장을 모델에 주지 않는다
    assert ch[0]["items"][0]["matched_patterns"] == ["가격"] and ch[0]["items"][0]["candidate_blocks"] == ["b2"]


def test_local_input_is_unchanged_by_channel_part(base_req, types):
    a, b = ScriptedJudge(), ScriptedJudge()
    _run(base_req, a)
    _run({**base_req, "bundle": channel_bundle()}, b)
    assert a.payloads == [p for p in b.payloads if p["items"][0]["id"].startswith("LC-")]


def test_channel_uncertain_excludes_and_keeps_uncertain(base_req, types):
    rep, problems = _run({**base_req, "bundle": channel_bundle()}, ScriptedJudge("uncertain"))
    assert problems == []
    s = _sections(rep)["sec_1_02"]
    f = next(x for x in s["content_findings"]["findings"] if x["content_type"] == "channel_synthetic_review")
    assert f["status"] == "uncertain"  # 원본 상태는 uncertain 그대로
    v = next(x for x in s["verdicts"] if x["external_id"] == "CH-902")
    assert (v["verdict_status"], v["finding_status"], v["bucket"]) == ("policy", "uncertain", "exclude")
    assert s["bucket_recommendation"] == "exclude"


def test_channel_absent_does_not_exclude(base_req, types):
    b = channel_bundle()
    b["channel"]["entries"] = [_row("CH-902", "없는패턴")]  # 고른 사이트의 행만 — 대응표의 일부여도 된다
    b["bundle_sha256"] = bundle_content_sha256(b)
    rep, problems = _run({**base_req, "bundle": b}, ScriptedJudge())
    assert problems == []
    s = _sections(rep)["sec_1_02"]
    assert s["channel_status"] == "completed" and s["bucket_recommendation"] == "include"
    assert [f["status"] for f in s["content_findings"]["findings"] if f["content_type"].startswith("channel_")] == ["absent"]
    assert s["audit"]["inspection"]["channel"]["items_checked"] == ["CH-902"]


@pytest.mark.parametrize("mode, kind", [("call_fails", "model_call_failed"), ("missing_item", "response_invalid")])
def test_channel_failure_excludes_and_keeps_other_axes(base_req, types, mode, kind):
    rep, problems = _run({**base_req, "bundle": channel_bundle()}, ScriptedJudge(mode))
    assert problems == [] and rep.outcome == "completed"  # 채널 실패는 섹션 결과로만 남는다
    assert rep.payload["summary"]["channel_failed"] == ["sec_1_01", "sec_1_02"] and rep.payload["summary"]["local_failed"] == []
    for s in rep.payload["sections"]:
        assert s["channel_status"] == "failed" and s["channel_failure"]["kind"] == kind
        assert "channel_judgment_failed" in s["recommendation_basis"] and s["bucket_recommendation"] == "exclude"
        types_ = [f["content_type"] for f in s["content_findings"]["findings"]]
        assert not any(t.startswith("channel_") for t in types_)  # 실패를 absent · uncertain으로 바꾸지 않는다
        assert s["local_status"] == "completed" and sum(t.startswith("local_") for t in types_) == 8
        assert not any(v["dict_type"] == "channel" for v in s["verdicts"])


def test_local_failure_does_not_fail_channel(base_req, types):
    rep, problems = _run({**base_req, "bundle": channel_bundle()}, ScriptedJudge(local_fails=True))
    assert problems == []
    s = _sections(rep)["sec_1_01"]
    assert s["local_status"] == "failed" and s["channel_status"] == "completed"
    assert {"local_judgment_failed"} < set(s["recommendation_basis"])  # 현지 실패 + 채널 판정 행
    assert any(v["external_id"] == "CH-901" for v in s["verdicts"])


def test_channel_dictionary_unavailable_is_not_checked_and_not_excluded(base_req, types):
    llm = ScriptedJudge()
    rep, problems = _run({**base_req, "bundle": channel_bundle(rows=False, unavailable=True)}, llm)
    assert problems == []
    assert rep.payload["summary"]["channel_not_checked"] == ["sec_1_01", "sec_1_02"] and rep.payload["summary"]["channel_failed"] == []
    assert all(p["items"][0]["id"].startswith("LC-") for p in llm.payloads)  # 채널 호출 없음
    s2 = _sections(rep)["sec_1_02"]
    assert s2["channel_status"] == "not_checked" and s2["channel_failure"]["kind"] == "dictionary_unavailable"
    assert s2["bucket_recommendation"] == "include" and "channel_judgment_failed" not in s2["recommendation_basis"]
    assert s2["audit"]["inspection"]["channel"]["site_key"] is None


def test_channel_judged_when_local_dictionary_unavailable(base_req, types):
    b = ex.bundle(local_ok=False)
    b["channel"] = {"site_key": "amazon", "version": "channel@synthetic.1", "entries": [_row("CH-901", "가격")]}
    b["rules"]["verdict_map"].append(dict(CHANNEL_RULE))
    b["bundle_sha256"] = bundle_content_sha256(b)
    rep, problems = _run({**base_req, "bundle": b}, ScriptedJudge())
    assert problems == []
    s = _sections(rep)["sec_1_01"]
    assert s["local_status"] == "not_checked" and s["channel_status"] == "completed" and s["bucket_recommendation"] == "exclude"


# ---------------------------------------------------------------------------
# 묶음 · 보고 검증
# ---------------------------------------------------------------------------
def test_bundle_rejects_channel_with_unavailable_and_unknown_or_duplicate_items(types):
    with pytest.raises(BundleError, match="channel과 channel_unavailable"):
        check_bundle(channel_bundle(unavailable=True), target_country="US", types=types)
    b = channel_bundle()
    b["channel"]["entries"].append(_row("CH-999", "기타"))
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="대응표에 없다"):
        check_bundle(b, target_country="US", types=types)
    b = channel_bundle()
    b["channel"]["entries"][1]["external_id"] = "LC-01"
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="중복 external_id"):
        check_bundle(b, target_country="US", types=types)
    b = channel_bundle()
    b["channel"]["entries"] = []
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="구조 오류"):
        check_bundle(b, target_country="US", types=types)


def test_operating_type_map_has_no_channel_items_yet(base_req):
    """운영 대응표에 채널 항목을 넣기 전에는 채널 행을 받지 않는다 — 유형 코드 없이 판정하지 않는다."""
    assert bundle_mod.load_type_map().channel == ()
    rep = judgment.run_judgment({**base_req, "bundle": channel_bundle()}, ex.config(), llm=ScriptedJudge())
    assert rep.outcome == "failed" and rep.failure.kind == "bundle_invalid" and "대응표에 없다" in rep.failure.message


def test_channel_rows_without_channel_rule_fail_the_report(base_req, types):
    rep = judgment.run_judgment({**base_req, "bundle": channel_bundle(rule=False)}, ex.config(), llm=ScriptedJudge())
    assert rep.outcome == "failed" and rep.failure.kind == "bundle_invalid" and "채널 판정 행" in rep.failure.message


def test_validator_rejects_inconsistent_channel_results(base_req, types):
    req = {**base_req, "bundle": channel_bundle()}
    rep, problems = _run(req, ScriptedJudge())
    assert problems == []
    doc = json.loads(rep.model_dump_json())

    def broken(mutate) -> list[str]:
        d = copy.deepcopy(doc)
        mutate(d["payload"]["sections"][0])
        return validate_mod.validate_report(d, req)

    def drop_finding(s):
        s["content_findings"]["findings"] = [f for f in s["content_findings"]["findings"] if f["content_type"] != "channel_synthetic_review"]

    assert any("채널 완료인데" in x for x in broken(drop_finding))
    assert any("channel_status" in x for x in broken(lambda s: s.pop("channel_status")))
    assert any("공급 여부" in x for x in broken(lambda s: s.update(channel_status="not_checked", channel_failure={"kind": "x", "message": "y"})))
    # 채널 판정 대상이 아닌 요청에 채널 결과가 실려 오면 거절한다
    plain_rep, _ = _run(base_req, ex.FakeJudge())
    d = json.loads(plain_rep.model_dump_json())
    d["payload"]["sections"][0]["channel_status"] = "completed"
    assert any("대상이 아닌데" in x for x in validate_mod.validate_report(d, base_req))
