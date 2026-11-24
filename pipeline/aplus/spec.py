"""A+ 규격 데이터 로더 · 조회 — 개발용 v1(운영 계약 아님, open-questions.md #92).

정본은 `pipeline/data/aplus/spec.json` 하나다. seed-builder 참고 코드(PR #2 · #3, 미병합)의 치수 파일 · 필수 칸 규칙 ·
글자 수 한도 · 라벨→칸 표를 옮긴 것이며 출처 커밋과 파일 해시는 그 파일의 `source`에 있다.
이 모듈은 그 자료를 읽고 형식을 검사하며, 값은 만들지 않는다 — 자료에 없는 한도는 `None`(한도 없음이 아니라 미정, #95)이다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SPEC_PATH = DATA_DIR / "aplus" / "spec.json"

SLOTS = tuple(range(1, 8))
MODULE_FIELDS = ("label", "headline", "subheadline", "body", "image", "mark", "items")  # 모듈 수준 칸
ITEM_FIELDS = ("title", "body", "image", "color", "icon")  # 반복 칸 안의 칸. icon은 규격에만 있고 슬롯 내용 타입에는 아직 없다
LIMIT_FIELDS = ("label", "headline", "subheadline", "body", "items.title", "items.body")
WHEN_KEYS = ("items", "layout", "mode")
REGION_KINDS = ("image", "text", "extra", "zone", "note")


class AplusSpecError(ValueError):
    """규격 데이터가 형식에 맞지 않는다. reasons = 어긴 항목 목록."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("A+ 규격 데이터 오류: " + "; ".join(reasons))


@dataclass(frozen=True)
class ItemsRule:
    """반복 칸 규칙. `required`의 `a|b`는 둘 중 하나."""

    min: int
    max: int
    required: tuple[str, ...]
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Requirements:
    """한 템플릿의 (배치 변형 · 모드를 반영한) 칸 규칙. `items`가 None이면 반복 칸을 쓰지 않는다."""

    required: tuple[str, ...]
    allowed: tuple[str, ...]
    items: ItemsRule | None


@dataclass(frozen=True)
class AplusSpec:
    raw: dict[str, Any]
    sha256: str

    @property
    def version(self) -> str:
        return self.raw["version"]

    @property
    def template_ids(self) -> tuple[str, ...]:
        return tuple(self.raw["templates"])

    def template(self, tid: str) -> dict[str, Any]:
        try:
            return self.raw["templates"][tid]
        except KeyError:
            raise KeyError(f"없는 템플릿 {tid!r} — 가능: {', '.join(self.raw['templates'])}") from None

    def templates_for_slot(self, slot: int) -> tuple[str, ...]:
        return tuple(self.raw["slots"][str(slot)]["templates"])

    def slot_of_label(self, label: str) -> int | None:
        """섹션 내용 라벨 → 7칸 번호. 7칸 밖 라벨은 None. 모르는 라벨은 KeyError."""
        return self.raw["labels"][label]["slot"]

    def labels_for_slot(self, slot: int) -> tuple[str, ...]:
        return tuple(code for code, v in self.raw["labels"].items() if v["slot"] == slot)

    @staticmethod
    def _matches(when: dict[str, Any], *, items: int, layout: str, mode: str | None) -> bool:
        actual = {"items": items, "layout": layout, "mode": mode}
        return all(actual[k] == v for k, v in when.items())

    def requirements(self, tid: str, *, layout: str = "default", mode: str | None = None) -> Requirements:
        t = self.template(tid)
        required = list(t["required"])
        items = t["items"]
        rule = ItemsRule(items["min"], items["max"], tuple(items["required"]), tuple(items["fields"])) if items else None
        for v in t["variants"]:
            if not self._matches(v["when"], items=-1, layout=layout, mode=mode):
                continue
            required = list(v.get("required", required))
            if v.get("items") == "forbidden":
                rule = None
            elif isinstance(v.get("items"), dict) and rule is not None:
                vi = v["items"]
                rule = ItemsRule(vi["min"], vi["max"], tuple(vi["required"]), rule.fields)
        allowed = list(dict.fromkeys(required + list(t["optional"]) + (["items"] if rule else [])))
        return Requirements(tuple(required), tuple(allowed), rule)

    def forbidden_layouts(self, tid: str, *, mode: str | None) -> tuple[str, ...]:
        """그 모드에서 쓸 수 없는 배치 변형(예: 4B 전후 모드의 cards)."""
        out: list[str] = []
        for v in self.template(tid)["variants"]:
            if "mode" in v["when"] and v["when"]["mode"] == mode:
                out += v.get("forbidden_layouts", [])
        return tuple(out)

    def char_limit(self, tid: str, field: str, *, items: int = 0, layout: str = "default", mode: str | None = None) -> int | None:
        """글자 수 한도(영어 · 공백 포함). 조건(`when`)이 맞는 규칙 중 조건이 가장 많은 것을 쓴다.
        맞는 규칙이 없으면 None — 자료에 값이 없다는 뜻이며 한도가 없다는 뜻이 아니다(#95)."""
        if field not in LIMIT_FIELDS:
            raise KeyError(f"한도를 두지 않는 칸 {field!r} — 가능: {', '.join(LIMIT_FIELDS)}")
        best: tuple[int, int] | None = None
        for r in self.template(tid)["char_limits"]:
            when = r.get("when", {})
            if r["field"] == field and self._matches(when, items=items, layout=layout, mode=mode):
                if best is None or len(when) > best[0]:
                    best = (len(when), r["max_chars"])
        return best[1] if best else None


