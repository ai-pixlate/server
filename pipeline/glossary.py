"""⑧ 용어집 선별 · 강제 용어 적용 대상 — BE 공급과 AI 번역문 검사가 함께 쓰는 규칙 [계약 5.1 · 5.1.1 · 통합 D10].

- 원문 검색: 블록마다 `term_ko`를 공통 한국어 매칭 규칙(`pipeline.matching`, 리터럴 · NFKC → lower → 공백 무시 · 왼쪽 경계 ·
  오른쪽 조사 허용)으로 찾는다. 형태소 분석 · 조사 판별을 더하지 않는다. 블록을 넘어 매칭하지 않는다.
- 포함 억제: 같은 블록에서 **강제(enforced) 용어끼리**, 한 매칭이 다른 매칭 구간을 엄격히 포함하면(같은 구간 제외) 포함된 매칭을
  뺀다. 매칭 위치별로 계산하므로 짧은 용어가 다른 위치에 따로 나오면 그 위치는 남는다. 같은 구간 · 부분 겹침은 모두 남긴다.
  참고(reference) 용어는 억제하지도, 억제당하지도 않는다.
- 블록의 유효 용어 = 억제되지 않은 매칭이 하나 이상 있는 용어. 공급 목록 = 대상 블록 유효 용어의 합집합(입력 순서 유지).
- **인증 강제 ↔ 문구 참고 충돌 예외**(계약 5.1.1, 2026-11-07 BE 회신): 같은 대상 블록에서 인증(`term_kind=certification` ·
  enforced)과 문구(`term_kind=marketing` · reference)가 **실제 같은 매칭 구간**에 함께 유효하면 인증을 우선하고 그 블록의 문구 후보를
  뺀다. 단순 포함 · 부분 겹침은 같은 구간이 아니므로 해당하지 않는다. 다른 강제 유형(성분 등)이나 `term_kind`가 없는 행에는 적용하지 않는다.
  유형은 공급된 `term_kind` 값으로만 판단한다(ID 접두어로 추정하지 않음). 문구의 `term_kind` 값 `marketing`은 적재 규칙
  (`app/glossary_ingest.py` SPECS의 phrase)을 따른다. 다른 블록에서 정상 매칭된 같은 문구 행은 그 블록의 공급으로 남는다.
- 블록별 유효 공급 관계는 `select_by_block`이 돌려준다. 합집합 하나로 공급하는 `select`는 충돌로 뺀 후보가 다른 블록 때문에 합집합에
  다시 들어가는 경우(블록 간 충돌 · 정상 혼재)를 공급하지 않는다 — 블록별 공급 계약 전에는 공급 실패다(계약 5.1.1 확장 공급 계약).
- 데이터 오류는 빈 목록으로 숨기지 않고 `GlossaryDataError`: 빈 원문 · 번역어, 허용 외 enforcement, 정규화 후 빈 원문,
  같은 ID의 다른 내용, 위 예외로 풀리지 않는 정규화 원문 충돌(같은 블록 안 또는 합집합 공급 — 임의로 하나를 고르지 않음),
  같은 블록에서 한 문구 후보가 충돌 구간과 정상 구간에 함께 매칭됨. 같은 ID · 같은 내용만 하나로 합친다.
- 예문은 이 모듈이 다루지 않는다. 공급에서 빠진 문구 후보의 예문도 함께 빠진다(예문을 공급할지는 별도 결정).
- 규제사전 정책 매칭(5.23)의 allowed 감싸기 · 충돌 규칙은 쓰지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from pipeline.matching import MATCH_RULES_VERSION, PatternError, PatternSpec, find_matches, normalize, normalize_pattern

SELECT_RULES_VERSION = "glossary-select@2026-11-08.1"  # 위 규칙(공통 매칭 MATCH_RULES_VERSION 포함). 바꾸면 올린다
ENFORCEMENTS = ("enforced", "reference")
CERTIFICATION_KIND = "certification"  # 인증 용어집의 term_kind
PHRASE_KIND = "marketing"  # 문구 용어집의 term_kind(app/glossary_ingest.py SPECS["phrase"])
CONFLICT_RULE = "certification_over_phrase"


class GlossaryDataError(ValueError):
    """용어 데이터를 선별 · 검사에 쓸 수 없다 — 공급 실패로 다룬다(빈 성공 아님)."""


@dataclass(frozen=True)
class Entry:
    glossary_id: str
    term_ko: str
    term_target: str
    enforcement: str
    term_kind: str | None = None  # DB glossary.term_kind. 없으면 인증↔문구 충돌 예외를 적용하지 않는다(기존 동작)


@dataclass(frozen=True)
class TermHit:
    glossary_id: str
    enforcement: str
    start: int
    end: int
    suppressed_by: tuple[str, ...] = ()  # 이 매칭을 포함한 강제 용어 ID(억제되지 않았으면 빈 값)


@dataclass(frozen=True)
class ConflictExclusion:
    """충돌 예외로 공급에서 뺀 문구 후보의 매칭 한 건(블록 · 구간 · 우선한 인증 ID)."""

    block_key: str
    reference_id: str
    enforced_ids: tuple[str, ...]
    start: int
    end: int
    rule: str = CONFLICT_RULE


@dataclass(frozen=True)
class BlockSupply:
    block_key: str
    glossary_ids: tuple[str, ...]  # 이 블록의 최종 유효 공급(입력 순서)


@dataclass(frozen=True)
class Selection:
    """블록별 최종 유효 공급 관계와 합집합. `union_conflicts`가 있으면 합집합 하나로는 블록별 관계를 지킬 수 없다."""

    blocks: tuple[BlockSupply, ...]
    entries: tuple[Entry, ...]  # 합집합(입력 순서)
    exclusions: tuple[ConflictExclusion, ...]
    union_conflicts: tuple[tuple[str, ...], ...]  # 정규화 원문이 같은 서로 다른 ID 묶음(블록이 서로 다름)


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
            if e.term_kind is not None and (not isinstance(e.term_kind, str) or not e.term_kind.strip()):
                raise GlossaryDataError(f"glossary_id {e.glossary_id}: term_kind는 비어 있지 않은 문자열이거나 없어야 한다")
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


def _is_cert(e: Entry) -> bool:
    return e.enforcement == "enforced" and e.term_kind == CERTIFICATION_KIND


def _is_phrase(e: Entry) -> bool:
    return e.enforcement == "reference" and e.term_kind == PHRASE_KIND


def _block_supply(key: str, src: str, p: Prepared) -> tuple[set[str], list[ConflictExclusion]]:
    """한 블록의 최종 유효 ID와 충돌 제외 기록. 풀리지 않는 충돌 · 블록 안 혼재는 GlossaryDataError."""
    live = [h for h in block_hits(key, src, p) if not h.suppressed_by]
    cert_at: dict[tuple[int, int], list[str]] = {}
    for h in live:
        if _is_cert(p.by_id[h.glossary_id]):
            cert_at.setdefault((h.start, h.end), []).append(h.glossary_id)
    exclusions: list[ConflictExclusion] = []
    kept_phrase_hit: set[str] = set()
    for h in live:
        if not _is_phrase(p.by_id[h.glossary_id]):
            continue
        certs = cert_at.get((h.start, h.end))
        if certs:
            exclusions.append(ConflictExclusion(key, h.glossary_id, tuple(sorted(certs)), h.start, h.end))
        else:
            kept_phrase_hit.add(h.glossary_id)
    excluded = {x.reference_id for x in exclusions}
    mixed = sorted(excluded & kept_phrase_hit)
    if mixed:
        raise GlossaryDataError(f"블록 {key}: 문구 후보 {mixed}가 인증 충돌 구간과 정상 구간에 함께 매칭된다 — 블록 안 혼재는 확장 공급 계약 전")
    ids = effective_ids(live) - excluded
    by_norm: dict[str, list[str]] = {}
    for gid in ids:
        by_norm.setdefault(p.norm[gid], []).append(gid)
    clash = sorted(sorted(v) for v in by_norm.values() if len(v) > 1)
    if clash:
        raise GlossaryDataError(f"블록 {key}: 정규화 후 같은 원문의 서로 다른 용어 — 임의로 고르지 않는다: {clash}")
    return ids, exclusions


def select_by_block(blocks: Sequence[tuple[str, str]], entries: Prepared | Iterable[Entry]) -> Selection:
    """대상 블록 [(block_key, source_ko)]마다 최종 유효 공급 관계를 계산한다. 블록 순서 = 입력 순서, 용어 순서 = 입력 용어 순서."""
    p = _prepared(entries)
    per_block: list[BlockSupply] = []
    exclusions: list[ConflictExclusion] = []
    chosen: set[str] = set()
    for key, src in blocks:
        ids, ex = _block_supply(key, src, p)
        per_block.append(BlockSupply(key, tuple(e.glossary_id for e in p.entries if e.glossary_id in ids)))
        exclusions += ex
        chosen |= ids
    out = tuple(e for e in p.entries if e.glossary_id in chosen)
    by_norm: dict[str, list[str]] = {}
    for e in out:
        by_norm.setdefault(p.norm[e.glossary_id], []).append(e.glossary_id)
    union_conflicts = tuple(tuple(v) for v in by_norm.values() if len(v) > 1)
    return Selection(tuple(per_block), out, tuple(exclusions), union_conflicts)


def select(blocks: Sequence[tuple[str, str]], entries: Prepared | Iterable[Entry]) -> list[Entry]:
    """대상 블록 [(block_key, source_ko)]의 유효 용어 합집합(현행 단일 목록 공급). 결과 순서 = 입력 용어 순서.
    풀리지 않는 정규화 원문 충돌은 오류. 블록별 관계가 필요한 합집합(블록 간 충돌 · 정상 혼재)도 공급하지 않고 오류로 둔다."""
    sel = select_by_block(blocks, entries)
    if sel.union_conflicts:
        excluded = {x.reference_id for x in sel.exclusions}
        if any(excluded & set(c) for c in sel.union_conflicts):
            raise GlossaryDataError("충돌로 뺀 문구 후보가 다른 블록 공급으로 합집합에 다시 들어간다 — 블록별 공급 계약 전에는 "
                                    f"합집합 하나로 공급하지 않는다: {sorted(sorted(c) for c in sel.union_conflicts)}")
        raise GlossaryDataError(f"정규화 후 같은 원문의 서로 다른 용어 — 임의로 고르지 않는다: {sorted(sorted(c) for c in sel.union_conflicts)}")
    return list(sel.entries)


RULES = {"select": SELECT_RULES_VERSION, "match": MATCH_RULES_VERSION}
