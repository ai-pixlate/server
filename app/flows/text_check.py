"""⑧ 번역문 규칙 검사(stage text_check) — BE 연결.

근거: docs/ai/be-handoff.md 8절, contract.md 5.3 · 7.1, OpenAPI BlockCheck · confirmReview 「규칙 검사 연결」.
2차 활성 검사는 rule 하나다. LLM 검사(translation_review)는 DB stage 만 허용하고 등록 · 호출하지 않는다.

이 모듈이 맡는 것(독립 BE 범위):
- 활성 검사 구성 — 새 하류 실행의 run_scope.active_checks 에 고정하고 등록 · 집계 · 확정이 같은 값을 쓴다.
- 등록 — 섹션 단위 시도. 같은 고정 입력의 현재 시도가 있으면(대기 · 실행 · 완료 · 최종 실패) 그대로 쓴다.
- 실행 — pipeline.handoff.translation.run_text_check 와 validate_report 를 그대로 부른다(모델 호출 없음).
- 채택 — 보고 전체만 채택(부분 채택 없음), 대상 블록 결과 완결성 확인, 자기 producer flag 만 교체.
- 블록 검사 상태(공개 5상태) 판정과 확정 조건 — 조회와 확정이 같은 판정 함수를 쓴다.
- 고정 요청 보관 — 요청 본문(원문 · 번역문) · 고정 공급 · 공급 기록은 job 별 삭제 가능한 객체(input/{job_id}/)에 두고,
  불변 명세(input_manifest)에는 콘텐츠 없는 참조 · 해시만 둔다. 전체 취소가 접두사를 지운다(app.flows.cancel).

미확정 연결 지점은 공급자로 분리했다. 실제 공급자가 연결되기 전에는 rule 검사를 켤 수 없다(configured_active_checks).
- SupplyPolicy (M3 · A3): 검사 요청에 고정할 용어집 공급(출처 · 버전 · 블록별 유효 강제 용어). 기본값은 '결정 전'.
- InputKeys (A1): 요청 → 블록별 inputKey. BE 는 키 계산 규칙을 만들지 않는다. 기본값은 '연결 전'.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
import os
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ai_adapters, execution
from app import db as app_db
from app.execution import AdoptContext, AdoptionRejected, AdoptOutcome, Envelope
from app.flows import common
from app.flows.common import block_key, json_value, report, section_key, submit
from app.manifest import sha256_bytes, sha256_text, stable_hash, stable_json
from app.storage import ObjectMissing, get_store

log = logging.getLogger(__name__)

STAGE = "text_check"
OWN_PRODUCERS = ("translation_validation", "glossary_validation")  # 규칙 검사가 소유 · 교체하는 flag(contract 7.1)
SUPPORTED_CHECKS = ("rule",)  # 2차 범위
ACTIVE_ENV = "PIXLATE_ACTIVE_CHECKS"
AUTO_RETRY = 1  # 개발 기본값(운영 수치 미정 — be-handoff 8.6 한도는 BE 운영값)
MAX_RETRY = 2

# 공개 상태(OpenAPI BlockCheck.state)
SETTLED, IN_PROGRESS, FAILED, MISSING, OUTDATED = "settled", "in_progress", "failed", "missing", "outdated"
PASSING = frozenset({SETTLED, FAILED})  # 2차 확정 통과(failed 는 확인 없이 통과)

# 규칙 검사 결과 → 공개 신호(OpenAPI confirmReview 「규칙 검사 연결」 · SignalCode · contract 7.1). 신호는 확인 필요 표시이며 위반 확정이 아니다.
# (producer, type) 별 대응. regulatory 는 저장된 원판정값이 아래 값일 때만 — allowed · 그 밖의 값 · 값 없음(원판정값을 버리던 이전 저장분)은
# 대응하지 않는다(현재 사전 행으로 추정하지 않는다). 2차 활성 검사는 rule 만이라 translation_llm_review 등 다른 producer 는 여기서 내지 않는다.
SIGNAL_MANDATORY_TERM = "mandatory_term_unapplied"
SIGNAL_PROHIBITED = "prohibited_expression"
REGULATORY_SIGNAL_STATUSES = frozenset({"regulated", "conditional"})


class CheckConfigError(RuntimeError):
    pass


class SupplyBasisUnresolved(Exception):
    """검사에 쓸 고정 공급을 정할 수 없다(M3 결정 전 · 공급 기록 없음 등). 추정해 채우지 않는다."""


class InputKeysUnavailable(Exception):
    """블록별 inputKey 를 계산할 수 없다(A1 연결 전)."""




class RequestStoreFailed(Exception):
    """고정 요청 객체를 저장 · 되읽기 · 해시 확인하지 못했다 — 검사 시도를 등록하지 않는다."""


# ---------------------------------------------------------------------------------------------------------
# 미확정 연결 지점 — 공급자
# ---------------------------------------------------------------------------------------------------------
class SupplyPolicy(Protocol):
    name: str

    def glossary(self, db: Session, run: dict[str, Any], section_id: int, targets: list[dict[str, Any]], *, origin: str
                 ) -> dict[str, Any]:
        """검사 요청에 고정할 용어집 공급(AI GlossarySupply 형식). 정할 수 없으면 SupplyBasisUnresolved."""
        ...


class InputKeys(Protocol):
    name: str

    def block_keys(self, request: dict[str, Any]) -> dict[str, str]:
        """검사 요청 → {block_key: inputKey}(대상 블록 전부). 계산할 수 없으면 InputKeysUnavailable."""
        ...


class _PendingSupply:
    name = "pending:M3"

    def glossary(self, db, run, section_id, targets, *, origin):
        raise SupplyBasisUnresolved("검사 공급 기준(M3)이 정해지지 않았다")


class _PendingKeys:
    name = "pending:A1"

    def block_keys(self, request):
        raise InputKeysUnavailable("블록별 inputKey 계산(A1)이 연결되지 않았다")


_supply: SupplyPolicy = _PendingSupply()
_keys: InputKeys = _PendingKeys()


def set_providers(*, supply: SupplyPolicy | None = None, keys: InputKeys | None = None) -> None:
    """공급자 교체(시험 대역 · 이후 실제 연결). None 이면 '연결 전'으로 되돌린다."""
    global _supply, _keys
    _supply = supply if supply is not None else _PendingSupply()
    _keys = keys if keys is not None else _PendingKeys()


def providers_ready() -> bool:
    return not isinstance(_supply, _PendingSupply) and not isinstance(_keys, _PendingKeys)


# ---------------------------------------------------------------------------------------------------------
# 활성 검사 구성
# ---------------------------------------------------------------------------------------------------------
def configured_active_checks() -> list[str]:
    """새 하류 실행에 고정할 활성 검사. 기본은 꺼짐(be-handoff 8.8 배포 순서 — 워커 교체 확인 뒤 켠다)."""
    raw = os.getenv(ACTIVE_ENV, "").strip()
    if not raw:
        return []
    vals = sorted({v.strip() for v in raw.split(",") if v.strip()})
    bad = [v for v in vals if v not in SUPPORTED_CHECKS]
    if bad:
        raise CheckConfigError(f"{ACTIVE_ENV}: 지원하지 않는 검사 {bad}(2차는 rule 만)")
    if "rule" in vals and not providers_ready():
        raise CheckConfigError(f"{ACTIVE_ENV}=rule: 공급(M3)·inputKey(A1) 연결 전에는 규칙 검사를 켤 수 없다")
    return vals


def run_active_checks(run: dict[str, Any] | None) -> tuple[str, ...]:
    """실행에 고정된 활성 검사. 기록이 없으면 비활성 — 실행 상태 none 에서 추정하지 않는다(contract 5.3)."""
    if run is None:
        return ()
    scope = json_value(run["run_scope"]) or {}
    v = scope.get("active_checks")
    return tuple(v) if isinstance(v, list) else ()


# ---------------------------------------------------------------------------------------------------------
# 요청 · 고정 입력
# ---------------------------------------------------------------------------------------------------------
def checkable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """검사 대상: 제외되지 않은 블록 · 비공백 원문 · 비공백 현재 trans_1(be-handoff 8.1)."""
    return [r for r in rows if r["is_excluded"] is False and (r["source_ko"] or "").strip() and (r["trans_1"] or "").strip()]


def _bundle(db: Session, run: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.flows import downstream

    return downstream._analysis_bundle(db, (json_value(run["run_scope"]) or {})["analysis_task_id"])


def _bundle_of_handoff(db: Session, handoff_id: int) -> dict[str, Any]:
    """등록 때 고정한 분석 인계의 사전 묶음(같은 인계 id 의 payload — 실행 때 새로 고르지 않는다)."""
    p = db.execute(text("SELECT payload FROM task_handoff WHERE id = :h AND state = 'adopted'"), {"h": handoff_id}).scalar()
    if p is None:
        raise AdoptionRejected(f"고정한 사전 묶음 인계 {handoff_id} 없음", code="INPUT_MISSING")
    return json_value(p)["bundle"]


def request_parts(run: dict[str, Any], section_id: int, rows: list[dict[str, Any]], bundle: dict[str, Any],
                  *, targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """요청 중 그 시점 DB 값에서 오는 부분(대상 · 문맥 블록 · revision · 번역문 · 국가 · 규제 분류). 등록 때 고정한다.
    targets 를 주면 그 블록만 요청 대상이다(블록 국소 실패를 뺀 실행 요청 — 문맥 blocks 는 섹션 전체 그대로). 키 계산 요청은 전체 대상."""
    from app.flows import downstream

    if targets is None:
        targets = checkable(rows)
    return {
        "target_country": bundle["target_country"], "regulatory_class": bundle["regulatory_class"],
        "section_key": section_key(section_id),
        "blocks": [b.model_dump(mode="json") for b in downstream._text_blocks(section_id, rows)],
        "targets": [{"block_key": block_key(r["id"]), "revision": r["revision"], "text": r["trans_1"]} for r in targets],
    }


def assemble(run_id: int, parts: dict[str, Any], glossary: dict[str, Any], bundle: dict[str, Any], *, attempt_id: str = "pending"
             ) -> dict[str, Any]:
    """TextCheckRequest(raw) — 고정 부분 · 고정 공급 · 고정 묶음으로만 만든다(실행 직전 DB 값으로 바꾸지 않는다)."""
    from app.dictionary_bundle import BundleError, to_ai_bundle
    from pipeline.handoff.envelope import CONTRACT_VERSION

    try:
        ai_bundle = to_ai_bundle(bundle)
    except BundleError as e:
        raise ai_adapters.AdapterUnavailable(f"규칙 검사를 시작할 수 없다: {e}") from e
    return {"contract_version": CONTRACT_VERSION, "execution_id": str(run_id), "attempt_id": attempt_id, "stage": STAGE,
            **parts, "glossary": glossary, "bundle": ai_bundle}


def build_request(db: Session, run: dict[str, Any], section_id: int, rows: list[dict[str, Any]], glossary: dict[str, Any],
                  *, attempt_id: str = "pending") -> dict[str, Any]:
    """현재 DB 값으로 만든 요청 — 등록(고정 전)과 현재 의미 키 계산에만 쓴다. 실행은 고정 요청(assemble)을 쓴다."""
    _, bundle = _bundle(db, run)
    return assemble(run["id"], request_parts(run, section_id, rows, bundle), glossary, bundle, attempt_id=attempt_id)


def current_keys(db: Session, run: dict[str, Any], section_id: int, *, origin: str) -> dict[str, str]:
    """현재 입력의 블록별 inputKey(현재 DB 문구 · 현재 공급 정책 · A1). 계산할 수 없으면 SupplyBasisUnresolved / InputKeysUnavailable."""
    from app.flows import downstream

    rows = downstream._block_rows(db, section_id)
    targets = checkable(rows)
    if not targets:
        return {}
    return _current_input(db, run, section_id, rows, targets, origin=origin)[0]


def _current_input(db: Session, run: dict[str, Any], section_id: int, rows: list[dict[str, Any]], targets: list[dict[str, Any]],
                   *, origin: str) -> tuple[dict[str, str], dict[str, Any], set[str]]:
    """(현재 블록별 inputKey, 현재 공급, 공급이 알려 준 블록 국소 오류 블록). 키는 섹션 검사 대상 전체로 만든 요청으로 계산한다 — 국소 오류
    블록의 키에는 그 블록의 충돌이 들어가고(AI 키 재료의 블록 국소 처리), 다른 블록의 키는 다른 블록 문구 · 오류를 담지 않는다.
    공급이 failed(공통 오류)여도 키는 만든다(AI 키 함수 — 실패 입력도 비교 가능)."""
    raw = _supply.glossary(db, run, section_id, targets, origin=origin)
    supplied = supplied_local_failures(raw)
    glossary, _ = split_supply(raw)
    keys = _keys.block_keys(build_request(db, run, section_id, rows, glossary))
    if missing := sorted(block_key(r["id"]) for r in targets if block_key(r["id"]) not in keys):
        raise InputKeysUnavailable(f"현재 inputKey 없음 {missing}")
    return keys, glossary, supplied


# 고정 요청 객체(취소 가능한 보관). 내부 참조 형식 — DB 스키마 · 공개 API 가 아니다.
#   key   = input/{job_id}/text_check/{sha256}.json   (내용 주소 · job 접두사 — 전체 취소의 접두사 삭제 대상)
#   본문  = stable_json({format, job_id, section_id, request, glossary, supply_record}) UTF-8
#   참조  = input_manifest.request_ref {format, key, sha256, size}
REQUEST_FORMAT = "text-check-request@1"
CHECK_REQUEST_MISSING = "CHECK_REQUEST_MISSING"  # 객체 없음(삭제 · 유실) — 같은 참조로 재시도해도 같다
CHECK_REQUEST_HASH_MISMATCH = "CHECK_REQUEST_HASH_MISMATCH"  # 내용이 참조 해시 · 형식과 다름
CHECK_REQUEST_READ_FAILED = "CHECK_REQUEST_READ_FAILED"  # 저장소 읽기 장애(일시 장애 가능 — 같은 입력 자동 재시도)


def request_key(job_id: int, sha: str) -> str:
    return f"input/{job_id}/text_check/{sha}.json"


def store_request(job_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """고정 요청 객체를 쓰고 되읽어 해시를 확인한다. 같은 키가 이미 있어도(내용 주소) 되읽은 내용을 확인한다.
    실패하면 RequestStoreFailed — 호출자는 시도를 등록하지 않는다. 메시지에 본문을 넣지 않는다."""
    data = stable_json(body).encode("utf-8")
    sha = sha256_bytes(data)
    key = request_key(job_id, sha)
    store = get_store()
    try:
        store.put_if_absent(key, data, "application/json")
        back = store.get(key)
    except Exception as e:  # noqa: BLE001 — 저장소 장애 종류와 무관하게 등록하지 않는다
        raise RequestStoreFailed(f"고정 요청 저장 · 되읽기 실패 {key}: {e.__class__.__name__}") from e
    if sha256_bytes(back) != sha:
        raise RequestStoreFailed(f"고정 요청 되읽기 해시 불일치 {key}")
    return {"format": REQUEST_FORMAT, "key": key, "sha256": sha, "size": len(data)}


class RequestUntrusted(Exception):
    """고정 요청을 믿을 수 없다(누락 · 해시 · 형식 불일치 · 읽기 장애). code 는 task error_code(API enum 아님)."""

    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def read_request(job_id: int, ref: dict[str, Any]) -> dict[str, Any]:
    """고정 요청 본문을 읽고 참조 형식 · job · sha256 을 확인한다. 실패 사유를 구분한 RequestUntrusted. 메시지에 본문을 넣지 않는다."""
    key, sha = ref.get("key"), ref.get("sha256")
    if ref.get("format") != REQUEST_FORMAT or not isinstance(key, str) or key != request_key(job_id, str(sha)):
        raise RequestUntrusted(CHECK_REQUEST_HASH_MISMATCH, f"고정 요청 참조가 형식 · job 과 맞지 않는다 {key}", retryable=False)
    try:
        data = get_store().get(key)
    except ObjectMissing:
        raise RequestUntrusted(CHECK_REQUEST_MISSING, f"고정 요청 객체 없음 {key}", retryable=False) from None
    except Exception as e:  # noqa: BLE001
        raise RequestUntrusted(CHECK_REQUEST_READ_FAILED, f"고정 요청 읽기 실패 {key}: {e.__class__.__name__}", retryable=True) from e
    if sha256_bytes(data) != sha:
        raise RequestUntrusted(CHECK_REQUEST_HASH_MISMATCH, f"고정 요청 해시 불일치 {key}", retryable=False)
    body = json.loads(data.decode("utf-8"))
    if body.get("format") != REQUEST_FORMAT or body.get("job_id") != job_id:
        raise RequestUntrusted(CHECK_REQUEST_HASH_MISMATCH, f"고정 요청 본문 형식 · job 불일치 {key}", retryable=False)
    return body


def load_request(job_id: int, ref: dict[str, Any]) -> dict[str, Any]:
    """실행 시작 때의 확인 — 요청을 믿을 수 없으면 실행하지 않는다(AdapterFailed, 사유 코드 그대로)."""
    try:
        return read_request(job_id, ref)
    except RequestUntrusted as e:
        raise ai_adapters.AdapterFailed(e.code, str(e), retryable=e.retryable) from e


def verify_request_for_adoption(job_id: int, ref: dict[str, Any]) -> None:
    """채택 직전(job 잠금 안)의 재확인 — 실행 뒤 요청 객체가 삭제 · 변조 · 읽기 불가가 되었으면 보고 전체를 채택하지 않는다.
    결과 보고(done · skipped)뿐 아니라 실패 보고도 같다 — 믿을 수 없는 요청의 실패를 일반 검사 실패(확정 통과)로 받지 않는다."""
    try:
        read_request(job_id, ref)
    except RequestUntrusted as e:
        raise AdoptionRejected(f"채택 전 고정 요청 확인 실패 — 보고 전체를 채택하지 않는다: {e}", code=e.code,
                               retryable=e.retryable) from e


def split_supply(supply: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """공급자 반환값 → (검사 요청에 넣을 공급, 공급 기록). 기록(`supply_record`, 예: Selection 직렬화)은 요청 · inputKey 에 넣지 않는다."""
    g = dict(supply)
    g.pop("local_failures", None)  # 블록 국소 오류 목록은 요청 공급이 아니다(supplied_local_failures)
    return g, g.pop("supply_record", None)


def supplied_local_failures(supply: dict[str, Any]) -> set[str]:
    """공급자가 알려 준 블록 국소 강제 용어 오류 블록(block_key). 공급 형식 밖의 내부 값 — 요청 · 지문 · 키에 넣지 않는다."""
    return set(supply.get("local_failures") or [])


def _fixed(db: Session, run: dict[str, Any], section_id: int, rows: list[dict[str, Any]], glossary: dict[str, Any], record: Any,
           keys: dict[str, str], run_targets: list[dict[str, Any]], local: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(불변 명세 — 콘텐츠 없는 참조 · 해시, 고정 요청 본문). request_ref 는 객체 저장이 성공한 뒤 register 가 채운다.
    run_targets = 실제 요청 대상(정상 블록), local = 블록 국소 실패 블록. 둘을 합치면 섹션 검사 대상 전체(누락 · 중복 없음 — register 가 확인).
    input_keys 는 전체 대상의 키(국소 실패 블록 포함 — 조회가 같은 입력의 실패인지 비교한다)."""
    from app.flows import downstream
    from pipeline.handoff.translation import text_check_manifest

    hid, bundle = _bundle(db, run)
    parts = request_parts(run, section_id, rows, bundle, targets=run_targets)
    _, msha = text_check_manifest(assemble(run["id"], parts, glossary, bundle))
    body = {"format": REQUEST_FORMAT, "job_id": run["job_id"], "section_id": section_id, "request": parts, "glossary": glossary,
            "supply_record": record}
    manifest = {
        "stage": STAGE, "section_id": section_id, "active_checks": ["rule"], "blocks_fp": downstream._blocks_fp(rows),
        "targets": [{"block_id": r["id"], "revision": r["revision"], "text_sha256": sha256_text(r["trans_1"]),
                     "source_sha256": sha256_text(r["source_ko"] or "")} for r in run_targets],
        # 블록 국소 실패(11-11 — 규칙 검사에 한한 요청 대상 예외): 실행하지 않고 그 입력의 failed 로 보인다. 최소 내부 필드(블록 id 목록)
        "local_failures": sorted(r["id"] for r in local),
        "bundle_handoff_id": hid, "bundle_sha256": bundle["sha256"],
        # 고정 공급은 본문 객체에 — 여기는 상태 · 버전 · 해시만(M3 — 검사가 실제로 쓴 공급을 해시로 묶는다)
        "glossary_ref": {"status": glossary.get("status"), "version": glossary.get("version"), "sha256": stable_hash(glossary)},
        "supply_policy": _supply.name, "supply_record_sha256": None if record is None else stable_hash(record),
        "input_keys": keys, "key_provider": _keys.name,
        "request_manifest_sha256": msha,
    }
    return manifest, body


