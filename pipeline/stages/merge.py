"""③ 줄·문단 병합 + 역할 분류 — `heuristic_v2` → `llm_assist` [pipeline.md 단계표 ③].

- 휴리스틱으로 영역 → 줄 → 문단(블록)을 만들고, LLM은 추가 병합과 역할 재판정만 한다. 분할 금지.
- 블록 1건 = 문단 1개. role 5종. ocr_confidence = 구성 영역 score 최솟값(types.ocr_confidence_of).
- 원시 영역은 모두 정확히 한 블록에 한 번 들어가고 값을 바꾸지 않는다 [계약 2.5].
- 휴리스틱 정의(계산식 · 정렬 · 입력 분류 · 역할 패턴 · 설정 검증)는 pipeline.md 7.1절이 정본이다.
  PoC 확인 동작과 우리 잠정 설계(open-questions #38)의 구분도 그 절에 있다. 규칙을 바꾸면 문서와 같은 커밋에서.

- llm_assist v1 정의(입력 · 출력 · 응답 검증 · 로컬 조립 · 기록 · 실행 모드별 설정 검증)는 pipeline.md 7.2절(open-questions #41).
  v1은 블록 병합만 한다 — 같은 줄 조각의 줄 복구는 하지 않는다. use_llm=False(CLI --no-llm)면 휴리스틱만 돈다.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pipeline.types import (
    ROLES,
    BBox,
    Line,
    MergeResult,
    OcrRegion,
    OcrResult,
    Role,
    Section,
    TextBlock,
    block_key,
    line_key,
    ocr_confidence_of,
)
from pipeline.vlm import API_KEY_ENV, GeminiMergeAssistant, LlmReply, MergeAssistant, VlmError, sha256_text

# 역할 탐지 패턴 — pipeline.md 7.1절 "역할 판정" 표의 "현재" 열. 주의문구의 의미 범위는 open-questions #9.
PRICE_RE = re.compile(r"₩|\d[\d,]*\s*원")
CAUTION_RE = re.compile(
    "|".join(
        [
            "주의", "경고", r"유의\s*사항", r"유의하(?!게)", "금지", "삼가", "반드시", "사용을 중지",
            "보관", "직사광선", "어린이", "알레르기", r"이상\s*(?:이|증상|반응)", "증상", "전문의",
            "상담", r"(?<!전)문의", "※",
        ]
    )
)

# 설정 값 검증 대상 — pipeline.md 7.1절 "설정 값 검증". llm_model · llm_temperature · prompt_path는 llm_assist 착수 때.
_INT_KEYS = ("gutter_min_px", "gutter_width_div", "title_max_lines", "caption_max_chars", "price_max_chars")
_NUM_KEYS = (
    "line_gap", "para_gap", "h_ratio", "overlap", "line_v_overlap", "line_gap_min", "para_gap_min",
    "left_align_tol", "title_pct", "caption_pct",
) + _INT_KEYS


def validate_config(cfg: dict[str, Any]) -> None:
    """[merge] 휴리스틱 설정을 검사하고 어기면 ValueError. 병합 입력을 건드리기 전에 부른다.

    타입(수치 · bool 제외 · 유한값 · 정수 전용)을 먼저 보고, 모두 맞을 때만 범위를 본다.
    """
    m = cfg.get("merge")
    if not isinstance(m, dict):
        raise ValueError("config에 [merge] 표가 없다")
    missing = [k for k in _NUM_KEYS + ("gutter", "llm_split") if k not in m]
    if missing:
        raise ValueError("잘못된 [merge] 설정: 없는 키 " + ", ".join(f"merge.{k}" for k in missing))

    errors: list[str] = []
    for k in _NUM_KEYS:
        v = m[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"merge.{k}={v!r} — 수치여야 한다(bool 제외)")
        elif not math.isfinite(v):
            errors.append(f"merge.{k}={v!r} — 유한값이어야 한다")
        elif k in _INT_KEYS and not isinstance(v, int):
            errors.append(f"merge.{k}={v!r} — 정수여야 한다")
    if not isinstance(m["gutter"], bool):
        errors.append(f"merge.gutter={m['gutter']!r} — bool이어야 한다")
    if m["llm_split"] is not False:
        errors.append(f"merge.llm_split={m['llm_split']!r} — false만 허용(분할 금지 고정)")
    if errors:
        raise ValueError("잘못된 [merge] 설정: " + "; ".join(errors))

    rules = [
        (0 <= m["line_v_overlap"] <= 1, "merge.line_v_overlap — 0 이상 1 이하"),
        (0 <= m["overlap"] <= 1, "merge.overlap — 0 이상 1 이하"),
        (m["line_gap_min"] <= m["line_gap"], "merge.line_gap_min ≤ merge.line_gap"),
        (m["para_gap_min"] <= m["para_gap"], "merge.para_gap_min ≤ merge.para_gap"),
        (m["h_ratio"] >= 1, "merge.h_ratio — 1 이상"),
        *[(m[k] >= 0, f"merge.{k} — 0 이상") for k in ("left_align_tol", "gutter_min_px", "caption_max_chars", "price_max_chars")],
        *[(m[k] >= 1, f"merge.{k} — 1 이상") for k in ("gutter_width_div", "title_max_lines")],
        *[(0 <= m[k] <= 100, f"merge.{k} — 0 이상 100 이하") for k in ("title_pct", "caption_pct")],
        (m["caption_pct"] <= m["title_pct"], "merge.caption_pct ≤ merge.title_pct"),
    ]
    errors = [msg for ok, msg in rules if not ok]
    if errors:
        raise ValueError("잘못된 [merge] 설정: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# 입력 분류 · 정렬
# ---------------------------------------------------------------------------
def region_kind(r: OcrRegion) -> str:
    """'zero'(폭 또는 높이 0) · 'empty'(text.strip() == "") · 'text'. 둘 다면 크기 0이 우선."""
    if r.bbox.w == 0 or r.bbox.h == 0:
        return "zero"
    if r.text.strip() == "":
        return "empty"
    return "text"


def _region_order(r: OcrRegion) -> tuple[int, int, str]:
    return (r.bbox.x, r.bbox.y, r.region_key)


@dataclass
class _Line:
    regions: list[OcrRegion] = field(default_factory=list)

    @property
    def bbox(self) -> BBox:
        return BBox.union([r.bbox for r in self.regions])

    @property
    def height(self) -> int:
        """판단용 줄 높이 = 영역 높이의 최댓값(줄 bbox 높이와 다르다)."""
        return max(r.bbox.h for r in self.regions)

    @property
    def order(self) -> tuple[int, int, str]:
        first = self.regions[0]
        return (first.bbox.y, first.bbox.x, first.region_key)

    @property
    def text(self) -> str:
        return " ".join(r.text.strip() for r in self.regions)


# ---------------------------------------------------------------------------
# gutter · 줄 병합 · 블록 병합
# ---------------------------------------------------------------------------
def find_gutters(regions: list[OcrRegion], width: int, m: dict[str, Any]) -> list[tuple[int, int]]:
    """영역이 차지하지 않는 열 [g0, g1) 중 최소 폭 이상이고 x=0 · x=width에 붙지 않은 구간."""
    min_w = max(m["gutter_min_px"], width // m["gutter_width_div"])
    covered = bytearray(width)
    for r in regions:
        x0, x1 = max(0, r.bbox.x), min(width, r.bbox.x2)
        if x1 > x0:
            covered[x0:x1] = b"\x01" * (x1 - x0)
    gutters: list[tuple[int, int]] = []
    x = 0
    while x < width:
        if covered[x]:
            x += 1
            continue
        start = x
        while x < width and not covered[x]:
            x += 1
        if start > 0 and x < width and x - start >= min_w:
            gutters.append((start, x))
    return gutters


def _joins_line(a: OcrRegion, b: OcrRegion, m: dict[str, Any], gutters: list[tuple[int, int]]) -> bool:
    ah, bh = a.bbox.h, b.bbox.h
    base = min(ah, bh)
    inter = max(0, min(a.bbox.y2, b.bbox.y2) - max(a.bbox.y, b.bbox.y))
    if inter / max(1, base) < m["line_v_overlap"]:
        return False
    gap = b.bbox.x - a.bbox.x2
    if not (m["line_gap_min"] * base <= gap <= m["line_gap"] * base):
        return False
    if max(ah, bh) / base > m["h_ratio"]:
        return False
    if m["gutter"] and any(a.bbox.x2 <= g0 and g1 <= b.bbox.x for g0, g1 in gutters):
        return False
    return True


def group_lines(regions: list[OcrRegion], m: dict[str, Any], gutters: list[tuple[int, int]]) -> list[_Line]:
    """병합 참여 영역 → 줄. 줄은 만든 순서로 보고 마지막 영역과 비교해 처음 맞는 줄에 붙인다. 결과는 줄 순서."""
    lines: list[_Line] = []
    for b in sorted(regions, key=_region_order):
        for ln in lines:
            if _joins_line(ln.regions[-1], b, m, gutters):
                ln.regions.append(b)
                break
        else:
            lines.append(_Line([b]))
    return sorted(lines, key=lambda ln: ln.order)


def _joins_block(p: _Line, l: _Line, m: dict[str, Any]) -> bool:
    ph, lh = p.height, l.height
    base = min(ph, lh)
    pb, lb = p.bbox, l.bbox
    gap = lb.y - pb.y2
    if not (m["para_gap_min"] * base <= gap <= m["para_gap"] * base):
        return False
    if max(ph, lh) / base > m["h_ratio"]:
        return False
    ov = max(0, min(pb.x2, lb.x2) - max(pb.x, lb.x)) / min(pb.w, lb.w)
    if ov < m["overlap"] and abs(pb.x - lb.x) > m["left_align_tol"] * base:
        return False
    return True


def _block_order(g: list[_Line]) -> tuple[int, int, str]:
    bb = BBox.union([ln.bbox for ln in g])
    return (bb.y, bb.x, g[0].regions[0].region_key)


def group_blocks(lines: list[_Line], m: dict[str, Any]) -> list[list[_Line]]:
    """줄 순서의 줄 → 그룹. 그룹은 만든 순서로 보고 마지막 줄과 비교해 처음 맞는 그룹에 붙인다."""
    groups: list[list[_Line]] = []
    for ln in lines:
        for g in groups:
            if _joins_block(g[-1], ln, m):
                g.append(ln)
                break
        else:
            groups.append([ln])
    return groups


# ---------------------------------------------------------------------------
# 역할 판정
# ---------------------------------------------------------------------------
def font_h(regions: list[OcrRegion]) -> int:
    """블록 영역 높이를 오름차순 정렬한 배열의 0부터 센 위치 n // 2의 값(짝수면 가운데 두 값 중 큰 값)."""
    hs = sorted(r.bbox.h for r in regions)
    return hs[len(hs) // 2]


def height_stats(heights: list[int], m: dict[str, Any]) -> tuple[float, float] | None:
    """(h_big, h_small) 선형 보간 백분위. 대상이 없거나 h_big == h_small이면 None(크기 규칙 생략)."""
    if not heights:
        return None
    h_big = float(np.percentile(heights, m["title_pct"], method="linear"))
    h_small = float(np.percentile(heights, m["caption_pct"], method="linear"))
    if h_big == h_small:
        return None
    return h_big, h_small


def classify(text: str, n_lines: int, fh: int, stats: tuple[float, float] | None, m: dict[str, Any]) -> Role:
    if PRICE_RE.search(text) and len(text) <= m["price_max_chars"]:
        return "price"
    if CAUTION_RE.search(text):
        return "caution"
    if stats is not None:
        h_big, h_small = stats
        if fh >= h_big and n_lines <= m["title_max_lines"]:
            return "title"
        if fh <= h_small and len(text) <= m["caption_max_chars"]:
            return "caption"
    return "body"


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def heuristic(section: Section, ocr: OcrResult, cfg: dict[str, Any]) -> MergeResult:
    """heuristic_v2 — LLM 없이 블록과 역할을 만든다(pipeline.md 7.1절)."""
    m = cfg["merge"]
    if ocr.section_key != section.section_key:
        raise ValueError(f"OCR 결과({ocr.section_key})와 섹션({section.section_key})이 다르다")
    keys = [r.region_key for r in ocr.regions]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{section.section_key}: 섹션 안에서 region_key가 중복된다")

    kinds = {r.region_key: region_kind(r) for r in ocr.regions}
    text_regions = [r for r in ocr.regions if kinds[r.region_key] == "text"]
    gutter_regions = [r for r in ocr.regions if kinds[r.region_key] in ("text", "empty")]
    gutters = find_gutters(gutter_regions, section.width, m) if m["gutter"] else []

    groups = group_blocks(group_lines(text_regions, m, gutters), m)
    groups += [[_Line([r])] for r in ocr.regions if kinds[r.region_key] != "text"]  # 빈 텍스트 · 크기 0 단독 블록
    groups.sort(key=_block_order)

    stats = height_stats([r.bbox.h for r in text_regions], m)
    blocks: list[TextBlock] = []
    n_line = 0
    for i, g in enumerate(groups, start=1):
        lines: list[Line] = []
        for ln in g:
            n_line += 1
            lines.append(Line(line_key=line_key(n_line), text=ln.text, bbox=ln.bbox, regions=list(ln.regions)))
        text = "\n".join(ln.text for ln in lines)
        regions = [r for ln in g for r in ln.regions]
        blocks.append(
            TextBlock(
                block_key=block_key(i),
                section_key=section.section_key,
                block_order=i,
                source_ko=text,
                source_lines=lines,
                bbox=BBox.union([r.bbox for r in regions]),
                role=classify(text, len(lines), font_h(regions), stats, m),
                ocr_confidence=ocr_confidence_of(lines),
            )
        )
    return MergeResult(section_key=section.section_key, blocks=blocks)


# ---------------------------------------------------------------------------
# llm_assist v1 [pipeline.md 7.2절 · open-questions #41]
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]  # pipeline 패키지의 상위 폴더

