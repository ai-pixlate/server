"""⑤ 브랜드 로고 제외 — 단독 개발 v1(2026-09-29). pipeline.md 단계표 ⑤ · 7.5절, open-questions.md #68.

정본(확정, 계약 2.3): `block_exact` — 블록 `source_ko` 전체와 `brand.name_ko` · `brand.name_en`을 정규화해 **완전 일치** 비교.
정규화 ① NFKC → ② 소문자 → ③ 공백 · 문장부호 제거, 정규화 결과가 빈 문자열인 값은 비교 대상에서 제외. 부분 일치 없음.
불일치는 경고가 아니다. ④ 라벨 블록은 비교도 건너뛴다(개발계획 2.1). 로고 파일 · 이미지 매칭은 MVP 이후.

단독 개발 방침(사용자 승인 2026-09-29, #68 — 운영 API/DB 계약 아님):
- 모델 호출 없음. 입력 = 원본 이미지 식별자 + 섹션의 ③ 블록(MergeResult) + 대응 ④ 결과(LabelResult) + 브랜드 자료 + 설정.
  이미지는 판정 입력이 아니다. 파일 읽기 · 저장은 실행 계층(run.py · 실측 드라이버)이 한다.
- 정규화 구현: NFKC → `str.lower()` → `str.isspace()` 공백과 Unicode 범주 P*(문장부호) 제거. S*(기호)는 남긴다.
  casefold · 악센트 제거 · 별칭 · 번역 · 음역 · OCR 오타 보정 · 부분 일치는 하지 않는다. `[logo] normalize`가 승인된 목록과 다르면 거부.
- 판정: ④ true → null(`product_label`, 비교 생략) / 정규화 텍스트 빈 문자열 → false(`empty_text`) /
  한글명 또는 영문명과 완전 일치 → true(`exact_match`) / 그 외 → false(`no_match`). 블록이 없으면 빈 목록으로 성공.
- 입력 검증(판정 전, 어기면 LogoInputError): ③ · ④ 섹션 키 · 블록 키 중복 · ④ ok · 블록마다 ④ 판정 정확히 하나(strict bool) ·
  ④ 입력 지문 = 현재 ③ 블록 지문 · 원본이 브랜드 자료의 정확히 한 그룹(명시된 images 목록, 접두사 추정 없음) ·
  자료 전체의 브랜드 상태와 값 형식 · 그룹 · 원본 중복과 not_in_input 상충 ·
  한글명 · 영문명 모두 provided이고 정규화 후 비어 있지 않음 · 승인된 정규화 설정.
  absent · unconfirmed · ④ 실패 · 누락 · NULL은 false로 바꾸지 않고 입력 오류로 거부한다 — v1 지원 범위이며 브랜드 부재의 운영 정책이 아니다.
- is_excluded는 계산하지 않는다(DB 자동계산, #63 미결). 원문 · 좌표 · 역할 · 원시 OCR · ④ 결과는 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any

from pipeline.stages.label import input_fingerprint
from pipeline.types import LabelResult, LogoDecision, LogoResult, MergeResult

NORMALIZE_STEPS = ["nfkc", "lower", "strip_space_punct"]  # 계약 2.3 순서. config [logo] normalize와 정확히 같아야 한다
NORMALIZE_RULE = "NFKC -> str.lower() -> str.isspace() 공백 및 Unicode 범주 P* 제거(S* 유지)"
BRAND_STATUSES = ("provided", "absent", "unconfirmed")  # brand-meta 자료의 값 상태(자료 value_rules). v1은 provided만 지원


class LogoInputError(ValueError):
    """⑤ 입력 · 설정 오류. 판정을 시작하지 않는다 — 실행 계층이 기록하고 종료 코드 2로 끝낸다. false로 대체하지 않는다."""


@dataclass(frozen=True)
class Brand:
    """브랜드 자료에서 원본 이미지에 연결된 그룹 하나. product_name은 추적용이며 비교값이 아니다."""

    image_group: str
    product_name: str | None
    name_ko: str
    name_en: str
    entry: dict[str, Any]  # 자료의 그룹 항목 원본(지문용)

    @property
    def normalized(self) -> dict[str, str]:
        return {"name_ko": normalize(self.name_ko), "name_en": normalize(self.name_en)}


# ---------------------------------------------------------------------------
# 설정 · 정규화
# ---------------------------------------------------------------------------
def validate_config(cfg: dict[str, Any]) -> None:
    """[logo] normalize가 승인된 순서 · 방식과 정확히 같아야 한다. 다르면 LogoInputError(설정 오류)."""
    if "logo" not in cfg or "normalize" not in cfg["logo"]:
        raise LogoInputError("config에 [logo] normalize가 없다")
    got = cfg["logo"]["normalize"]
    if got != NORMALIZE_STEPS:
        raise LogoInputError(f"logo.normalize={got!r} — 승인된 정규화 {NORMALIZE_STEPS}만 지원한다(#68)")


def normalize(text: str) -> str:
    """NFKC → str.lower() → 공백(str.isspace, 줄바꿈 포함) · 문장부호(Unicode 범주 P*) 제거. 기호(S*)는 남긴다."""
    lowered = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in lowered if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


def sha256_json(data: Any) -> str:
    """정렬한 키 · 공백 없는 JSON의 SHA-256(지문용)."""
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------
def _check_brand_value(where: str, value: Any, status: Any) -> list[str]:
    if status not in BRAND_STATUSES:
        return [f"{where}_status={status!r} — {BRAND_STATUSES} 중 하나"]
    if status == "provided":
        if not isinstance(value, str) or not value:
            return [f"{where}: provided인데 값이 비어 있지 않은 문자열이 아니다({value!r})"]
    elif value is not None:
        return [f"{where}: {status}이면 값은 null이어야 한다({value!r})"]
    return []


def not_in_input_images(brand_meta: dict[str, Any]) -> dict[str, str]:
    """브랜드 자료의 not_in_input.images(개발 입력본 밖으로 명시한 제외 원본 → 그룹). 없으면 빈 dict, 형식이 틀리면 LogoInputError.
    제외 원본은 허용하며 개발 대상과 겹치지 않는지만 본다(#68 수정 5)."""
    nii = brand_meta.get("not_in_input")
    if nii is None:
        return {}
    imgs = nii.get("images") if isinstance(nii, dict) else None
    if not isinstance(imgs, dict) or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in imgs.items()):
        raise LogoInputError("브랜드 자료 형식 오류: not_in_input.images가 {원본: 그룹} 문자열 dict가 아니다")
    return imgs