def _glossary_local_failures(targets: list[dict[str, Any]], glossary: dict[str, Any], supplied: set[str]) -> set[str]:
    """블록 국소 강제 용어 오류 블록(block_key) — 공급자가 알려 준 블록 + AI 가 실행 때 쓰는 같은 함수(`applicable_enforced_ids`)를 같은 고정 입력
    (블록 원문 · 공급 용어)으로 블록마다 불러 실패하는 블록. 규칙은 BE 가 정하지 않는다. 공통 공급 실패(status≠ok)는 섹션 전체라 여기서 보지 않는다."""
    from pipeline.glossary import GlossaryDataError
    from pipeline.stages.text_check import GlossaryTerm, applicable_enforced_ids

    if glossary.get("status") != "ok":
        return set()
    keys = {block_key(r["id"]) for r in targets}
    out = set(supplied) & keys
    terms = [GlossaryTerm(str(t["glossary_id"]), t["term_ko"], t["term_target"], t["enforcement"]) for t in glossary.get("terms") or []]
    for r in targets:
        k = block_key(r["id"])
        if k in out:
            continue
        try:
            applicable_enforced_ids(k, r["source_ko"] or "", terms)
        except GlossaryDataError:
            out.add(k)
    return out


def _run_local_failures(req: dict[str, Any], skip: set[str]) -> set[str]:
    """블록 하나에서만 나는 규칙 검사 오류 블록 — 공통 입력(묶음 · 섹션 블록 · 후보 · 규제 행)은 AI 입력 검증으로 한 번만 만들고,
    블록마다 AI `check_block` 을 불러 강제 용어 적용 판단 불가(GlossaryDataError)와 금지 · 허용 같은 구간 충돌(MatchConflictError)만
    그 블록의 국소 실패로 본다(규칙 기반 · 모델 호출 없음 · 결과는 쓰지 않음). 공통 입력 오류(요청 형식 · 묶음 · 대상 불일치)는 국소로
    나누지 않는다 — 고정 지문 계산 · 실행이 섹션 전체로 보고한다. 매칭 규칙은 AI 함수 그대로 — BE 가 충돌을 해소하거나 새 규칙을 만들지 않는다."""
    from pydantic import ValidationError

    from pipeline.glossary import GlossaryDataError
    from pipeline.handoff import translation as ai_tc
    from pipeline.handoff.bundle import BundleError
    from pipeline.handoff.envelope import HandoffInputError
    from pipeline.matching import MatchConflictError
    from pipeline.stages import text_check as ai_stage

    try:
        model = ai_tc.TextCheckRequest.model_validate({**req, "attempt_id": "local-precheck"})
        _cb, by_key, terms, rows, _m, _sha = ai_tc._text_check_inputs(model)
    except (ValidationError, BundleError, HandoffInputError):
        return set()  # 공통 오류 — 블록마다 국소 실패로 바꾸지 않는다
    gl = [ai_stage.GlossaryTerm(t.glossary_id, t.term_ko, t.term_target, t.enforcement) for t in terms]
    out: set[str] = set()
    for t in model.targets:
        if t.block_key in skip:
            continue
        try:
            ai_stage.check_block(t.block_key, t.revision, t.text, by_key[t.block_key].source_ko, rows, gl)
        except (GlossaryDataError, MatchConflictError):
            out.add(t.block_key)
    return out