Recorder = Callable[[dict[str, Any]], None]


class LlmResponseError(VlmError):
    """LLM 응답이 7.2절 응답 검증을 통과하지 못했다. reasons = 어긴 항목 목록."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("LLM 응답 검증 실패: " + "; ".join(reasons))


def resolve_prompt_path(p: str) -> Path:
    """상대 경로는 레포 루트(pipeline 패키지의 상위 폴더) 기준, 절대 경로는 그대로(7.2절)."""
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def _finite_number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def validate_llm_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """LLM 실행·재생 모드의 설정 검증(7.2절). 어기면 ValueError. 통과하면 프롬프트 원문을 돌려준다.

    need_api_key: 실제 호출(기본 Gemini 호출자)일 때만 True. 재생 · 주입 호출자는 키가 필요 없다.
    """
    m = cfg["merge"]
    missing = [k for k in ("llm_model", "llm_temperature", "llm_timeout_s", "prompt_path") if k not in m]
    if missing:
        raise ValueError("잘못된 [merge] LLM 설정: 없는 키 " + ", ".join(f"merge.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(m["llm_model"], str) or not m["llm_model"].strip():
        errors.append(f"merge.llm_model={m['llm_model']!r} — 비어 있지 않은 문자열")
    t = m["llm_temperature"]
    if not _finite_number(t) or not 0 <= t <= 2:
        errors.append(f"merge.llm_temperature={t!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    to = m["llm_timeout_s"]
    if not _finite_number(to) or not to > 0:
        errors.append(f"merge.llm_timeout_s={to!r} — 수치(bool 제외) · 유한값 · 0 초과")
    prompt = ""
    pp = m["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"merge.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = resolve_prompt_path(pp)
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"merge.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"merge.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 LLM 호출에 필요하다")
    if errors:
        raise ValueError("잘못된 [merge] LLM 설정: " + "; ".join(errors))
    return prompt


def llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """기록 · 재생 비교에 쓰는 모델 설정(모델 · 온도 · 시간 제한)."""
    m = cfg["merge"]
    return {"model": m["llm_model"], "temperature": m["llm_temperature"], "timeout_s": m["llm_timeout_s"]}


def default_assistant(cfg: dict[str, Any]) -> GeminiMergeAssistant:
    c = llm_config(cfg)
    return GeminiMergeAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"])


def _sendable(b: TextBlock) -> bool:
    """텍스트가 있고 크기 0이 아닌 휴리스틱 블록만 LLM에 보낸다(빈 텍스트 · 크기 0 단독 블록은 보존)."""
    return all(region_kind(r) == "text" for ln in b.source_lines for r in ln.regions)


def build_payload(section: Section, heur: MergeResult) -> tuple[dict[str, Any], dict[str, TextBlock]]:
    """LLM 입력(7.2절)과 임시 ID → 휴리스틱 블록 대응. ID는 보낼 블록의 블록 순서대로 b1, b2, …"""
    send = [b for b in heur.blocks if _sendable(b)]
    ids = {f"b{i}": b for i, b in enumerate(send, start=1)}
    payload = {
        "section": {"width": section.width, "height": section.height},
        "blocks": [
            {
                "id": bid,
                "text": b.source_ko,
                "bbox": [b.bbox.x, b.bbox.y, b.bbox.w, b.bbox.h],
                "lines": len(b.source_lines),
                "font_h": font_h([r for ln in b.source_lines for r in ln.regions]),
            }
            for bid, b in ids.items()
        ],
    }
    return payload, ids


def payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_groups(text: str, ids: dict[str, TextBlock]) -> list[tuple[list[str], str]]:
    """응답 검증(7.2절). 형식 · 스키마 · 모든 ID가 정확히 한 번 · 미등록 ID · 빈 묶음 · 역할 5종. 어기면 LlmResponseError."""
    try:
        data = json.loads(text)
    except ValueError:
        raise LlmResponseError(["JSON 형식이 아니다"]) from None
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise LlmResponseError(["최상위가 {\"blocks\": [...]} 형식이 아니다"])
    reasons: list[str] = []
    groups: list[tuple[list[str], str]] = []
    for i, item in enumerate(data["blocks"]):
        if not isinstance(item, dict) or not isinstance(item.get("members"), list) or "role" not in item:
            reasons.append(f"blocks[{i}] — members 배열과 role이 있어야 한다")
            continue
        members = item["members"]
        if not members:
            reasons.append(f"blocks[{i}] — 빈 members")
        if not all(isinstance(x, str) for x in members):
            reasons.append(f"blocks[{i}] — members는 문자열 ID 배열이어야 한다")
            continue
        if item["role"] not in ROLES:
            reasons.append(f"blocks[{i}] — 모르는 role {item['role']!r}")
        groups.append((list(members), item["role"]))
    seen = Counter(x for members, _ in groups for x in members)
    unknown = sorted(x for x in seen if x not in ids)
    dup = sorted(x for x, n in seen.items() if n > 1 and x in ids)
    missing = sorted((x for x in ids if x not in seen), key=lambda s: int(s[1:]))
    if unknown:
        reasons.append("모르는 ID " + ", ".join(unknown))
    if dup:
        reasons.append("두 번 이상 나온 ID " + ", ".join(dup))
    if missing:
        reasons.append("빠진 ID " + ", ".join(missing))
    if reasons:
        raise LlmResponseError(reasons)
    return groups


def assemble(
    section: Section, heur: MergeResult, ids: dict[str, TextBlock], groups: list[tuple[list[str], str]]
) -> tuple[MergeResult, dict[str, str]]:
    """로컬 조립(7.2절, v1 — 블록 병합만). 묶음의 멤버는 휴리스틱 블록 순서로 정렬해 줄을 그대로 잇는다.
    같은 줄 조각의 줄 복구는 하지 않는다. 보내지 않은 블록은 그대로. 반환: (결과, 휴리스틱 블록 키 → 섹션 최종 블록 키)."""
    out: list[tuple[list[TextBlock], str]] = [
        (sorted((ids[x] for x in members), key=lambda b: b.block_order), role) for members, role in groups
    ]
    sent = {b.block_key for b in ids.values()}
    out += [([b], b.role) for b in heur.blocks if b.block_key not in sent]

    def order(g: tuple[list[TextBlock], str]) -> tuple[int, int, str]:
        bb = BBox.union([b.bbox for b in g[0]])
        return (bb.y, bb.x, g[0][0].source_lines[0].regions[0].region_key)

    out.sort(key=order)
    blocks: list[TextBlock] = []
    mapping: dict[str, str] = {}
    n_line = 0
    for i, (members, role) in enumerate(out, start=1):
        lines: list[Line] = []
        for b in members:
            for ln in b.source_lines:
                n_line += 1
                lines.append(ln.model_copy(update={"line_key": line_key(n_line)}))
            mapping[b.block_key] = block_key(i)
        regions = [r for ln in lines for r in ln.regions]
        blocks.append(
            TextBlock(
                block_key=block_key(i),
                section_key=section.section_key,
                block_order=i,
                source_ko="\n".join(ln.text for ln in lines),
                source_lines=lines,
                bbox=BBox.union([r.bbox for r in regions]),
                role=role,
                ocr_confidence=ocr_confidence_of(lines),
            )
        )
    return MergeResult(section_key=section.section_key, blocks=blocks), mapping


def _emit(recorder: Recorder | None, rec: dict[str, Any], err: BaseException | None = None) -> None:
    """기록을 쓴다. 호출·검증이 실패한 중이면(err) 기록 저장 실패는 err의 note로만 붙이고 원래 오류를 살린다.
    성공한 중이면 기록 저장 실패를 그대로 올린다 — 기록 없는 결과를 남기지 않는다(7.2절)."""
    if recorder is None:
        return
    try:
        recorder(rec)
    except Exception as w:  # noqa: BLE001
        if err is None:
            raise
        err.add_note(f"merge_debug 기록 저장 실패: {w!r}")


def assist(
    section: Section, heur: MergeResult, prompt: str, cfg: dict[str, Any], llm: MergeAssistant,
    recorder: Recorder | None = None,
) -> MergeResult:
    """llm_assist v1: 휴리스틱 결과에 LLM 추가 병합 · 역할 재판정을 적용한다. 실패는 VlmError로 전파(#37)."""
    payload, ids = build_payload(section, heur)
    ptext = payload_text(payload)
    started = time.monotonic()
    rec: dict[str, Any] = {
        "section_key": section.section_key,
        "source_image_id": section.source_image_id,
        "status": None,
        "model_config": llm_config(cfg),
        "prompt_path": str(cfg["merge"]["prompt_path"]),
        "prompt_sha256": sha256_text(prompt),
        "payload": payload,
        "payload_sha256": sha256_text(ptext),
        "ids": {bid: b.block_key for bid, b in ids.items()},
        "response_text": None,
        "error": None,
        "result": None,
        "usage": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": None,
    }

    def done(status: str) -> None:
        rec["status"] = status
        rec["duration_s"] = round(time.monotonic() - started, 3)

    if not ids:
        done("skipped")  # 보낼 블록 0개 — 호출하지 않는다
        rec["result"] = {"groups": [], "block_map": {b.block_key: b.block_key for b in heur.blocks}}
        _emit(recorder, rec)
        return heur
    try:
        reply: LlmReply = llm(prompt, ptext)
    except VlmError as e:
        done("call_failed")
        rec["error"] = str(e)
        _emit(recorder, rec, e)
        raise
    rec["response_text"] = reply.text
    rec["usage"] = reply.usage
    try:
        groups = parse_groups(reply.text, ids)
    except LlmResponseError as e:
        done("validation_failed")
        rec["error"] = e.reasons
        _emit(recorder, rec, e)
        raise
    result, mapping = assemble(section, heur, ids, groups)
    done("ok")
    rec["result"] = {
        "groups": [{"members": [ids[x].block_key for x in members], "role": role} for members, role in groups],
        "block_map": mapping,
    }
    _emit(recorder, rec)
    return result


def json_recorder(debug_dir: Path) -> Recorder:
    """`<debug_dir>/<section_key>.json`에 기록을 쓰는 recorder."""

    def write(rec: dict[str, Any]) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{rec['section_key']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

    return write


def run(
    section: Section, ocr: OcrResult, cfg: dict[str, Any], *, use_llm: bool = True,
    llm: MergeAssistant | None = None, recorder: Recorder | None = None,
) -> MergeResult:
    """③ 실행. use_llm=False면 heuristic_v2만(프롬프트 · API 키 불필요). llm을 주지 않으면 기본 Gemini 호출자(API 키 필요)."""
    validate_config(cfg)
    if not use_llm:
        return heuristic(section, ocr, cfg)
    prompt = validate_llm_config(cfg, need_api_key=llm is None)
    heur = heuristic(section, ocr, cfg)
    return assist(section, heur, prompt, cfg, llm if llm is not None else default_assistant(cfg), recorder)
