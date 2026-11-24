"""A+ 슬롯 내용 검사 — 개발용 v1(운영 계약 아님).

`types.AplusModule` 한 장이 규격 데이터(spec.json)의 템플릿 규칙에 맞는지 본다.
- `validate_module`: 필수 칸 · 그 템플릿이 쓰지 않는 칸 · 반복 칸 개수와 칸별 필수 · 배치 변형 · 모드. 어긴 항목을 문장 목록으로 돌려준다(빈 목록 = 통과).
  규칙은 참고 렌더러(seed-builder PR #3 `renderer/spec.py` `validate`)와 같고, 하나만 더 엄격하다 — 템플릿이 그리지 않는 칸에
  값이 있으면 오류로 본다(참고 렌더러는 조용히 무시한다. 채운 문구가 결과에 보이지 않는 것을 놓치지 않기 위해서다).
- `limit_excesses`: 템플릿 한도를 넘은 글 칸. **자르거나 거절하지 않고 표시만 한다** — 문장을 자동으로 줄이거나 자르지 않는 것은
  조판 계약과 같다(contract.md 6.4). 한도가 자료에 없는 칸은 `max_chars=None`으로 따로 돌려준다(#95).
- `guide_excesses`: 가이드 상한(넘으면 안 되는 바깥 한계, PM 결정 2026-11-24)을 넘은 글 칸. 템플릿 한도는 가이드 상한보다 작거나 같으므로
  템플릿 한도 안이면 이것도 지킨다. 대응 필드가 없는 칸(자체 배치 · label · subheadline)은 검사하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.aplus.spec import AplusSpec
from pipeline.types import AplusModule, AplusText

TEXT_FIELDS = ("label", "headline", "subheadline", "body")
ITEM_TEXT_FIELDS = ("title", "body")


def _has(value: object) -> bool:
    return value is not None and value != []


def validate_module(m: AplusModule, spec: AplusSpec) -> list[str]:
    """어긴 규칙의 문장 목록(빈 목록 = 통과). 값을 고치지 않는다."""
    if m.template not in spec.template_ids:
        return [f"없는 템플릿 {m.template!r} — 가능: {', '.join(spec.template_ids)}"]
    tid = m.template
    t = spec.template(tid)
    errs: list[str] = []
    if m.layout not in t["layouts"]:
        errs.append(f"layout {m.layout!r}은 {tid}에서 쓸 수 없다 — 가능: {', '.join(t['layouts'])}")
    modes = {v["when"]["mode"] for v in t["variants"] if "mode" in v["when"]}
    if m.mode is not None and m.mode not in modes:
        errs.append(f"mode {m.mode!r}은 {tid}에서 쓸 수 없다")
    if m.layout in spec.forbidden_layouts(tid, mode=m.mode):
        errs.append(f"{tid} mode {m.mode!r}에는 layout {m.layout!r}을 쓸 수 없다")
    if errs:  # 변형이 틀리면 칸 규칙을 고를 수 없다
        return errs

    req = spec.requirements(tid, layout=m.layout, mode=m.mode)
    for name in req.required:
        if not _has(getattr(m, name)):
            errs.append(f"필수 칸 없음: {name}")
    for name in ("label", "headline", "subheadline", "body", "image", "mark", "items"):
        if _has(getattr(m, name)) and name not in req.allowed:
            why = "이 배치 변형에서는" if name in t["required"] + t["optional"] else "이 템플릿은"
            errs.append(f"{why} 쓰지 않는 칸에 값이 있다: {name}")
    if req.items is not None and m.items:
        rule = req.items
        if not rule.min <= len(m.items) <= rule.max:
            errs.append(f"items 개수 {len(m.items)}개 — {tid}는 {rule.min}~{rule.max}개")
        for i, it in enumerate(m.items):
            for need in rule.required:
                if not any(_has(getattr(it, x)) for x in need.split("|")):
                    errs.append(f"items[{i}]에 {need.replace('|', ' 또는 ')} 없음")
            for name in ("title", "body", "image", "color"):
                if _has(getattr(it, name)) and name not in rule.fields:
                    errs.append(f"items[{i}]: 이 템플릿은 쓰지 않는 칸에 값이 있다: {name}")
    return errs


@dataclass(frozen=True)
class LimitCheck:
    """글 칸 하나의 글자 수와 한도. `field`는 `headline` 또는 `items[2].title` 형식. `max_chars`(템플릿 한도)가 None이면 자료에 한도가 없다.
    `guide_max_chars`(가이드 칸당 상한)가 None이면 대응하는 아마존 모듈 필드가 없다."""

    field: str
    chars: int
    max_chars: int | None
    origin_block_keys: tuple[str, ...]
    guide_max_chars: int | None = None

    @property
    def over(self) -> int:
        return max(0, self.chars - self.max_chars) if self.max_chars is not None else 0

    @property
    def over_guide(self) -> int:
        return max(0, self.chars - self.guide_max_chars) if self.guide_max_chars is not None else 0


def count_chars(text: str) -> int:
    """한도 비교에 쓰는 글자 수 — 공백을 포함한 Unicode 코드 포인트 수(참고 자료의 '영어 기준 · 공백 포함')."""
    return len(text)


def limit_checks(m: AplusModule, spec: AplusSpec) -> list[LimitCheck]:
    """값이 있는 모든 글 칸의 글자 수와 한도. 템플릿이 없는 값이면 KeyError(먼저 validate_module)."""
    n = len(m.items)
    out: list[LimitCheck] = []

    def add(field: str, limit_field: str, value: AplusText) -> None:
        out.append(LimitCheck(field, count_chars(value.text), spec.char_limit(m.template, limit_field, items=n, layout=m.layout, mode=m.mode),
                              tuple(value.origin_block_keys), spec.guide_limit(m.template, limit_field)))

    for name in TEXT_FIELDS:
        value = getattr(m, name)
        if value is not None:
            add(name, name, value)
    for i, it in enumerate(m.items):
        for name in ITEM_TEXT_FIELDS:
            value = getattr(it, name)
            if value is not None:
                add(f"items[{i}].{name}", f"items.{name}", value)
    return out


def limit_excesses(m: AplusModule, spec: AplusSpec) -> list[LimitCheck]:
    """템플릿 한도를 넘은 글 칸만. 표시용이며 문구를 자르지 않는다."""
    return [c for c in limit_checks(m, spec) if c.over > 0]


def guide_excesses(m: AplusModule, spec: AplusSpec) -> list[LimitCheck]:
    """가이드 상한(바깥 한계)을 넘은 글 칸만. 표시용이며 문구를 자르지 않는다."""
    return [c for c in limit_checks(m, spec) if c.over_guide > 0]