def _unregistrable(glossary: dict[str, Any]) -> bool:
    """섹션 공통으로 검사 요청을 만들 수 없는가(공급 실패 — 후보 데이터 오류 등). 블록 국소 오류는 _glossary_local_failures ·
    _run_local_failures 로 그 블록만 실패시키고 나머지 블록은 실제로 검사한다. 조회(section_states)와 N4→N5 대기(waiting)가 같은 판단을 쓴다."""
    return glossary.get("status") != "ok"


def _current_attempt(db: Session, run_id: int, section_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    r = db.execute(
        text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND stage = 'text_check' AND unit_id = :s AND is_current"
             + (" FOR UPDATE" if lock else "")),
        {"r": run_id, "s": section_id},
    ).mappings().first()
    return dict(r) if r else None


# 등록 결과(일괄 등록 CLI 가 구분해 보고한다)
REGISTERED, REUSED, NO_TARGETS, NOT_APPLICABLE, UNRESOLVED_SUPPLY, UNRESOLVED_KEYS, UNRESOLVED_REQUEST_STORE = (
    "registered", "reused", "no_targets", "not_applicable", "unresolved_supply", "unresolved_keys", "unresolved_request_store")
# 고정 요청 지문(AI text_check_manifest)을 계산할 수 없다 — 예: 검사 공급 status=failed(AI 함수가 실패 공급의 지문을 거부).
# 등록하지 않는다(번역 채택 · 블록 저장 트랜잭션을 깨지 않는다). 조회 · 확정은 그 입력을 failed 로 본다(section_states — PM 11-10)
UNRESOLVED_REQUEST_INVALID = "unresolved_request_invalid"
# 요청 조립(사전 묶음 변환 — assemble 의 AdapterUnavailable) 실패. 검사 부가 처리 오류라 번역 채택 · 블록 저장을 되돌리지 않는다
UNRESOLVED_REQUEST_BUILD = "unresolved_request_build"
UNRESOLVED = frozenset({UNRESOLVED_SUPPLY, UNRESOLVED_KEYS, UNRESOLVED_REQUEST_STORE, UNRESOLVED_REQUEST_INVALID,
                        UNRESOLVED_REQUEST_BUILD})