def resolve_brand(brand_meta: Any, image_id: str) -> Brand:
    """브랜드 자료 **전체**의 형식 · 상태와 값의 일관성, 그룹 · 원본 중복, not_in_input과 products[].images의 상충을 확인하고
    image_id가 명시된 그룹을 정확히 하나 찾는다(접두사 추정 없음). provided 제한(한글명 · 영문명 모두 provided, 정규화 후 비어 있지 않음)은
    연결된 그룹에만 적용한다(v1 지원 범위, #68 확정 4). 어기면 LogoInputError."""
    if not isinstance(brand_meta, dict) or not isinstance(brand_meta.get("products"), list) or not brand_meta["products"]:
        raise LogoInputError("브랜드 자료에 비어 있지 않은 products 목록이 없다")
    errors: list[str] = []
    for i, p in enumerate(brand_meta["products"]):
        if not isinstance(p, dict):
            errors.append(f"products[{i}] 객체가 아니다")
            continue
        g = p.get("image_group")
        if not isinstance(g, str) or not g:
            errors.append(f"products[{i}].image_group이 비어 있지 않은 문자열이 아니다")
        imgs = p.get("images")
        if not isinstance(imgs, list) or not all(isinstance(x, str) and x for x in imgs):
            errors.append(f"products[{i}].images가 문자열 목록이 아니다")
        for k in ("name_ko", "name_en"):
            if k not in p or f"{k}_status" not in p:
                errors.append(f"products[{i}]에 {k} · {k}_status가 없다")
            else:
                errors += _check_brand_value(f"products[{i}].{k}", p[k], p[f"{k}_status"])
    if errors:
        raise LogoInputError("브랜드 자료 형식 오류: " + "; ".join(errors))
    # 연결 상충은 연결된 그룹만이 아니라 자료 전체에서 검사한다(#68 확정 4)
    groups = [p["image_group"] for p in brand_meta["products"]]
    all_images = [x for p in brand_meta["products"] for x in p["images"]]
    dup_groups = sorted({g for g in groups if groups.count(g) > 1})
    dup_images = sorted({x for x in all_images if all_images.count(x) > 1})
    if dup_groups or dup_images:
        raise LogoInputError(f"브랜드 자료 연결 상충: 중복 그룹 {dup_groups} · 두 번 이상 연결된 원본 {dup_images}")
    excluded = not_in_input_images(brand_meta)
    overlap = sorted(set(excluded) & set(all_images))
    if overlap:
        raise LogoInputError(f"브랜드 자료 연결 상충: not_in_input 제외 원본이 products[].images에도 있다 {overlap}")
    hits = [p for p in brand_meta["products"] for x in p["images"] if x == image_id]
    if len(hits) != 1:
        raise LogoInputError(f"원본 {image_id!r}이 브랜드 자료의 images에 {len(hits)}번 연결돼 있다 — 정확히 한 그룹이어야 한다")
    p = hits[0]
    unsupported = [f"{k}={p[f'{k}_status']}" for k in ("name_ko", "name_en") if p[f"{k}_status"] != "provided"]
    if unsupported:
        raise LogoInputError(
            f"원본 {image_id} 그룹 {p['image_group']}: {', '.join(unsupported)} — ⑤ v1은 한글명 · 영문명이 모두 provided인 경우만 지원한다"
            "(false로 대체하지 않음. 브랜드 부재의 운영 정책은 정하지 않았다, #68)"
        )
    brand = Brand(image_group=p["image_group"], product_name=p.get("product_name"), name_ko=p["name_ko"], name_en=p["name_en"], entry=p)
    empty = [k for k, v in brand.normalized.items() if not v]
    if empty:
        raise LogoInputError(f"원본 {image_id} 그룹 {brand.image_group}: 정규화 후 빈 브랜드명 {empty} — 비교할 수 없다")
    return brand