def _check(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return ["최상위가 객체가 아니다"]
    errs: list[str] = []
    for k in ("version", "canvas", "slots", "labels", "templates"):
        if k not in raw:
            errs.append(f"최상위 키 없음: {k}")
    if errs:
        return errs
    cw, ch = raw["canvas"].get("width"), raw["canvas"].get("height")
    if not (isinstance(cw, int) and isinstance(ch, int) and cw > 0 and ch > 0):
        errs.append(f"canvas 크기가 양의 정수가 아니다: {raw['canvas']}")
        return errs
    tids = set(raw["templates"])
    if set(raw["slots"]) != {str(n) for n in SLOTS}:
        errs.append(f"slots 키가 1~7이 아니다: {sorted(raw['slots'])}")
    seen: list[str] = []
    for n, s in raw["slots"].items():
        for t in s.get("templates", []):
            if t not in tids:
                errs.append(f"slots[{n}]의 템플릿 {t!r}이 templates에 없다")
            elif str(raw["templates"][t].get("slot")) != n:
                errs.append(f"템플릿 {t}의 slot이 slots[{n}]과 다르다")
            seen.append(t)
    if sorted(seen) != sorted(tids):
        errs.append("slots의 템플릿 목록과 templates가 일대일이 아니다")
    for code, v in raw["labels"].items():
        if v.get("slot") is not None and v["slot"] not in SLOTS:
            errs.append(f"labels[{code}].slot={v.get('slot')!r} — 1~7 또는 null")
    for tid, t in raw["templates"].items():
        where = f"templates[{tid}]"
        fields = list(t.get("required", [])) + list(t.get("optional", []))
        bad = [f for f in fields if f not in MODULE_FIELDS]
        if bad:
            errs.append(f"{where} 모르는 칸 {bad}")
        if len(set(fields)) != len(fields):
            errs.append(f"{where} required와 optional에 같은 칸이 있다")
        items = t.get("items")
        if ("items" in t.get("required", [])) != (items is not None):
            errs.append(f"{where} required의 items와 items 규칙이 어긋난다")
        if items is not None:
            if not (isinstance(items.get("min"), int) and isinstance(items.get("max"), int) and 1 <= items["min"] <= items["max"]):
                errs.append(f"{where}.items 개수 범위가 잘못됐다: {items.get('min')}~{items.get('max')}")
            names = [x for r in items.get("required", []) for x in r.split("|")] + list(items.get("fields", []))
            bad = sorted({f for f in names if f not in ITEM_FIELDS})
            if bad:
                errs.append(f"{where}.items 모르는 칸 {bad}")
        if "default" not in t.get("layouts", []):
            errs.append(f"{where}.layouts에 default가 없다")
        for i, r in enumerate(t.get("char_limits", [])):
            if r.get("field") not in LIMIT_FIELDS:
                errs.append(f"{where}.char_limits[{i}] 모르는 칸 {r.get('field')!r}")
            if not (isinstance(r.get("max_chars"), int) and not isinstance(r.get("max_chars"), bool) and r["max_chars"] > 0):
                errs.append(f"{where}.char_limits[{i}] max_chars가 양의 정수가 아니다")
            badk = sorted(set(r.get("when", {})) - set(WHEN_KEYS))
            if badk:
                errs.append(f"{where}.char_limits[{i}] 모르는 조건 {badk}")
        for v in t.get("variants", []):
            badk = sorted(set(v.get("when", {})) - set(WHEN_KEYS))
            if badk or not v.get("when"):
                errs.append(f"{where}.variants 조건이 잘못됐다: {v.get('when')}")
        names = [r.get("name") for r in t.get("regions", [])]
        if not names:
            errs.append(f"{where} 구역이 없다")
        if len(set(names)) != len(names):
            errs.append(f"{where} 구역 이름이 겹친다")
        for r in t.get("regions", []):
            if r.get("kind") not in REGION_KINDS:
                errs.append(f"{where} 구역 {r.get('name')!r}의 kind {r.get('kind')!r}")
            box = [r.get(k) for k in ("x", "y", "w", "h")]
            if not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in box):
                errs.append(f"{where} 구역 {r.get('name')!r} 좌표가 수가 아니다")
            elif box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0 or box[0] + box[2] > cw or box[1] + box[3] > ch:
                errs.append(f"{where} 구역 {r.get('name')!r}이 캔버스 {cw}×{ch} 밖이다: {box}")
    return errs


def load_spec(path: str | Path = SPEC_PATH) -> AplusSpec:
    """규격 데이터를 읽고 형식을 검사한다. 어기면 AplusSpecError(값을 고치거나 채우지 않는다)."""
    data = Path(path).read_bytes()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise AplusSpecError([f"JSON으로 읽을 수 없다: {e}"]) from e
    errs = _check(raw)
    if errs:
        raise AplusSpecError(errs)
    return AplusSpec(raw=raw, sha256=hashlib.sha256(data).hexdigest())