# 요청 객체 저장 실패 기록(PM 11-10 — 셀러가 고칠 수 없는 오류로 작업을 가두지 않는다). 키는 계산됐으므로 그 입력의 실패를 현재 검사 시도 행
# (status=failed · 요청 참조 없음 · input_keys = 실제 A1 키)으로 남긴다 — N4 대기 종료 · 조회 failed · 확정 통과가 같은 기록을 본다.
# 실행 · 큐 전달 · 자동 재시도 없음. 같은 입력이면 기존 재사용 규칙(같은 키 최종 실패 = reused)으로 저장소를 다시 부르지 않는다.
# errorCode = CHECK_REQUEST_STORE_FAILED(11-11 API 6-10 채택): 등록 단계의 고정 요청 객체 쓰기 · 되읽기 · 해시 확인 실패, 실행하지 않은 시도.
# 생산자는 _record_store_failure 하나. 기존 CHECK_REQUEST_*(실행 · 채택 때 읽기 장애 · 유실 · 훼손)와 단계로 구분하며 그 의미는 바꾸지 않는다.
# 검사 작업(verify)은 사용자 재시도 대상이 아니다(retryable=false) — 재시도 API 로 실행되지 않는다.
REQUEST_STORE_FAILURE_CODE = "CHECK_REQUEST_STORE_FAILED"

# 시도가 failed 로 끝났어도 '그 입력의 검사 결과'가 아닌 사유 — 현재 입력의 검사 실패(failed)로 보지 않고 재사용하지 않는다
NOT_A_CHECK_RESULT = frozenset({"INPUT_CHANGED", "CHECK_KEY_UNAVAILABLE"})
# 같은 고정 입력으로 다시 해도 결과가 같은 실패 — 자동 재시도하지 않는다(after_failure). 상태는 다른 최종 실패와 같이 failed(PM 11-10).
# AI 보고의 공급 · 입력 · 데이터 오류(failure.kind = input_invalid · bundle_invalid → 기존 코드 TEXT_CHECK_{KIND}, 지문 유무 무관) +
# 규칙 검사 단계의 AI_ADAPTER_UNAVAILABLE(실행 때 고정 사전 묶음 변환 실패 — 이 단계의 유일한 생산자는 assemble). 분석 · 번역 단계의 같은 코드는 무관
AI_INPUT_ERROR_CODES = frozenset({"TEXT_CHECK_INPUT_INVALID", "TEXT_CHECK_BUNDLE_INVALID"})
BUNDLE_CONVERSION_CODES = frozenset({ai_adapters.AdapterUnavailable.code})
INPUT_ERROR_CODES = AI_INPUT_ERROR_CODES | BUNDLE_CONVERSION_CODES


@dataclass
class Registration:
    status: str
    attempt_id: int | None = None
    reason: str | None = None


