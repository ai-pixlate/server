"""A+ 7칸 구성 — 칸별 템플릿 · 블록 · 사진 자리 선택과 슬롯 채우기. 개발용 v1(2026-11-23, 운영 계약 아님. open-questions.md 2.9절 · #94).

**선택은 모델, 문구와 한도는 코드**(사용자 결정 2026-11-23):
- 모델은 상품 하나의 섹션 전체를 1회 호출로 보고, 칸마다 템플릿(A/B) · 배치 변형 · 각 칸에 넣을 **1차 번역 블록의 ID** · 사진을 자를 자리만 고른다.
  문구를 쓰지 않는다.
- 코드는 고른 블록의 번역문을 **그대로** 이어 붙인다. 블록은 원래 순서(섹션 순서 → `block_order`)로 넣고, 다음 블록을 더하면 글자 수 한도를
  넘을 때 그 앞에서 멈춘다. 첫 블록이 이미 넘으면 그 블록 하나만 넣고 한도 초과로 남긴다(자르지 않는다 — 압축은 별도 과업).
- 블록 역할마다 들어갈 수 있는 칸이 정해져 있다(규격 데이터 `block_roles` — PM 확정 2026-11-24): `title` → `headline` · 항목 제목,
  `body` → `body` · 항목 설명, `caption` → 1A · 3A의 `subheadline`과 사진 있는 항목형(4B · 5B · 6A · 3B · 7B · 5A)의 항목 칸 —
  그 항목에 `title` 블록이 없으면 항목 제목, 있으면 항목 설명(데이터 담당 답변 2026-11-24). `price` · `caution` → 없음. 그 칸의 템플릿 어디에도 들어갈 수 없는
  역할의 블록은 모델에게 보내지 않고(응답이 그 ID를 가리키면 보낸 적 없는 ID로 빠진다), 보낸 블록도 역할이 그 칸에 맞지 않으면 뺀다.
- `label` 칸은 블록에서 가져오지 않는다. 고정 문구 템플릿(3A · 7A)은 규격 데이터의 값을 코드가 넣고, 1A(브랜드명)는 공급원이 정해질 때까지
  비운다(#94).
- 코드가 바로잡는 것(참고 변환기 `apply_rules`와 같은 취지): 그 칸 라벨의 섹션이 아닌 블록 · 이미 다른 곳에 쓴 블록 · 보낸 적 없는 ID는 빼고
  `dropped`에 남긴다. 4A · 4B 사진은 통째로(`contain`). 필수 큰 사진이 비면 다른 칸의 큰 사진을 다시 쓰고 확인 표시를 남긴다.
  누끼 여부(`cutout`)도 모델이 사진마다 판단한다(사용자 결정 2026-11-24, #98). 칸을 꽉 채우는 사진(`cover`)의 누끼 요청은 뺀다.
  누끼 자체는 `pipeline/aplus/cutout.py`가 한다.
  좌우 번갈아 배치는 참고 변환기 `zigzag.py`의 규칙이다.
- 바로잡은 뒤에도 템플릿 규칙을 어기면 그 칸의 `errors`에 남긴다(다른 템플릿으로 바꾸거나 내용을 지어내지 않는다).

블록을 한 칸 안에서 잇는 구분자(공백 한 칸)와 블록 안 줄바꿈을 공백으로 바꾸는 것은 잠정이다(#94).
실패(호출 · 시간 초과 · 응답 구조)는 예외가 아니라 `status=failed` 결과다. 입력 오류는 예외다.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from pipeline.aplus import content
from pipeline.aplus.label import Recorder, _emit, llm_config, prepare_image, slot_candidates, validate_config
from pipeline.aplus.spec import ITEM_RULE, SLOTS, AplusSpec
from pipeline.types import (AplusDropped, AplusImage, AplusItem, AplusModule, AplusPlan, AplusPlanChecked, AplusSectionLabel, AplusSlotPlan,
                            AplusText, BBox, Section)
from pipeline.vlm import GeminiMultiImageAssistant, LlmReply, MultiImageAssistant, VlmError, image_sha256, sha256_text

TABLE = "aplus_plan"
TEXT_FIELDS = ("headline", "subheadline", "body")  # 모델이 블록을 고르는 칸. label은 블록에서 오지 않는다
ONE_SIDED = ("1A", "2A", "2B", "3A", "3B", "4A", "6B", "7A")  # 사진이 한쪽에 있는 템플릿(참고 변환기 zigzag.py)
CONTAIN_ONLY = ("4A", "4B")  # 근거 그림은 일부를 확대하지 않고 통째로
SEPARATOR = " "


def usable_role(role: str, slot: int, spec: AplusSpec) -> bool:
    """그 역할의 블록이 그 칸의 템플릿 어느 하나에서라도 들어갈 칸이 있는가(규격 데이터 `block_roles`)."""
    return any(spec.role_fields(role, tid) for tid in spec.templates_for_slot(slot))


@dataclass(frozen=True)
class TranslatedBlock:
    """1차 번역 블록. 제품 라벨 · 로고 · 공백 · 번역 실패 블록은 호출자가 넣지 않는다. 그 칸에 들어갈 수 없는 역할은 넣어도 재료에서 빠진다."""

    block_key: str
    section_key: str
    block_order: int
    role: str
    text: str


# ---------------------------------------------------------------------------
# 페이로드 · 응답 스키마
# ---------------------------------------------------------------------------
def _ids_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _image_schema() -> dict[str, Any]:
    return {"type": "object", "nullable": True,
            "properties": {"section": {"type": "string"}, "box_2d": {"type": "array", "items": {"type": "integer"}},
                           "fit": {"type": "string", "enum": ["contain", "cover"]}, "cutout": {"type": "boolean"}},
            "required": ["section", "box_2d", "fit", "cutout"]}


def response_schema() -> dict[str, Any]:
    item = {"type": "object",
            "properties": {"title": _ids_schema(), "body": _ids_schema(), "image": _image_schema(), "color": {"type": "string", "nullable": True}},
            "required": ["title", "body"]}
    slot = {"type": "object",
            "properties": {"slot": {"type": "integer"}, "template": {"type": "string", "nullable": True}, "layout": {"type": "string"},
                           "mode": {"type": "string", "nullable": True}, "why": {"type": "string"},
                           **{f: _ids_schema() for f in TEXT_FIELDS}, "image": _image_schema(), "mark": _image_schema(),
                           "items": {"type": "array", "items": item}},
            "required": ["slot", "template", "why", *TEXT_FIELDS, "items"]}
    return {"type": "object", "properties": {"slots": {"type": "array", "items": slot}}, "required": ["slots"]}


def normalize_text(text: str) -> str:
    """블록 번역문 안의 줄바꿈 · 연속 공백을 공백 한 칸으로(원문 줄바꿈은 한국어 배치의 것이라 옮기지 않는다 — 잠정 #94)."""
    return " ".join(text.split())


def build_payload(sections: list[Section], labels: list[AplusSectionLabel], blocks: list[TranslatedBlock],
                  spec: AplusSpec) -> tuple[dict[str, Any], dict[str, str], dict[str, TranslatedBlock]]:
    """보낼 페이로드, 임시 섹션 ID → section_key, 임시 블록 ID → 블록."""
    sid_of = {s.section_key: f"s{i + 1}" for i, s in enumerate(sections)}
    label_of = {x.section_key: x.label for x in labels}
    cands = slot_candidates(labels, spec)
    by_section: dict[str, list[TranslatedBlock]] = {}
    for b in sorted(blocks, key=lambda b: b.block_order):
        by_section.setdefault(b.section_key, []).append(b)
    block_ids: dict[str, TranslatedBlock] = {}
    secs = []
    for s in sections:
        lab = label_of[s.section_key]
        slot = spec.slot_of_label(lab)
        entry: dict[str, Any] = {"id": sid_of[s.section_key], "label": lab, "slot": slot,
                                 "photo_ok": not spec.raw["labels"][lab]["photo_banned"]}
        if slot is not None:
            entry["blocks"] = []
            for b in by_section.get(s.section_key, []):
                if not usable_role(b.role, slot, spec):
                    continue
                bid = f"{sid_of[s.section_key]}.b{b.block_order}"
                block_ids[bid] = b
                t = normalize_text(b.text)
                entry["blocks"].append({"id": bid, "role": b.role, "text": t, "chars": content.count_chars(t)})
        secs.append(entry)
    templates = {tid: {k: t[k] for k in ("slot", "required", "optional", "items", "layouts", "variants", "char_limits")}
                 for tid, t in spec.raw["templates"].items()}
    payload = {
        "slots": [{"slot": n, "name": spec.raw["slots"][str(n)]["name_ko"], "templates": list(spec.templates_for_slot(n)),
                   "candidates": [sid_of[k] for k in cands[n]]} for n in SLOTS],
        "templates": templates,
        "role_fields": {role: {"fields": r["fields"], "by_template": r["by_template"], "item_rule": r["item_rule"]}
                        for role, r in spec.raw["block_roles"]["roles"].items() if r["fields"] or r["by_template"]},
        "sections": secs,
    }
    return payload, {v: k for k, v in sid_of.items()}, block_ids


# ---------------------------------------------------------------------------
# 응답 구조 검증 — 구조가 틀리면 전체 실패. 내용(없는 ID · 칸 밖 블록 등)은 compose가 바로잡고 기록한다
# ---------------------------------------------------------------------------
class AplusPlanResponseError(VlmError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("A+ 7칸 구성 응답 검증 실패: " + "; ".join(reasons))


def _is_ids(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _check_image(v: Any, where: str, reasons: list[str]) -> None:
    if v is None:
        return
    if not isinstance(v, dict) or not isinstance(v.get("section"), str) or v.get("fit") not in ("contain", "cover", None):
        reasons.append(f"{where} 형식이 잘못됐다")
        return
    box = v.get("box_2d")
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(n, int) and not isinstance(n, bool) for n in box)):
        reasons.append(f"{where}.box_2d가 정수 4개가 아니다: {box!r}")
    if not isinstance(v.get("cutout", False), bool):
        reasons.append(f"{where}.cutout이 true · false가 아니다: {v.get('cutout')!r}")


def parse_plan(text: str) -> dict[int, dict[str, Any]]:
    """칸 번호 → 응답 항목(1~7 전부). 구조가 틀리면 AplusPlanResponseError."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AplusPlanResponseError([f"JSON 아님: {e}"]) from e
    if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
        raise AplusPlanResponseError(["최상위 slots 배열이 없다"])
    reasons: list[str] = []
    out: dict[int, dict[str, Any]] = {}
    for i, raw in enumerate(data["slots"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("slot"), int) or isinstance(raw.get("slot"), bool) or raw["slot"] not in SLOTS:
            reasons.append(f"slots[{i}] 칸 번호가 1~7이 아니다")
            continue
        n = raw["slot"]
        if n in out:
            reasons.append(f"칸 {n} 중복")
            continue
        where = f"칸 {n}"
        if raw.get("template") is not None and not isinstance(raw["template"], str):
            reasons.append(f"{where} template이 문자열 · null이 아니다")
        for f in TEXT_FIELDS:
            if not _is_ids(raw.get(f, [])):
                reasons.append(f"{where} {f}가 블록 ID 배열이 아니다")
        _check_image(raw.get("image"), f"{where} image", reasons)
        _check_image(raw.get("mark"), f"{where} mark", reasons)
        items = raw.get("items", [])
        if not isinstance(items, list):
            reasons.append(f"{where} items가 배열이 아니다")
            items = []
        for j, it in enumerate(items):
            if not isinstance(it, dict) or not _is_ids(it.get("title", [])) or not _is_ids(it.get("body", [])):
                reasons.append(f"{where} items[{j}] 형식이 잘못됐다")
                continue
            _check_image(it.get("image"), f"{where} items[{j}].image", reasons)
            if it.get("color") is not None and not isinstance(it["color"], str):
                reasons.append(f"{where} items[{j}].color가 문자열이 아니다")
        out[n] = raw
    missing = [n for n in SLOTS if n not in out]
    if missing:
        reasons.append(f"칸 누락 {missing}")
    if reasons:
        raise AplusPlanResponseError(reasons)
    return out


# ---------------------------------------------------------------------------
# 채우기 — 코드가 순서 · 한도 · 재료 규칙을 강제한다
# ---------------------------------------------------------------------------
class _Composer:
    def __init__(self, sections: list[Section], labels: list[AplusSectionLabel], block_ids: dict[str, TranslatedBlock],
                 sid_to_key: dict[str, str], spec: AplusSpec) -> None:
        self.spec = spec
        self.sections = {s.section_key: s for s in sections}
        self.order = {s.section_key: i for i, s in enumerate(sections)}
        self.label_of = {x.section_key: x.label for x in labels}
        self.cands = slot_candidates(labels, spec)
        self.block_ids = block_ids
        self.sid_to_key = sid_to_key
        self.used: dict[str, str] = {}  # block_key → "칸 n field"

    def _fits(self, b: TranslatedBlock, tid: str, limit_field: str, item_has_title: bool | None) -> bool:
        """그 블록의 역할이 그 칸에 들어갈 수 있는가. 항목 조건이 있는 역할(caption)은 그 항목에 title 블록이 없을 때만 항목 제목,
        있을 때만 항목 설명에 들어간다."""
        if limit_field not in self.spec.role_fields(b.role, tid):
            return False
        if item_has_title is not None and self.spec.item_rule(b.role) == ITEM_RULE:
            return limit_field == ("items.body" if item_has_title else "items.title")
        return True

    def _item_has_title(self, n: int, tid: str, ids: list[str]) -> bool:
        """그 항목의 제목에 (항목 조건이 없는) title 역할 블록이 실제로 들어갈 수 있는가."""
        return any(self.spec.item_rule(b.role) is None and "items.title" in self.spec.role_fields(b.role, tid)
                   and b.section_key in self.cands[n] and b.block_key not in self.used
                   for b in (self.block_ids[x] for x in ids if x in self.block_ids))

    def _text(self, n: int, tid: str, field: str, limit_field: str, ids: list[str], *, items: int, layout: str, mode: str | None,
              dropped: list[AplusDropped], flags: list[str], item_has_title: bool | None = None) -> AplusText | None:
        unknown = [x for x in ids if x not in self.block_ids]
        if unknown:
            dropped.append(AplusDropped(field=field, block_keys=unknown, reason="unknown"))
        picked = list(dict.fromkeys(self.block_ids[x] for x in ids if x in self.block_ids))
        outside = [b for b in picked if b.section_key not in self.cands[n]]
        if outside:
            dropped.append(AplusDropped(field=field, block_keys=[b.block_key for b in outside], reason="not_candidate"))
        reused = [b for b in picked if b not in outside and b.block_key in self.used]
        if reused:
            dropped.append(AplusDropped(field=field, block_keys=[b.block_key for b in reused], reason="already_used"))
        wrong = [b for b in picked if b not in outside and b not in reused and not self._fits(b, tid, limit_field, item_has_title)]
        if wrong:
            dropped.append(AplusDropped(field=field, block_keys=[b.block_key for b in wrong], reason="role"))
        rest = [b for b in picked if b not in outside and b not in reused and b not in wrong]
        ordered = sorted(rest, key=lambda b: (self.order[b.section_key], b.block_order))
        if ordered != rest:
            flags.append(f"[규칙] {field}: 블록을 원래 순서로 다시 놓음")
        limit = self.spec.char_limit(tid, limit_field, items=items, layout=layout, mode=mode)
        parts: list[str] = []
        kept: list[TranslatedBlock] = []
        for i, b in enumerate(ordered):
            t = normalize_text(b.text)
            if kept and limit is not None and content.count_chars(SEPARATOR.join(parts + [t])) > limit:
                dropped.append(AplusDropped(field=field, block_keys=[x.block_key for x in ordered[i:]], reason="limit"))
                break
            parts.append(t)
            kept.append(b)
        if not kept:
            return None
        for b in kept:
            self.used[b.block_key] = f"칸 {n} {field}"
        return AplusText(text=SEPARATOR.join(parts), origin_block_keys=[b.block_key for b in kept])

    def _image(self, raw: Any, tid: str, what: str, flags: list[str], *, default_fit: str) -> AplusImage | None:
        if raw is None:
            return None
        key = self.sid_to_key.get(raw["section"])
        if key is None:
            flags.append(f"[규칙] {what}: 없는 섹션 {raw['section']!r} — 사진을 넣지 않음")
            return None
        s = self.sections[key]
        y0, x0, y1, x1 = (max(0, min(1000, v)) for v in raw["box_2d"])
        box = BBox(x=round(x0 * s.width / 1000), y=round(y0 * s.height / 1000),
                   w=round(x1 * s.width / 1000) - round(x0 * s.width / 1000), h=round(y1 * s.height / 1000) - round(y0 * s.height / 1000))
        if box.w <= 0 or box.h <= 0:
            flags.append(f"[규칙] {what}: 자를 자리의 넓이가 0 — 사진을 넣지 않음")
            return None
        if self.spec.raw["labels"][self.label_of[key]]["photo_banned"]:
            flags.append(f"[확인] {what}이 {key}({self.label_of[key]}) 섹션에서 옴 — 사진을 가져오지 않는 라벨")
        fit = raw.get("fit") or default_fit
        if tid in CONTAIN_ONLY and fit != "contain":
            fit = "contain"
            flags.append(f"[규칙] {what}: 근거 그림은 통째로(contain)")
        cutout = bool(raw.get("cutout", False))
        if cutout and fit != "contain":
            cutout = False
            flags.append(f"[규칙] {what}: 칸을 꽉 채우는 사진(cover)은 누끼하지 않음")
        return AplusImage(section_key=key, bbox=box, fit=fit, cutout=cutout)

    def slot(self, n: int, raw: dict[str, Any]) -> AplusSlotPlan:
        why = raw.get("why") if isinstance(raw.get("why"), str) else ""
        tid = raw.get("template") or None
        flags: list[str] = []
        if tid is None:
            return AplusSlotPlan(slot=n, template=None, why=why, module=None)
        if tid not in self.spec.templates_for_slot(n):
            return AplusSlotPlan(slot=n, template=None, why=why, module=None,
                                 flags=[f"[규칙] 칸 {n}에 쓸 수 없는 템플릿 {tid!r} — 비움"])
        t = self.spec.template(tid)
        layout = raw.get("layout") or "default"
        if layout not in t["layouts"]:
            flags.append(f"[규칙] layout {layout!r}은 {tid}에서 쓸 수 없어 default로 바꿈")
            layout = "default"
        mode = raw.get("mode") or None
        if mode is not None and mode not in {v["when"]["mode"] for v in t["variants"] if "mode" in v["when"]}:
            flags.append(f"[규칙] mode {mode!r}은 {tid}에서 쓸 수 없어 뺌")
            mode = None
        req = self.spec.requirements(tid, layout=layout, mode=mode)
        dropped: list[AplusDropped] = []
        raw_items = [it for it in raw.get("items", []) if it.get("title") or it.get("body") or it.get("image") or it.get("color")]
        if req.items is None and raw_items:
            flags.append(f"[규칙] {tid}{'' if layout == 'default' else ' ' + layout}는 반복 칸을 쓰지 않아 items를 뺌")
            raw_items = []
        count = len(raw_items)
        kw: dict[str, Any] = {}
        for f in TEXT_FIELDS:
            ids = raw.get(f, [])
            if not ids:
                continue
            if f not in req.allowed:
                flags.append(f"[규칙] {tid}는 {f} 칸을 쓰지 않아 뺌")
                continue
            kw[f] = self._text(n, tid, f, f, ids, items=count, layout=layout, mode=mode, dropped=dropped, flags=flags)
        if raw.get("label"):
            flags.append("[규칙] label 칸은 블록에서 가져오지 않아 뺌")
        if "label" in req.allowed and t.get("label_source") == "fixed":
            kw["label"] = AplusText(text=t["default_label"])  # 템플릿마다 정해진 문구 — 블록에서 오지 않아 출처 블록이 없다
        default_fit = "cover" if tid == "1B" else "contain"
        if "image" in req.allowed:
            kw["image"] = self._image(raw.get("image"), tid, "큰 사진", flags, default_fit=default_fit)
        if "mark" in req.allowed:
            kw["mark"] = self._image(raw.get("mark"), tid, "마크", flags, default_fit="contain")
        items: list[AplusItem] = []
        item_fit = "cover" if (mode == "before_after" or tid == "5A") else "contain"
        for j, it in enumerate(raw_items):
            fields = req.items.fields if req.items else ()
            ikw: dict[str, Any] = {}
            has_title = self._item_has_title(n, tid, it.get("title") or [])
            for f in ("title", "body"):
                if it.get(f) and f in fields:
                    ikw[f] = self._text(n, tid, f"items[{j}].{f}", f"items.{f}", it[f], items=count, layout=layout, mode=mode,
                                        dropped=dropped, flags=flags, item_has_title=has_title)
                elif it.get(f):
                    flags.append(f"[규칙] {tid}는 항목 {f} 칸을 쓰지 않아 뺌")
            if "image" in fields:
                ikw["image"] = self._image(it.get("image"), tid, f"항목{j + 1} 사진", flags, default_fit=item_fit)
            color = it.get("color")
            if color and "color" in fields:
                c = color.strip().upper()
                if len(c) == 7 and c[0] == "#" and all(ch in "0123456789ABCDEF" for ch in c[1:]):
                    ikw["color"] = c
                else:
                    flags.append(f"[규칙] 항목{j + 1} 색 {color!r}이 #RRGGBB가 아니어서 뺌")
            item = AplusItem(**{k: v for k, v in ikw.items() if v is not None})
            if item != AplusItem():
                items.append(item)
        module = AplusModule(template=tid, layout=layout, mode=mode, items=items, **{k: v for k, v in kw.items() if v is not None})
        used_keys = [k for x in [module.label, module.headline, module.subheadline, module.body] if x for k in x.origin_block_keys]
        used_keys += [k for it in module.items for x in (it.title, it.body) if x for k in x.origin_block_keys]
        key_section = {b.block_key: b.section_key for b in self.block_ids.values()}
        sources = sorted({key_section[k] for k in used_keys}, key=lambda k: self.order[k])
        return AplusSlotPlan(slot=n, template=tid, why=why, module=module, sources=sources, dropped=dropped, flags=flags)


def fill_missing_photos(slots: list[AplusSlotPlan], spec: AplusSpec) -> None:
    """필수 큰 사진이 비었으면 다른 칸의 큰 사진을 다시 쓰고 확인 표시를 남긴다(참고 변환기 fill_missing_photos)."""
    donor = next((s.module.image for s in slots if s.module is not None and s.module.image is not None), None)
    for s in slots:
        m = s.module
        if m is None or m.image is not None or donor is None:
            continue
        if "image" in spec.requirements(m.template, layout=m.layout, mode=m.mode).required:
            fit = "contain" if m.template in CONTAIN_ONLY else donor.fit
            s.module = m.model_copy(update={"image": donor.model_copy(update={"fit": fit, "cutout": donor.cutout and fit == "contain"})})
            s.flags.append("[확인] 큰 사진이 없어 다른 칸의 큰 사진을 다시 씀")


def alternate(slots: list[AplusSlotPlan]) -> None:
    """좌우 번갈아(참고 변환기 zigzag.py): 사진이 한쪽에 있는 템플릿은 앞 장의 글 쪽과 반대로 둔다. 첫 장은 글 왼쪽 · 사진 오른쪽.
    1B는 바꾸지 않고 세기만 한다. 대칭 · 통짜 템플릿은 바꾸지도 세지도 않는다. 같은 템플릿이 바로 이어지면 표시만 한다."""
    prev: str | None = None
    prev_t: str | None = None
    for s in slots:
        if s.module is None:
            continue
        t = s.module.template
        if t in ONE_SIDED:
            side = "left" if prev in (None, "right") else "right"  # 글 상자 쪽
            s.module = s.module.model_copy(update={"mirror": side == "right"})
            prev = side
        elif t == "1B":
            prev = "right" if s.module.mirror else "left"
        if t == prev_t:
            s.flags.append(f"[배치] 바로 앞 장과 같은 뼈대({t}) — 확인 필요")
        prev_t = t


def compose(parsed: dict[int, dict[str, Any]], sections: list[Section], labels: list[AplusSectionLabel],
            block_ids: dict[str, TranslatedBlock], sid_to_key: dict[str, str], spec: AplusSpec) -> list[AplusSlotPlan]:
    """응답(칸 번호 → 항목)을 7칸 결과로. 칸 1부터 차례로 채워 '한 블록은 한 곳에만'을 강제한다."""
    c = _Composer(sections, labels, block_ids, sid_to_key, spec)
    slots = [c.slot(n, parsed[n]) for n in SLOTS]
    fill_missing_photos(slots, spec)
    alternate(slots)
    for s in slots:
        if s.module is not None:
            s.errors = content.validate_module(s.module, spec)
    return slots


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def input_fingerprint(sections: list[Section], labels: list[AplusSectionLabel], blocks: list[TranslatedBlock]) -> str:
    data = {"sections": [s.model_dump(mode="json", exclude={"image_path"}) for s in sections],
            "labels": [x.model_dump(mode="json") for x in labels],
            "blocks": [asdict(b) for b in sorted(blocks, key=lambda b: (b.section_key, b.block_order))]}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def run(sections: list[Section], labels: list[AplusSectionLabel], blocks: list[TranslatedBlock], spec: AplusSpec, cfg: dict[str, Any], *,
        llm: MultiImageAssistant | None = None, recorder: Recorder | None = None) -> AplusPlan:
    """상품 하나의 7칸 구성. `sections`는 페이지 순서, `labels`는 같은 섹션의 내용 라벨, `blocks`는 쓸 수 있는 1차 번역 블록."""
    prompt = validate_config(cfg, need_api_key=llm is None, table=TABLE)
    keys = [s.section_key for s in sections]
    if not sections or len(set(keys)) != len(keys):
        raise ValueError("섹션이 없거나 같은 section_key가 두 번 있다")
    if [x.section_key for x in labels] != keys:
        raise ValueError("labels의 섹션 순서 · 집합이 sections와 다르다")
    codes = spec.raw["labels"]
    bad = sorted({x.label for x in labels if x.label not in codes})
    if bad:
        raise ValueError(f"규격 데이터에 없는 라벨: {bad}")
    bkeys = [b.block_key for b in blocks]
    if len(set(bkeys)) != len(bkeys) or any(b.section_key not in keys for b in blocks) or any(not b.text.strip() for b in blocks):
        raise ValueError("블록의 block_key가 겹치거나, 섹션이 없거나, 번역문이 비어 있다")
    odd = sorted({b.role for b in blocks} - set(spec.raw["block_roles"]["roles"]))
    if odd:
        raise ValueError(f"규격 데이터에 없는 블록 역할: {odd}")
    pairs = [(b.section_key, b.block_order) for b in blocks]
    if len(set(pairs)) != len(pairs):
        raise ValueError("한 섹션 안에 같은 block_order가 두 번 있다")
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    fp = input_fingerprint(sections, labels, blocks)
    payload, sid_to_key, block_ids = build_payload(sections, labels, blocks, spec)
    long_side = int(cfg[TABLE]["long_side_px"])
    images: list[tuple[str, Image.Image]] = []
    image_meta: dict[str, Any] = {}
    for sid, key in sid_to_key.items():
        img, meta = prepare_image(next(s for s in sections if s.section_key == key), long_side)
        images.append((sid, img))
        image_meta[sid] = dict(meta, section_key=key, sha256=image_sha256(img))
    ptext = json.dumps(payload, ensure_ascii=False, indent=1)
    slot_of = {x.section_key: spec.slot_of_label(x.label) for x in labels}
    rec: dict[str, Any] = {
        "stage": TABLE, "status": None, "sections": sid_to_key, "input_fingerprint": fp, "spec_version": spec.version, "spec_sha256": spec.sha256,
        "started_at": started.isoformat(timespec="seconds"), "payload": payload, "payload_sha256": sha256_text(ptext),
        "excluded_role_block_keys": [b.block_key for b in blocks if slot_of[b.section_key] is not None
                                     and not usable_role(b.role, slot_of[b.section_key], spec)],
        "prompt_sha256": sha256_text(prompt), "images": image_meta, "long_side_px": long_side, "model_config": llm_config(cfg, TABLE),
        "response_text": None, "usage": None, "finish_reason": None, "call_duration_s": None, "validation": None, "error": None,
    }
    checked = AplusPlanChecked(input_fingerprint=fp, spec_version=spec.version, llm_called=True)

    def failed(status: str, err: str) -> AplusPlan:
        rec.update(status=status, duration_s=round(time.perf_counter() - t0, 3), error=err)
        _emit(recorder, rec, failed=True)
        return AplusPlan(status="failed", slots=None, checked=checked, error=rec["error"])

    c = llm_config(cfg, TABLE)
    assistant = llm if llm is not None else GeminiMultiImageAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"],
                                                                      response_schema=response_schema())
    c0 = time.perf_counter()
    try:
        reply: LlmReply = assistant(prompt, ptext, images)
    except VlmError as e:
        rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
        return failed("call_failed", str(e))
    rec.update(call_duration_s=round(time.perf_counter() - c0, 3), response_text=reply.text, usage=reply.usage, finish_reason=reply.finish_reason)
    try:
        parsed = parse_plan(reply.text)
    except AplusPlanResponseError as e:
        rec["validation"] = {"ok": False, "reasons": e.reasons}
        return failed("validation_failed", "; ".join(e.reasons))
    rec["validation"] = {"ok": True, "reasons": []}
    slots = compose(parsed, sections, labels, block_ids, sid_to_key, spec)
    rec.update(status="ok", duration_s=round(time.perf_counter() - t0, 3), result=[s.model_dump(mode="json") for s in slots])
    _emit(recorder, rec, failed=False)
    return AplusPlan(status="ok", slots=slots, checked=checked)
