"""⑧ 용어집 선별 · 강제 용어 적용 대상 — BE 공급과 AI 번역문 검사가 함께 쓰는 규칙 [계약 5.1 · 통합 D10].

- 원문 검색: 블록마다 `term_ko`를 공통 한국어 매칭 규칙(`pipeline.matching`, 리터럴 · NFKC → lower → 공백 무시 · 왼쪽 경계 ·
  오른쪽 조사 허용)으로 찾는다. 형태소 분석 · 조사 판별을 더하지 않는다. 블록을 넘어 매칭하지 않는다.
- 포함 억제: 같은 블록에서 **강제(enforced) 용어끼리**, 한 매칭이 다른 매칭 구간을 엄격히 포함하면(같은 구간 제외) 포함된 매칭을
  뺀다. 매칭 위치별로 계산하므로 짧은 용어가 다른 위치에 따로 나오면 그 위치는 남는다. 같은 구간 · 부분 겹침은 모두 남긴다.
  참고(reference) 용어는 억제하지도, 억제당하지도 않는다.
- 블록의 유효 용어 = 억제되지 않은 매칭이 하나 이상 있는 용어. 공급 목록 = 대상 블록 유효 용어의 합집합(입력 순서 유지).
- 데이터 오류는 빈 목록으로 숨기지 않고 `GlossaryDataError`: 빈 원문 · 번역어, 허용 외 enforcement, 정규화 후 빈 원문,
  같은 ID의 다른 내용, 선택된 서로 다른 ID의 정규화 원문이 같음(임의로 하나를 고르지 않음). 같은 ID · 같은 내용만 하나로 합친다.
- 규제사전 정책 매칭(5.23)의 allowed 감싸기 · 충돌 규칙은 쓰지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from pipeline.matching import MATCH_RULES_VERSION, PatternError, PatternSpec, find_matches, normalize, normalize_pattern

SELECT_RULES_VERSION = "glossary-select@2026-10-11.1"  # 위 규칙(공통 매칭 MATCH_RULES_VERSION 포함). 바꾸면 올린다
ENFORCEMENTS = ("enforced", "reference")


class GlossaryDataError(ValueError):
    """용어 데이터를 선별 · 검사에 쓸 수 없다 — 공급 실패로 다룬다(빈 성공 아님)."""


@dataclass(frozen=True)
class Entry:
    glossary_id: str
    term_ko: str
    term_target: str
    enforcement: str


@dataclass(frozen=True)
class TermHit:
    glossary_id: str
    enforcement: str
    start: int
    end: int
    suppressed_by: tuple[str, ...] = ()  # 이 매칭을 포함한 강제 용어 ID(억제되지 않았으면 빈 값)


class Prepared:
    """검증 · 정규화를 한 번만 하고 여러 블록에 쓴다. 입력 순서를 보존한다."""

    def __init__(self, entries: Iterable[Entry]):
        seen: dict[str, Entry] = {}
        self.entries: list[Entry] = []
        self.norm: dict[str, str] = {}
        for e in entries:
            if e.glossary_id in seen:
                if seen[e.glossary_id] != e:
                    raise GlossaryDataError(f"glossary_id {e.glossary_id}: 같은 ID에 다른 내용")
                continue
            if not e.glossary_id or not e.term_ko.strip() or not e.term_target.strip():
                raise GlossaryDataError(f"glossary_id {e.glossary_id!r}: 식별자 · 원문 · 번역어가 비어 있다")
            if e.enforcement not in ENFORCEMENTS:
                raise GlossaryDataError(f"glossary_id {e.glossary_id}: 허용 외 enforcement {e.enforcement!r}")
            try:
                self.norm[e.glossary_id] = normalize_pattern(e.term_ko, "ko")
            except PatternError as err:
                raise GlossaryDataError(f"glossary_id {e.glossary_id}: {err}") from err
            seen[e.glossary_id] = e
            self.entries.append(e)
        self.by_id = seen


def _prepared(entries: Prepared | Iterable[Entry]) -> Prepared:
    return entries if isinstance(entries, Prepared) else Prepared(entries)


def block_hits(block_key: str, source_ko: str, entries: Prepared | Iterable[Entry]) -> list[TermHit]:
    """한 블록 원문의 모든 용어 매칭과 강제 용어 포함 억제 결과. 정렬 (start, end, glossary_id)."""
    p = _prepared(entries)
    norm, _ = normalize(source_ko, "ko")
    # 정규화 부분 문자열이 없는 용어는 공통 매칭에서도 나오지 않는다 — 대상 후보만 넘긴다(결과 동일, 속도용)
    specs = [PatternSpec(e.glossary_id, e.term_ko) for e in p.entries if p.norm[e.glossary_id] in norm]
    raw = find_matches(block_key, source_ko, specs, "ko")
    enf = [m for m in raw if p.by_id[m.external_id].enforcement == "enforced"]
    hits = []
    for m in raw:
        by: tuple[str, ...] = ()
        if p.by_id[m.external_id].enforcement == "enforced":
            by = tuple(sorted({o.external_id for o in enf
                               if o.start <= m.start and m.end <= o.end and (o.start, o.end) != (m.start, m.end)}))
        hits.append(TermHit(m.external_id, p.by_id[m.external_id].enforcement, m.start, m.end, by))
    return hits


def effective_ids(hits: Sequence[TermHit]) -> set[str]:
    return {h.glossary_id for h in hits if not h.suppressed_by}


def select(blocks: Sequence[tuple[str, str]], entries: Prepared | Iterable[Entry]) -> list[Entry]:
    """대상 블록 [(block_key, source_ko)]의 유효 용어 합집합. 결과 순서 = 입력 용어 순서. 정규화 원문 충돌은 오류."""
    p = _prepared(entries)
    chosen: set[str] = set()
    for key, src in blocks:
        chosen |= effective_ids(block_hits(key, src, p))
    out = [e for e in p.entries if e.glossary_id in chosen]
    by_norm: dict[str, list[str]] = {}
    for e in out:
        by_norm.setdefault(p.norm[e.glossary_id], []).append(e.glossary_id)
    clash = {n: ids for n, ids in by_norm.items() if len(ids) > 1}
    if clash:
        raise GlossaryDataError(f"정규화 후 같은 원문의 서로 다른 용어 — 임의로 고르지 않는다: {sorted(clash.values())}")
    return out


RULES = {"select": SELECT_RULES_VERSION, "match": MATCH_RULES_VERSION}