def register(db: Session, run: dict[str, Any], section_id: int, *, origin: str) -> Registration:
    """그 섹션의 현재 입력에 대한 규칙 검사 시도를 확보한다(호출자가 job 잠금 · 커밋 · dispatch).
    - 활성 검사에 rule 이 없거나, 실행이 진행 중이 아니거나, 포함 섹션이 아니면 not_applicable, 검사 대상이 없으면 no_targets.
    - 현재 시도의 고정 키가 현재 키와 같고 유효하면(대기 · 실행 · 완료 · 최종 실패) 그대로 쓴다(reused) — 실패 상태 · 재시도 횟수 보존.
      revision 만 바뀐 경우(배치 · 배율)는 키가 같으므로 재사용한다.
    - 다르면 새 시도가 이전 시도를 대체한다(진행 중이면 취소). 재시도 한도는 쓰지 않는다.
    - 공급(M3)이나 inputKey(A1)를 정할 수 없으면 등록하지 않는다(unresolved_*). 요청 조립(사전 묶음 변환) 실패도 같다 — 검사 부가 처리
      오류만 좁게 받는다(DB · 트랜잭션 오류는 그대로 올려 호출자의 채택 · 저장을 기존대로 롤백한다).
    - 요청 객체 저장에 실패하면 그 입력의 실패를 시도 행으로 남긴다(_record_store_failure — 실행하지 않음)."""
    from app.flows import downstream

    if "rule" not in run_active_checks(run) or run["status"] != "running" or run["cancelled_at"] is not None:
        return Registration(NOT_APPLICABLE, reason="활성 규칙 검사 없음 또는 진행 중 실행 아님")
    sec = downstream._section_row(db, section_id)
    if sec["bucket"] != "include" or section_id not in (json_value(run["run_scope"]) or {}).get("section_ids", []):
        return Registration(NOT_APPLICABLE, reason="포함 섹션 아님")
    rows = downstream._block_rows(db, section_id)
    targets = checkable(rows)
    if not targets:
        return Registration(NO_TARGETS)
    try:
        raw = _supply.glossary(db, run, section_id, targets, origin=origin)
    except SupplyBasisUnresolved as e:
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — %s", run["id"], section_id, origin, e)
        return Registration(UNRESOLVED_SUPPLY, reason=str(e))
    supplied = supplied_local_failures(raw)
    glossary, record = split_supply(raw)
    try:
        req = build_request(db, run, section_id, rows, glossary)  # 키 계산용 — 섹션 검사 대상 전체
    except ai_adapters.AdapterUnavailable as e:  # 요청 조립(사전 묶음 변환) 실패만 — 다른 예외는 그대로 올린다
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — %s", run["id"], section_id, origin, e)
        return Registration(UNRESOLVED_REQUEST_BUILD, reason=str(e))
    try:
        keys = _keys.block_keys(req)
        if missing := sorted(block_key(r["id"]) for r in targets if block_key(r["id"]) not in keys):
            raise InputKeysUnavailable(f"inputKey 없음 {missing}")
    except InputKeysUnavailable as e:
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — %s", run["id"], section_id, origin, e)
        return Registration(UNRESOLVED_KEYS, reason=str(e))
    cur = _current_attempt(db, run["id"], section_id, lock=True)
    if (cur is not None and cur["status"] in ("pending", "running", "done", "failed")
            and not (cur["status"] == "failed" and cur["error_code"] in NOT_A_CHECK_RESULT)
            and keys and all(keys.values()) and (cur["input_manifest"] or {}).get("input_keys") == keys):
        return Registration(REUSED, attempt_id=cur["id"])  # 현재 입력의 유효한 시도(섹션 복원 ④ · 중복 등록 방지)
    # 블록 국소 오류(11-11 AI 회신 — 규칙 검사에 한한 요청 대상 예외): 강제 용어 충돌 · 금지/허용 같은 구간 충돌 블록은 실행 요청에서 빼고
    # 그 입력의 failed 로 남긴다(고정 명세 local_failures). 정상 블록만 실제로 검사한다. 공통 공급 실패면 섹션 전체(아래 지문 거부)
    local = _glossary_local_failures(targets, glossary, supplied)
    if not _unregistrable(glossary):
        local |= _run_local_failures(req, local)
    run_targets = [r for r in targets if block_key(r["id"]) not in local]
    local_rows = [r for r in targets if block_key(r["id"]) in local]
    if (sorted(r["id"] for r in run_targets + local_rows) != sorted(r["id"] for r in targets)
            or {r["id"] for r in run_targets} & {r["id"] for r in local_rows}):
        raise RuntimeError(f"검사 대상 분할 불일치 section={section_id}")  # 원래 전체 대상 = 국소 실패 + 실제 요청 대상(누락 · 중복 없음)
    if local:
        log.warning("규칙 검사 블록 국소 실패 run=%s section=%s origin=%s blocks=%s", run["id"], section_id, origin, sorted(local))
    try:
        m, body = _fixed(db, run, section_id, rows, glossary, record, keys, run_targets, local_rows)
    except ValueError as e:  # AI text_check_manifest 거부(공통 공급 실패 · 요청 형식) — 시도를 만들지 않는다
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — 고정 요청 지문 계산 불가: %s", run["id"], section_id, origin, e)
        return Registration(UNRESOLVED_REQUEST_INVALID, reason=f"고정 요청 지문 계산 불가: {e}")
    except ai_adapters.AdapterUnavailable as e:  # 요청 조립(사전 묶음 변환) 실패만
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — %s", run["id"], section_id, origin, e)
        return Registration(UNRESOLVED_REQUEST_BUILD, reason=str(e))
    # 객체 저장 · 되읽기 · 해시 확인이 성공해야 실행할 시도를 만든다. 실패하면 그 입력의 실패를 기록한다(_record_store_failure — 실행 없음)
    try:
        m["request_ref"] = store_request(run["job_id"], body)
    except RequestStoreFailed as e:
        log.warning("규칙 검사 미등록 run=%s section=%s origin=%s — %s", run["id"], section_id, origin, e)
        return _record_store_failure(db, run, section_id, targets, m, cur, origin=origin, reason=str(e))
    _supersede(db, cur)
    aid = execution.create_attempt(
        db, run=run, stage=STAGE, unit_id=section_id, manifest=m, target_count=len(run_targets),
        supersedes=cur, retry_origin=None if cur is None else ("auto" if origin in ("translate", "backfill") else "user"),
        max_retry=MAX_RETRY, count_retry=False,
    )
    return Registration(REGISTERED, attempt_id=aid)


def _supersede(db: Session, cur: dict[str, Any] | None) -> None:
    """새 시도가 현재 시도를 대체할 때 진행 중이면 취소한다(늦은 결과는 lease 세대로 막힌다)."""
    if cur is not None and cur["status"] in ("pending", "running"):
        db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                        "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"), {"t": cur["id"]})


def _record_store_failure(db: Session, run: dict[str, Any], section_id: int, targets: list[dict[str, Any]], m: dict[str, Any],
                          cur: dict[str, Any] | None, *, origin: str, reason: str) -> Registration:
    """요청 객체를 저장하지 못한 입력의 실패를 현재 검사 시도 행으로 남긴다(REQUEST_STORE_FAILURE_CODE 참고).
    고정 명세(m)는 등록 때와 같다 — input_keys 는 실제 A1 키, request_ref 만 없다(실행할 수 없는 시도의 표지). 호출자 트랜잭션에서 만들고
    전달하지 않는다. 같은 입력의 재등록은 register 의 재사용 규칙이 저장 전에 막는다."""
    _supersede(db, cur)
    m = {**m, "registration_failure": {"stage": "request_store", "origin": origin}}
    aid = execution.create_attempt(
        db, run=run, stage=STAGE, unit_id=section_id, manifest=m, target_count=len(targets),
        supersedes=cur, retry_origin=None if cur is None else ("auto" if origin in ("translate", "backfill") else "user"),
        max_retry=MAX_RETRY, count_retry=False,
    )
    db.execute(text("UPDATE job_async_task SET status = 'failed', finished_at = now(), error_code = :c, error_message = :m "
                    "WHERE id = :t"), {"c": REQUEST_STORE_FAILURE_CODE, "m": reason[:4000], "t": aid})
    return Registration(UNRESOLVED_REQUEST_STORE, attempt_id=aid, reason=reason)


def ensure_check(db: Session, run: dict[str, Any], section_id: int, *, origin: str) -> int | None:
    """register 의 새 시도 id(새로 만들지 않았으면 None)."""
    r = register(db, run, section_id, origin=origin)
    return r.attempt_id if r.status == REGISTERED else None


def ensure_for_job(db: Session, job_id: int, section_id: int, *, origin: str) -> int | None:
    """N5 경로(번역문 수정 · 섹션 복원 · 확정 보조 등록)용. 진행 중인 하류 실행이 없으면 만들지 않는다."""
    run = execution.active_run(db, job_id, "downstream")
    if run is None:
        return None
    return ensure_check(db, execution.lock_task(db, run["id"]), section_id, origin=origin)


def after_translate(db: Session, run_id: int, section_id: int) -> list[int]:
    """번역 채택 · 재시도 채택 트랜잭션에서 실제 저장된 문구로 등록한다(등록 보장 ①②)."""
    aid = ensure_check(db, execution.lock_task(db, run_id), section_id, origin="translate")
    return [aid] if aid else []