def validate_inputs(image_id: str, merged: MergeResult, label: LabelResult, brand_meta: Any, cfg: dict[str, Any]) -> Brand:
    """판정 전 입력 검증(#68). 통과하면 연결된 브랜드를, 어기면 LogoInputError를 낸다. 입력은 바꾸지 않는다."""
    validate_config(cfg)
    if not isinstance(image_id, str) or not image_id:
        raise LogoInputError(f"원본 이미지 식별자가 비어 있다: {image_id!r}")
    key = merged.section_key
    if label.section_key != key:
        raise LogoInputError(f"section_key 불일치: ③ {key} · ④ {label.section_key}")
    other = [b.block_key for b in merged.blocks if b.section_key != key]
    if other:
        raise LogoInputError(f"③ 블록의 section_key가 섹션 {key}와 다르다: {other}")
    keys = [b.block_key for b in merged.blocks]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise LogoInputError(f"{key}: 같은 block_key가 있다 {dup}")
    if label.status != "ok" or label.labels is None:
        raise LogoInputError(f"{key}: ④ 결과가 ok가 아니다(status={label.status}) — 미판정을 false로 바꾸지 않는다")
    lkeys = [d.block_key for d in label.labels]
    ldup = sorted({k for k in lkeys if lkeys.count(k) > 1})
    missing = sorted(set(keys) - set(lkeys))
    extra = sorted(set(lkeys) - set(keys))
    not_bool = [d.block_key for d in label.labels if type(d.is_product_label) is not bool]
    problems = []
    if ldup:
        problems.append(f"④ 판정 중복 {ldup}")
    if missing:
        problems.append(f"④ 판정 누락 {missing}")
    if extra:
        problems.append(f"③에 없는 블록의 ④ 판정 {extra}")
    if not_bool:
        problems.append(f"④ 판정이 boolean이 아니다 {not_bool}")
    if problems:
        raise LogoInputError(f"{key}: " + "; ".join(problems))
    fp = input_fingerprint(merged.blocks)
    if label.checked.input_fingerprint != fp:
        raise LogoInputError(f"{key}: ④ 입력 지문 {label.checked.input_fingerprint[:12]}…이 현재 ③ 블록 지문 {fp[:12]}…과 다르다 — ④ 재실행 대상")
    return resolve_brand(brand_meta, image_id)


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def run(image_id: str, merged: MergeResult, label: LabelResult, brand_meta: Any, cfg: dict[str, Any]) -> tuple[LogoResult, dict[str, Any]]:
    """⑤ 한 섹션. (LogoResult, 기록) — 기록은 실행 계층이 logo_debug/<section_key>.json으로 쓴다.
    입력 오류는 LogoInputError로 전달한다(결과를 만들지 않음). 판정 중 예기치 않은 오류는 그대로 올라가며 실행 계층이 failed 결과를 만든다."""
    brand = validate_inputs(image_id, merged, label, brand_meta, cfg)
    names = brand.normalized
    by_label = {d.block_key: d.is_product_label for d in label.labels}
    ordered = sorted(merged.blocks, key=lambda b: (b.block_order, b.block_key))
    decisions: list[LogoDecision] = []
    rows: list[dict[str, Any]] = []
    for b in ordered:
        is_label = by_label[b.block_key]
        norm: str | None = None
        matched: list[str] = []
        if is_label:
            d = LogoDecision(block_key=b.block_key, is_brand_logo=None, basis="product_label")
        else:
            norm = normalize(b.source_ko)
            if not norm:  # 빈 문자열끼리는 비교하지 않는다
                d = LogoDecision(block_key=b.block_key, is_brand_logo=False, basis="empty_text")
            else:
                matched = [k for k, v in names.items() if norm == v]
                d = LogoDecision(block_key=b.block_key, is_brand_logo=bool(matched),
                                 basis="exact_match" if matched else "no_match")
        decisions.append(d)
        rows.append({"block_key": b.block_key, "block_order": b.block_order, "is_product_label": is_label,
                     "normalized": norm, "matched_names": matched, "is_brand_logo": d.is_brand_logo, "basis": d.basis})
    result = LogoResult(image_id=image_id, section_key=merged.section_key, status="ok", decisions=decisions)
    counts = {basis: sum(1 for d in decisions if d.basis == basis) for basis in ("product_label", "empty_text", "exact_match", "no_match")}
    record = {
        "stage": "logo", "image_id": image_id, "section_key": merged.section_key, "status": "ok", "error": None,
        "normalize": list(cfg["logo"]["normalize"]), "normalize_rule": NORMALIZE_RULE, "unicode_version": unicodedata.unidata_version,
        "blocks_fingerprint": input_fingerprint(merged.blocks),
        "label_fingerprint": sha256_json(label.model_dump(mode="json")),
        "label_input_fingerprint": label.checked.input_fingerprint,
        "brand": {"image_group": brand.image_group, "product_name": brand.product_name, "name_ko": brand.name_ko, "name_en": brand.name_en,
                  "name_ko_normalized": names["name_ko"], "name_en_normalized": names["name_en"]},
        "brand_entry_fingerprint": sha256_json(brand.entry),
        "blocks": len(ordered), "counts": {**counts, "compared": counts["empty_text"] + counts["exact_match"] + counts["no_match"]},
        "decisions": rows,
    }
    return result, record


def failed_result(image_id: str, section_key: str, error: str) -> LogoResult:
    """판정 중 예기치 않은 오류의 섹션 결과(decisions=None). 실행 계층이 기록한 뒤 전체 실행을 멈춘다."""
    return LogoResult(image_id=image_id, section_key=section_key, status="failed", decisions=None, error=error or "알 수 없는 오류")
