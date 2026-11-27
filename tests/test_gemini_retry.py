"""실제 SDK + 모킹 HTTP 전송. Gemini·DB·브로커를 호출하지 않는다."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from pipeline import vlm
from pipeline.config import load_config


@pytest.fixture
def be_dependencies():
    for module in ("sqlalchemy", "redis", "celery", "boto3", "jcs", "jwt", "psycopg"):
        pytest.importorskip(module, reason="BE 환경에서 실행할 작업 계층 회귀 테스트")


def quota(*ids, value="10"):
    return [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": name, "quotaValue": value} for name in ids
    ]}, {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]


def error_body(code, details=None):
    return {"error": {"code": code, "status": "RESOURCE_EXHAUSTED" if code == 429 else "UNAVAILABLE",
                      "message": "synthetic API failure", "details": details or []}}


@pytest.fixture
def wire(monkeypatch):
    """SDK 직하단 전송을 대체하므로 SDK의 숨은 재시도도 횟수에 포함한다."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    state = SimpleNamespace(events=[], calls=[], sleeps=[], text='{"blocks": []}')

    def send(_client, request, **kwargs):
        state.calls.append(request)
        if not state.events:
            raise AssertionError("예정한 횟수보다 많은 실제 SDK HTTP 전송")
        event = state.events.pop(0)
        if isinstance(event, Exception):
            raise event
        if event == 200:
            body = {"candidates": [{"content": {"parts": [{"text": state.text}], "role": "model"},
                                    "finishReason": "STOP"}]}
            return httpx.Response(200, json=body, request=request)
        code, details = event if isinstance(event, tuple) else (event, [])
        return httpx.Response(code, json=error_body(code, details), request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(vlm.time, "sleep", state.sleeps.append)
    return state


@pytest.fixture(params=["merge", "translate"])
def caller(request):
    cfg = load_config()
    if request.param == "merge":
        obj = vlm.GeminiMergeAssistant("gemini-test", 0, cfg["merge"]["llm_timeout_s"])
        call = lambda: obj("prompt", "payload")
    else:
        obj = vlm.GeminiTranslateAssistant("gemini-test", 0, cfg["translate"]["timeout_s"])
        call = lambda: obj("prompt", "payload", None)
    yield obj, call
    if obj._client is not None:
        obj._client.close()


def test_first_success_and_90_second_timeout(wire, caller, caplog):
    wire.events = [200]
    with caplog.at_level("INFO", logger="pipeline.vlm"):
        assert caller[1]().text == wire.text
    assert len(wire.calls) == 1 and wire.sleeps == []
    assert set(wire.calls[0].extensions["timeout"].values()) == {90.0}
    assert "attempt=1/2" in caplog.text and "Gemini 완료" in caplog.text


@pytest.mark.parametrize("first", [408, 500, 502, 503, 504, httpx.ConnectTimeout("connect"),
                                  httpx.ReadTimeout("read"), httpx.ConnectError("connection"), httpx.ReadError("read"),
                                  (429, quota("GenerateRequestsPerMinutePerProject")),
                                  (429, [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"}])])
def test_transient_error_then_success(wire, caller, first, caplog):
    wire.events = [first, 200]
    with caplog.at_level("INFO", logger="pipeline.vlm"):
        caller[1]()
    assert len(wire.calls) == 2 and wire.sleeps == [1.0]
    assert all(set(r.extensions["timeout"].values()) == {90.0} for r in wire.calls)
    assert "retry=True" in caplog.text and "Gemini 완료" in caplog.text and "attempt=2/2" in caplog.text


@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout("read")])
def test_both_fail_with_no_sdk_retry(wire, caller, failure, caplog):
    wire.events = [failure, failure]
    with pytest.raises(vlm.VlmError):
        caller[1]()
    assert len(wire.calls) == 2 and wire.sleeps == [1.0]
    assert "attempt=2/2" in caplog.text and "final=True" in caplog.text


@pytest.mark.parametrize("failure", [400, 401, 403, 404, 501, 429,
    (429, quota("GenerateRequestsPerDayPerProject")),
    (429, quota("generate_requests_per_day")),
    (429, quota("GenerateRequestsPerMonthPerProject")),
    (429, quota("GenerateRequestsPerMinutePerProject", "GenerateRequestsPerDayPerProject")),
    (429, quota("GenerateRequestsPerMinutePerProject", value="0")),
    (429, quota("UnknownQuota")),
    (429, [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]),
    (429, [{"reason": "BILLING_LIMIT_EXCEEDED"}, {"reason": "RATE_LIMIT_EXCEEDED"}]),
    (429, [{"reason": "CREDIT_BALANCE_EXHAUSTED"}, {"reason": "RATE_LIMIT_EXCEEDED"}]),
    ValueError("invalid request"), httpx.WriteTimeout("write"), httpx.PoolTimeout("pool")])
def test_non_retryable_fails_immediately(wire, caller, failure):
    wire.events = [failure]
    with pytest.raises(vlm.VlmError):
        caller[1]()
    assert len(wire.calls) == 1 and wire.sleeps == []


def test_response_format_error_does_not_repeat_request(wire):
    from pipeline.stages.translate import parse_response, TranslateResponseError

    wire.events = [200]
    wire.text = "not JSON"
    caller = vlm.GeminiTranslateAssistant("gemini-test", 0, 90)
    try:
        reply = caller("prompt", "payload", None)
        with pytest.raises(TranslateResponseError):
            parse_response(reply.text, ["b1"])
    finally:
        caller._client.close()
    assert len(wire.calls) == 1 and wire.sleeps == []


@pytest.mark.parametrize("cls", [vlm.GeminiJudgeAssistant, vlm.GeminiLabelAssistant])
def test_other_stages_keep_existing_policy(wire, cls):
    wire.events = [503]
    caller = cls("gemini-test", 0, 60)
    try:
        with pytest.raises(vlm.VlmError):
            caller("prompt", "payload", None)
    finally:
        caller._client.close()
    assert len(wire.calls) == 1 and wire.sleeps == []
    assert set(wire.calls[0].extensions["timeout"].values()) == {60.0}


def test_scope_remembers_success_before_later_ocr_failure_and_resets(wire):
    wire.events = [200]
    caller = vlm.GeminiMergeAssistant("gemini-test", 0, 90)
    try:
        with vlm.track_gemini_calls(7) as calls:
            assert not calls["started"]
            caller("prompt", "payload")
            assert calls == {"attempt_id": 7, "started": True}
        with vlm.track_gemini_calls(8) as calls:
            assert not calls["started"]
    finally:
        caller._client.close()


@pytest.mark.parametrize("stage,origin,code,allowed", [
    ("translate", "auto", "TRANSLATE_ALL_FAILED", False),
    ("translate", "recovery", "LEASE_EXPIRED", False),
    ("analyze", "recovery", "LEASE_EXPIRED", False),
    ("analyze", "auto", "ANALYZE_RESULT_INVALID", False),
    ("analyze", "auto", "ARTIFACT_STORE", False),
    ("analyze", "auto", "REGULATORY_DICT_LOOKUP_FAILED", True),
    ("analyze", "auto", "OCR_FAILED", True),
    ("judge", "auto", "JUDGE_FAILED", True),
    ("label", "recovery", "LEASE_EXPIRED", True),
])
def test_task_retry_gate(be_dependencies, stage, origin, code, allowed):
    from app.execution import automatic_retry_allowed

    assert automatic_retry_allowed({"stage": stage, "error_code": code}, origin) is allowed


@pytest.mark.parametrize("stage", ["analyze", "translate"])
@pytest.mark.parametrize("origin", ["auto", "recovery", "user"])
def test_retry_creation_blocks_automatic_but_preserves_job06(be_dependencies, monkeypatch, stage, origin):
    from app import execution

    run = {"status": "running", "cancelled_at": None}
    att = {"stage": stage, "status": "failed", "is_current": True, "retry_count": 0, "max_retry": 2,
           "error_code": "LEASE_EXPIRED", "unit_id": 4, "input_manifest": {}, "target_count": 1, "revision": 0}
    monkeypatch.setattr(execution, "lock_chain", lambda *a: ({"status": "processing"}, run, att))
    monkeypatch.setattr(execution, "retry_blocked", lambda *a: None)
    create = Mock(return_value=8)
    monkeypatch.setattr(execution, "create_attempt", create)
    if origin == "user":
        assert execution.create_retry(None, 7, origin) == 8
        assert create.call_args.kwargs["supersedes"] is att
    else:
        with pytest.raises(execution.RetryNotAllowed):
            execution.create_retry(None, 7, origin)
        create.assert_not_called()


def test_translate_failure_handler_cannot_add_task_retry(be_dependencies, monkeypatch):
    from app import execution
    from app.flows import downstream

    db = Mock()
    db.execute.return_value.mappings.return_value.one.return_value = {"id": 1, "status": "running"}
    retry = Mock(side_effect=AssertionError("작업 계층 중복 재시도"))
    monkeypatch.setattr(execution, "create_retry", retry)
    monkeypatch.setattr(downstream, "progress", lambda *a: [])
    att = {"stage": "translate", "parent_task_id": 1, "unit_id": 2, "retry_count": 0, "max_retry": 2}
    assert downstream._after_failure(db, att, "TRANSLATE_ALL_FAILED", True) == []
    retry.assert_not_called()


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("failure", ["ocr", "unexpected", "gemini"])
def test_analyze_worker_retries_only_before_gemini(be_dependencies, monkeypatch, wire, started, failure):
    from contextlib import nullcontext
    from app import ai_adapters, execution
    from app.flows import analysis
    from pipeline.errors import AnalyzeError

    wire.events = [503, 503] if failure == "gemini" else [200]
    gemini = vlm.GeminiMergeAssistant("gemini-test", 0, 90)

    def analyze(*args, **kwargs):
        if started:
            gemini("prompt", "payload")  # 앞 섹션 성공 뒤 다음 섹션의 오류도 전체 재실행 금지
        if failure == "ocr":
            raise AnalyzeError("OCR_FAILED", "synthetic failure", 1)
        raise ValueError("synthetic failure")

    lease = SimpleNamespace(manifest={"sources": [], "target_country": "US", "regulatory_class": "cosmetic"},
                            epoch=1, fingerprint="test", attempt_id=7)
    monkeypatch.setattr(execution, "acquire", lambda *args: lease)
    monkeypatch.setattr(analysis, "build_bundle", lambda *args: {})
    monkeypatch.setattr(analysis, "get_store", lambda: None)
    monkeypatch.setattr(ai_adapters, "analyzer", lambda: SimpleNamespace(analyze=analyze))
    monkeypatch.setattr(analysis.common, "Heartbeat", lambda *args: nullcontext(SimpleNamespace(lost=False)))
    monkeypatch.setattr(analysis.analysis_progress, "ProgressReporter", Mock(side_effect=RuntimeError("no Redis")))
    envelope = Mock(return_value=None)
    monkeypatch.setattr(analysis, "failed_envelope", envelope)
    monkeypatch.setattr(analysis, "submit", lambda *args: None)
    failed = Mock(return_value=[])
    monkeypatch.setattr(execution, "fail_attempt", failed)
    try:
        analysis.execute_analyze(7)
    finally:
        if gemini._client is not None:
            gemini._client.close()
    if failure == "ocr":
        assert envelope.call_args.args[3] is (not started)
    else:
        assert failed.call_args.kwargs["retryable"] is (not started)
    assert len(wire.calls) == (2 if failure == "gemini" else 1) * int(started)


@pytest.mark.parametrize("stage", ["analyze", "translate"])
def test_recovery_does_not_recalculate_unknown_call_count(be_dependencies, monkeypatch, stage):
    from app import execution, recovery

    att = {"id": 7, "stage": stage, "retry_count": 0, "max_retry": 2}
    db = Mock()
    db.execute.return_value.first.side_effect = [("running", True), None]
    monkeypatch.setattr(recovery.app_db, "SessionLocal", lambda: db)
    monkeypatch.setattr(recovery, "_rows", lambda sql, **kw: [] if "task_handoff" in sql else [{"id": 7}])
    monkeypatch.setattr(execution, "lock_chain", lambda *a: ({"status": "processing"}, {"status": "running"}, att))
    monkeypatch.setattr(execution, "_mark_failed", lambda *a: {**att, "error_code": "LEASE_EXPIRED"})
    failed = Mock(return_value=[])
    monkeypatch.setattr(execution, "handler_for", lambda *a: SimpleNamespace(after_failure=failed))
    retry = Mock(side_effect=AssertionError("복구 재계산 금지"))
    monkeypatch.setattr(execution, "create_retry", retry)
    monkeypatch.setattr(execution, "dispatch", Mock())
    stats = {"expired": 0}
    recovery._expired(stats)
    assert stats["expired"] == 1
    assert failed.call_args.args[-2:] == ("LEASE_EXPIRED", False)
    retry.assert_not_called()