def waiting(db: Session, run: dict[str, Any], section_id: int, attempt: dict[str, Any] | None) -> bool:
    """N4→N5 조건: 활성 규칙 검사가 끝났는지(채택 또는 최종 실패, 또는 등록할 수 없는 현재 입력 — failed 로 보인다). 검사 대상이 없으면 기다리지 않는다."""
    from app.flows import downstream

    if "rule" not in run_active_checks(run):
        return False
    rows = downstream._block_rows(db, section_id)
    targets = checkable(rows)
    if not targets:
        return False
    if attempt is not None:
        return attempt["status"] not in ("done", "failed")
    # 시도가 없다 — 현재 입력으로 검사 요청을 만들 수 없어 등록이 거부된 섹션(공급 실패 · 강제 용어 충돌)은 조회 · 확정과 같이
    # failed 로 보고 기다리지 않는다(PM 11-10 · be-handoff 8.2 「최종 실패면 N4 에서 막지 않고 N5 로」). 공급을 정할 수 없으면(대상 언어 없음)
    # 판단할 수 없으므로 기다린다(미해결 U1 · U2 와 같이 남김 — 진행 계산에서 예외를 내 번역 채택을 되돌리지 않는다)
    # 현재 입력의 검사 요청 · 키를 만들 수 없으면(U1 — 키 계산 불가 · 요청 조립 실패) 등록될 수 없으므로 기다리지 않는다(기록 없이 현재 입력으로
    # 판단 — 키가 없어 기록을 입력과 대조할 수 없다). N5 의 표시 · 확정은 미해결 U1(7.21). 저장소는 부르지 않는다(공급 조회 · 순수 함수만)
    try:
        _, glossary, _supplied = _current_input(db, run, section_id, rows, targets, origin="progress")
    except SupplyBasisUnresolved:
        return True
    except (InputKeysUnavailable, ai_adapters.AdapterUnavailable):
        return False
    return not _unregistrable(glossary)


# ---------------------------------------------------------------------------------------------------------
# 실행 · 채택
# ---------------------------------------------------------------------------------------------------------
def _same_input(rows: dict[int, dict[str, Any]], m: dict[str, Any]) -> list[int]:
    """고정 입력과 현재 문구(원문 · 번역문 · 제외)가 다른 대상 블록. revision · 배치 · 배율은 보지 않는다(contract 5.3)."""
    bad = []
    for t in m["targets"]:
        r = rows.get(t["block_id"])
        if (r is None or r["is_excluded"] is not False or r["trans_1"] is None or sha256_text(r["trans_1"]) != t["text_sha256"]
                or sha256_text(r["source_ko"] or "") != t["source_sha256"]):
            bad.append(t["block_id"])
    return bad


def result_block_keys(payload: Any) -> list[str] | None:
    """보고 payload 의 블록 결과 키 목록. 구조가 잘못돼 대상 집합을 판별할 수 없으면 None(예외를 내지 않는다).
    판별 불가: payload 가 객체가 아님 · blocks 가 없거나 배열이 아님 · 항목이 객체가 아님 · block_key 가 문자열이 아님."""
    if not isinstance(payload, dict):
        return None
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        return None
    keys = [b.get("block_key") for b in blocks]
    return keys if all(isinstance(k, str) for k in keys) else None


def incomplete_blocks(payload: dict[str, Any] | None, req: dict[str, Any]) -> bool:
    """완료 보고의 블록 결과가 요청 대상과 정확히 한 번씩 대응하지 않는가(누락 · 중복 · 미등록), 또는 구조가 잘못돼 그 대응을
    판별할 수 없는가. 완료가 아니면 False. 판별 불가도 보고 무결성 오류로 본다(사유 코드 CHECK_RESULT_INCOMPLETE 로 기록 — 상태는 failed, PM 11-10)."""
    if payload is None:
        return False
    keys = result_block_keys(payload)
    want = [t["block_key"] for t in req["targets"]]
    return keys is None or len(set(keys)) != len(keys) or sorted(keys) != sorted(want)


def execute_text_check(attempt_id: int) -> dict[str, Any]:
    from app.flows import downstream
    from pipeline.handoff.translation import run_text_check
    from pipeline.handoff.validate import validate_report

    def work(lease):
        m = lease.manifest
        body = load_request(lease.job_id, m["request_ref"])  # 믿을 수 없는 요청으로는 실행하지 않는다
        db = app_db.SessionLocal()
        try:
            bundle = _bundle_of_handoff(db, m["bundle_handoff_id"])
        finally:
            db.close()
        if bundle["sha256"] != m["bundle_sha256"]:
            raise AdoptionRejected("고정한 사전 묶음의 내용이 다르다", code="INPUT_CHANGED")
        # 등록 당시 요청을 그대로 재현한다 — 실행 직전 DB 값(revision · 번역문)으로 바꾸지 않는다.
        # 현재 입력과의 의미 유효성은 채택 때 inputKey 로 따로 대조한다.
        req = assemble(lease.run_id, body["request"], body["glossary"], bundle, attempt_id=str(attempt_id))
        req["expected_input_manifest_sha256"] = m["request_manifest_sha256"]
        rep = run_text_check(req)
        problems = validate_report(rep, req)
        if problems:
            # 보고 무결성 오류(대상 블록 결과 누락 · 중복 · 미등록)는 어디서 먼저 잡히든 같은 사유 코드로 남긴다(M7 — 보고는 채택하지 않고
            # 상태는 failed). 그 밖의 검증 실패는 기존대로 CHECK_RESULT_INVALID.
            code = "CHECK_RESULT_INCOMPLETE" if incomplete_blocks(rep.payload if rep.outcome == "completed" else None, req) else "CHECK_RESULT_INVALID"
            raise ai_adapters.AdapterFailed(code, "AI 보고 검증 실패: " + "; ".join(problems[:10]), retryable=True)
        meta = {"input_manifest_sha256": rep.input_manifest_sha256, "implementation": rep.implementation}
        if rep.outcome == "failed":
            f = rep.failure
            env = Envelope(outcome="failed", target_count=len(m["targets"]), input_fingerprint=lease.fingerprint,
                           payload={"error_code": f"TEXT_CHECK_{f.kind.upper()}", "message": f.message[:2000],
                                    "retryable": bool(f.retryable), "report": meta})
        elif rep.outcome == "skipped":
            env = Envelope(outcome="skipped", skip_reason="no_targets", target_count=0, input_fingerprint=lease.fingerprint,
                           payload={"text_check": rep.payload, "report": meta})
        else:
            env = Envelope(outcome="done", target_count=rep.target_count, input_fingerprint=lease.fingerprint,
                           impl_version=str(rep.implementation.get("rules") or rep.implementation)[:200],
                           payload={"text_check": rep.payload, "report": meta})
        return report(attempt_id, submit(lease, env))

    return downstream._guard(attempt_id, work)


def _flag_db(f: dict[str, Any], dict_ids: dict[str, int]) -> dict[str, Any]:
    """AI flag(원본 external_id · glossary id 문자열) → 저장 flag(DB id). contract 7.1.
    규칙 규제 flag 의 원판정값(`dictionary_verdict_status`)은 그대로 남긴다 — contract 7.1 「regulated · conditional 매칭은
    dictionary_verdict_status · 사전 ID · span 을 유지한다」. 공개 신호(check_signals)는 이 저장값만 보고 현재 사전 행을 다시 읽지 않는다."""
    out = {k: v for k, v in f.items() if k not in ("external_id", "glossary_id")}
    if "external_id" in f:
        if f["external_id"] not in dict_ids:
            raise AdoptionRejected(f"사전 항목 {f['external_id']} 의 DB id 없음", code="CHECK_RESULT_INVALID")
        out["dictionary_id"] = dict_ids[f["external_id"]]
    if "glossary_id" in f:
        try:
            out["glossary_id"] = int(f["glossary_id"])
        except (TypeError, ValueError) as e:
            raise AdoptionRejected(f"glossary_id {f['glossary_id']!r} 변환 실패", code="CHECK_RESULT_INVALID") from e
    return out


class TextCheckHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        p = json_value(h["payload"])
        m = ctx.attempt["input_manifest"]
        verify_request_for_adoption(ctx.attempt["job_id"], m["request_ref"])  # 본문 삭제 · 변조 뒤 도착한 보고는 채택하지 않는다
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "TEXT_CHECK_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        if h["outcome"] == "skipped":
            if m["targets"]:
                raise AdoptionRejected("검사 대상이 있는데 정상 생략", code="CHECK_RESULT_INCOMPLETE")
            return AdoptOutcome(status="done", skip_reason="no_targets")
        tc_payload = p.get("text_check")
        want = {block_key(t["block_id"]): t["block_id"] for t in m["targets"]}
        keys = result_block_keys(tc_payload)
        if keys is None or sorted(keys) != sorted(want) or len(set(keys)) != len(keys):  # M7 — 대상 블록 결과 완결성(판별 불가 포함)
            raise AdoptionRejected("검사 결과의 대상 블록 누락 · 중복 · 미등록(보고 전체 거절)", code="CHECK_RESULT_INCOMPLETE",
                                   retryable=True)  # validate_report 에서 먼저 잡힌 경우와 같게 재시도 후 같은 사유로 끝난다
        blocks = tc_payload["blocks"]
        rows = {r["id"]: dict(r) for r in db.execute(
            text("SELECT id, is_excluded, source_ko, trans_1, compliance_flags FROM text_block WHERE id = ANY(:ids) FOR UPDATE"),
            {"ids": list(want.values())}).mappings().all()}
        if bad := _same_input(rows, m):  # 현재 의미 입력 대조 — 다르면 보고 전체 거절, 부분 채택 없음
            raise AdoptionRejected(f"현재 입력과 다른 검사 결과 {bad}", code="INPUT_CHANGED")
        # 현재 inputKey 대조(job 잠금 안 — adopt 는 lock_chain 아래에서 불린다). A1 계산은 공급자만 부른다
        try:
            now_keys = current_keys(db, ctx.run, ctx.attempt["unit_id"], origin="adopt")
        except (SupplyBasisUnresolved, InputKeysUnavailable) as e:
            raise AdoptionRejected(f"현재 inputKey 를 정할 수 없다 — 채택하지 않는다: {e}", code="CHECK_KEY_UNAVAILABLE") from e
        fixed = m.get("input_keys") or {}
        if changed := sorted(k for k in want if fixed.get(k) is None or now_keys.get(k) != fixed.get(k)):
            raise AdoptionRejected(f"고정 inputKey 와 현재 inputKey 가 다르다 {changed}", code="INPUT_CHANGED")
        ext = sorted({f["external_id"] for b in blocks for f in b.get("flags") or [] if "external_id" in f})
        dict_ids = {r[0]: r[1] for r in db.execute(
            text("SELECT external_id, id FROM expression_dictionary WHERE external_id = ANY(:e)"), {"e": ext}).all()} if ext else {}
        written = {}
        for b in blocks:
            bid = want[b["block_key"]]
            new = [_flag_db(f, dict_ids) for f in b.get("flags") or []]
            keep = [f for f in (json_value(rows[bid]["compliance_flags"]) or []) if f.get("producer") not in OWN_PRODUCERS]
            db.execute(text("UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b"),
                       {"f": stable_json(keep + new), "b": bid})
            written[str(bid)] = {"input_key": (m.get("input_keys") or {}).get(b["block_key"]), "flags": len(new)}
        return AdoptOutcome(status="done", verify_result={"blocks": written})

    def after_done(self, db, att):
        from app.flows import downstream

        return downstream.progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        from app.flows import downstream

        run = db.execute(text("SELECT status FROM job_async_task WHERE id = :r"), {"r": att["parent_task_id"]}).scalar()
        if run == "running" and retryable and code not in INPUT_ERROR_CODES and att["retry_count"] < min(AUTO_RETRY, att["max_retry"]):
            return [execution.create_retry(db, att["id"], "auto")]
        return downstream.progress(db, att["parent_task_id"], att["unit_id"])  # 최종 실패 — 검사 실패로 남기고 N5 로(8.6)


execution.register_handler(STAGE, TextCheckHandler())


# ---------------------------------------------------------------------------------------------------------
# 블록 검사 상태 — 조회 · 확정 공통
# ---------------------------------------------------------------------------------------------------------
def decide(*, current_key: str, result_key: str | None, attempt_status: str | None, attempt_key: str | None,
           attempt_done_without_result: bool, attempt_error_code: str | None = None,
           current_unregistrable: bool = False) -> dict[str, Any]:
    """한 블록의 공개 검사 상태(OpenAPI BlockCheck). 순서가 의미를 가진다(PM 11-10: 셀러가 고칠 수 없는 검사 오류로 확정을 막지 않는다).
    - 정상 채택된 결과가 있고 그 키가 현재 키와 같을 때만 settled(키 일치만으로 settled 를 만들지 않는다 — M8). 오류 시도가 이를 무효화하지 않는다.
    - 현재 입력의 시도가 대기 · 실행 중이면 in_progress(보일 결과가 없으면 inputKey null) — 기존 대기 · 확정 조건 그대로.
    - 현재 입력(고정 키 = 현재 키)의 시도가 최종 실패면 사유와 무관하게 failed — 일반 실패, 공급 · 입력 · 데이터 오류(input_invalid ·
      bundle_invalid · 묶음 변환 실패), 보고 · 요청 무결성 오류(M7 — 채택 전 누락 · 중복 · 미등록 보고는 전체 거절), 채택 뒤 저장 기록 훼손 · 유실(done 인데 BE 채택 기록에
      그 블록 결과가 없음 — 검증기가 누락 보고를 받아들이는 경우가 아니다). 실패 보고는 flag 를 쓰지 않고
      채택하지 않으므로 「실패 표시」이지 보고를 믿는 것이 아니다. 대상 블록은 BE 가 고정한 시도 명세(input_keys)로 정한다 — 보고의 블록 ID 를 쓰지 않는다.
    - 시도가 없더라도 현재 입력으로 유효한 검사 요청을 만들 수 없으면(current_unregistrable — 공급 실패 · 실행하면 input_invalid 가 될
      강제 용어 충돌) failed. 조회 때 현재 입력으로 다시 판단하므로 과거 입력의 실패를 현재 실패로 보지 않는다. 공통 공급 실패는 섹션 전체,
      블록 국소 오류(강제 용어 · 금지/허용 같은 구간 충돌)는 그 블록만이다(11-11) — 같은 섹션의 정상 블록은 실제로 검사된다.
    - 그 밖에 이전 입력의 결과만 있으면 outdated, 아무것도 없으면 missing.
    - 채택 거절 사유가 '입력 변경 · 키 계산 불가'인 실패는 그 입력의 검사 결과가 아니다 — 시도가 없는 것으로 본다."""
    if attempt_status == "failed" and attempt_error_code in NOT_A_CHECK_RESULT:
        attempt_status, attempt_key = None, None
    if result_key is not None and result_key == current_key:
        return {"state": SETTLED, "inputKey": result_key}
    if attempt_status in ("pending", "running") and attempt_key == current_key:
        return {"state": IN_PROGRESS, "inputKey": result_key}
    if attempt_key == current_key and (attempt_status == "failed" or attempt_done_without_result):
        return {"state": FAILED, "inputKey": current_key}
    if current_unregistrable:
        return {"state": FAILED, "inputKey": current_key}
    if result_key is not None:
        return {"state": OUTDATED, "inputKey": result_key}
    return {"state": MISSING, "inputKey": None}


def _flag_signal_code(f: dict[str, Any]) -> str | None:
    producer, kind = f.get("producer"), f.get("type")
    if producer == "glossary_validation" and kind == "mandatory_term_unapplied":
        return SIGNAL_MANDATORY_TERM
    if producer == "translation_validation" and kind == "regulatory" and f.get("dictionary_verdict_status") in REGULATORY_SIGNAL_STATUSES:
        return SIGNAL_PROHIBITED
    return None


def check_signals(flags: Any, check: dict[str, Any] | None) -> list[dict[str, Any]]:
    """한 블록의 규칙 검사 공개 신호(ReviewSignal). 조회(GET /blocks)와 블록 저장 응답이 같은 함수를 쓴다(cfm._blocks).
    - 그 블록 검사 상태가 settled 일 때만 낸다. settled = 가장 최근 채택 결과의 inputKey 가 현재 입력 키와 같다(decide) — 채택이 그 블록의
      자기 producer flag 를 통째로 바꾸므로(TextCheckHandler.adopt) 이때 저장 flag 는 현재 입력의 결과다. in_progress · outdated · failed ·
      missing · 검사 대상 밖(null)에서는 남아 있는 이전 flag 를 신호로 내지 않는다.
    - 같은 블록 · 같은 코드는 1개로 묶는다(flag 여러 개 → 신호 1개). 사유 · 조문 · 근거 URL 은 null(계약 허용),
      taskId · retryable 은 translation_failed 전용이라 null.
    - 확정의 필수 확인 집합(final.REQUIRED_ACK_CODES = translation_failed)에는 들어가지 않는다 — final.block_signals 와 별개다."""
    if not check or check.get("state") != SETTLED:
        return []
    codes: list[str] = []
    for f in json_value(flags) or []:
        code = _flag_signal_code(f) if isinstance(f, dict) else None
        if code is not None and code not in codes:
            codes.append(code)
    return [{"code": c, "reason": None, "basisArticle": None, "evidenceUrl": None, "taskId": None, "retryable": None} for c in codes]


def _adopted_results(db: Session, run_id: int, section_id: int) -> dict[str, str]:
    """그 섹션에서 정상 채택된 검사 결과의 블록별 키(가장 최근 채택이 우선). {block_key: inputKey}."""
    out: dict[str, str] = {}
    for r in db.execute(
        text("SELECT a.input_manifest, h.payload FROM job_async_task a JOIN task_handoff h ON h.task_id = a.id AND h.state = 'adopted' "
             "WHERE a.parent_task_id = :r AND a.stage = 'text_check' AND a.unit_id = :s AND a.status = 'done' AND h.outcome = 'done' "
             "ORDER BY h.adopted_at DESC, h.id DESC"),
        {"r": run_id, "s": section_id},
    ).all():
        keys = (json_value(r[0]) or {}).get("input_keys") or {}
        for b in ((json_value(r[1]) or {}).get("text_check") or {}).get("blocks") or []:
            k = b.get("block_key")
            if k in keys and k not in out:
                out[k] = keys[k]
    return out


def section_states(db: Session, run: dict[str, Any], section_id: int) -> dict[int, dict[str, Any]]:
    """포함 섹션의 검사 대상 블록 상태. 현재 키는 InputKeys 로 계산한다(공급은 SupplyPolicy — 조회 시점의 현재 입력)."""
    from app.flows import downstream

    rows = downstream._block_rows(db, section_id)
    targets = checkable(rows)
    if not targets:
        return {}
    try:
        current, glossary, supplied = _current_input(db, run, section_id, rows, targets, origin="query")
    except (InputKeysUnavailable, ai_adapters.AdapterUnavailable):
        # U1(11-11 API 6-10): 현재 입력의 키를 계산할 수 없다(요청 조립 · 키 함수) — 시도 행 · 임의 키 없이 그 섹션 대상 블록을
        # failed · inputKey null 로 판정한다. 매번 현재 입력으로 다시 판정하므로 입력이 바뀌어 키를 계산할 수 있게 되면 일반 판정 · 등록으로
        # 돌아간다. null 은 다른 null 과 같은 입력을 뜻하지 않는다(재사용 판단에 쓰지 않음). DB 오류 등 다른 예외는 그대로 올린다
        # AI 집계(check_aggregate)의 not_comparable(키 없음)을 정상 비교 결과로 바꾸지 않는다 — BE 가 이 경우를 별도의 실패 종료 경계로 처리한다
        # (AI 11-11: 묶음 · 요청 형식 오류에서는 유효한 의미 키를 만들 수 없다 — 임의 해시 · manifest 지문으로 대신하지 않는다)
        return {r["id"]: {"state": FAILED, "inputKey": None} for r in targets}
    # 섹션 공통으로 검사 요청을 만들 수 없음(공통 공급 실패) — 시도 없이도 그 섹션 대상 블록은 failed(현재 키 기준).
    # 블록 국소 오류(11-11): 현재 공급 · 원문으로 다시 판단한 강제 용어 국소 충돌 블록(local_now)과, 현재 시도가 국소 실패로 고정한 블록
    # (고정 명세 local_failures — 금지/허용 같은 구간 충돌 포함, 고정 키 = 현재 키일 때만)만 failed. 같은 섹션의 정상 블록은 실제 검사 결과로 판정
    unregistrable = _unregistrable(glossary)
    local_now = set() if unregistrable else _glossary_local_failures(targets, glossary, supplied)
    results = _adopted_results(db, run["id"], section_id)
    cur = _current_attempt(db, run["id"], section_id)
    cur_keys = (cur["input_manifest"].get("input_keys") or {}) if cur else {}
    cur_done_blocks = set()
    if cur is not None and cur["status"] == "done":
        h = db.execute(text("SELECT payload FROM task_handoff WHERE task_id = :t AND state = 'adopted'"), {"t": cur["id"]}).scalar()
        cur_done_blocks = {b.get("block_key") for b in ((json_value(h) or {}).get("text_check") or {}).get("blocks") or []}
    cur_local = {block_key(b) for b in ((cur["input_manifest"] or {}).get("local_failures") or [])} if cur else set()
    out = {}
    for r in targets:
        k = block_key(r["id"])
        if k not in current:
            raise InputKeysUnavailable(f"{k} 의 현재 inputKey 없음")
        local = k in cur_local  # 이 시도가 실행하지 않고 그 입력의 실패로 고정한 블록 — 시도의 진행 · 결과와 무관하게 failed(키가 같을 때)
        out[r["id"]] = decide(
            current_key=current[k], result_key=results.get(k),
            attempt_status=("failed" if local else cur["status"]) if cur else None, attempt_key=cur_keys.get(k),
            # 채택 뒤 저장된 인계 기록의 훼손 · 유실 — 채택 경로는 결과 집합이 고정 대상과 정확히 같을 때만 채택하므로 정상 흐름에서는 생기지 않는다
            attempt_done_without_result=(not local and cur is not None and cur["status"] == "done" and k in cur_keys
                                         and k not in cur_done_blocks),
            attempt_error_code=None if local or cur is None else cur["error_code"],
            current_unregistrable=unregistrable or k in local_now,
        )
    return out


def job_block_states(db: Session, job_id: int) -> dict[int, dict[str, Any]] | None:
    """작업의 블록별 검사 상태. 활성 규칙 검사가 없으면 None(모든 블록 check=null).
    포함 섹션의 검사 대상 블록만 값을 갖는다(대상 밖은 null)."""
    run = execution.latest_run(db, job_id, "downstream")
    if run is None or "rule" not in run_active_checks(run):
        return None
    out: dict[int, dict[str, Any]] = {}
    for s in common.visible_sections(db, job_id, include_only=True):
        if s["id"] not in (json_value(run["run_scope"]) or {}).get("section_ids", []):
            continue
        out.update(section_states(db, run, s["id"]))
    return out
