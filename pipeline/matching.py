"""사전 표현 매칭 · 겹침 억제 — 통합 운영 규칙 [통합 5.23 · D8 · D9-2].

개발용 ③-1a(`stages/judge.detect`, 공백 제거 부분 문자열 · 예외 쌍)를 바꾸지 않고, 운영 인계(`pipeline/handoff`)가 이 모듈을 쓴다.
사전 패턴은 리터럴이다(정규식으로 실행하지 않음). 정규화 후 빈 패턴은 공급 오류다. 블록을 넘어 매칭하지 않는다.

한국어(N3 source_ko · 용어 원문 확인)
- 문자별 NFKC → lower → str.isspace 공백 제거(LF · CR · 탭 포함). 정규화 문자마다 원문 문자 위치를 보존한다.
- 왼쪽 경계: 원문에서 매칭 시작 직전 문자가 없거나 글자 · 결합 · 숫자(Unicode L* · M* · N*)가 아니어야 한다. 오른쪽 조사 · 어미는 허용.
- 가격 접미 예외: `price_suffix=True` 패턴(현지 원화 가격 항목)만, 직전 원문이 금액(숫자 또는 세 자리 쉼표 묶음 · 선택 소수부 ·
  선택 배수 단위 십 · 백 · 천 · 만 · 억)으로 끝나면 왼쪽 경계를 통과한다. 임의 한국어 단어 뒤 '원'은 허용하지 않는다.
영어(N5 trans_1 재대조 · 강제 용어)
- 문자별 NFKC → lower. 공백(str.isspace)과 하이픈(- U+2010 U+2011)을 구분자 한 칸으로 정규화하고 연속 구분자를 합친다. 다른 기호는 보존.
- 패턴 양끝이 글자/숫자면 매칭 양끝에서도 단어 경계(이웃이 L* · M* · N*가 아님)를 요구한다.
공통
- 문자 확장(예 ㎖ → ml)의 일부만 매칭하지 않도록 시작 · 끝이 원문 문자 경계와 맞아야 한다. 한글 자모 결합은 하지 않는다.
- 원문 구간은 정규화 전 코드 포인트 [start, end), matched_text = text[start:end]. 같은 사전 항목 · 블록 · 구간의 반복 검출은 1건으로
  집계하고 일치한 패턴은 모두 보존한다. 다른 항목의 같은 구간은 따로 유지한다.

겹침 억제(규제 정책 매칭, 같은 실행 · 언어 · 블록 · 텍스트 안에서만)
1. 원시 매칭은 모두 보존한다(억제는 관계 기록이며 삭제가 아니다).
2. allowed와 비허용(allowed 외)이 정확히 같은 구간이면 임의 우선순위로 정하지 않고 `MatchConflictError`(공급 · 실행 검증 오류).
3. allowed 매칭이 다른 매칭 구간을 완전히 감싸면(같은 구간 제외) 내부 매칭을 억제한다. 일부 겹침은 억제하지 않는다.
4. 3 뒤 남은 비허용 매칭끼리 엄격 포함을 한 번에 계산해 포함된 짧은 구간을 억제한다(입력 순서와 무관). 같은 구간은 모두 유지.
5. 부분 겹침 · 비겹침은 모두 남긴다. 억제 원인은 모두 기록한다. allowed는 판정 행을 만들지 않는다(정책 단계).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Literal

MATCH_RULES_VERSION = "match@2026-10-08.1"  # 위 규칙의 버전. 바꾸면 올린다(입력 지문에 들어간다)
Language = Literal["ko", "en"]
HYPHENS = frozenset("-‐‑")
_AMOUNT_TAIL = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?[십백천만억]*$")
_AMOUNT_LOOKBACK = 64


class PatternError(ValueError):
    """패턴을 매칭에 쓸 수 없다(정규화 후 빈 문자열 등) — 사전 공급 오류."""


class MatchConflictError(ValueError):
    """같은 구간에 allowed와 비허용 매칭이 함께 있다 — 임의로 해소하지 않는다(5.23 2번)."""


def _is_word(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LMN"


def normalize(text: str, lang: Language) -> tuple[str, list[int]]:
    """정규화 문자열과 대응표(정규화 k번째 문자 → 원문 문자 인덱스)."""
    out: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if lang == "ko":
                if c.isspace():
                    continue
                out.append(c)
                offsets.append(i)
            else:
                if c.isspace() or c in HYPHENS:
                    if out and out[-1] == " ":
                        continue
                    c = " "
                out.append(c)
                offsets.append(i)
    return "".join(out), offsets


def normalize_pattern(pattern: str, lang: Language) -> str:
    norm, _ = normalize(pattern, lang)
    if lang == "en":
        norm = norm.strip(" ")
    if not norm:
        raise PatternError(f"정규화 후 빈 패턴은 매칭에 쓸 수 없다: {pattern!r}")
    return norm


@dataclass(frozen=True)
class PatternSpec:
    external_id: str
    pattern: str  # 사전 원문 패턴(보존용)
    price_suffix: bool = False


@dataclass
class RawMatch:
    external_id: str
    block_key: str
    language: Language
    start: int
    end: int
    matched_text: str
    patterns: list[str] = field(default_factory=list)
    match_key: str = ""


def _starts_at_char(offsets: list[int], p: int) -> bool:
    return p == 0 or offsets[p - 1] != offsets[p]


def _ends_at_char(offsets: list[int], q: int) -> bool:  # q = 매칭 끝(배타) 정규화 위치
    return q == len(offsets) or offsets[q] != offsets[q - 1]


def _left_ok_ko(text: str, start: int, price_suffix: bool) -> bool:
    if start == 0 or not _is_word(text[start - 1]):
        return True
    if price_suffix:
        before = unicodedata.normalize("NFKC", text[max(0, start - _AMOUNT_LOOKBACK):start])
        return _AMOUNT_TAIL.search(before) is not None
    return False


def find_matches(block_key: str, text: str, specs: Iterable[PatternSpec], lang: Language) -> list[RawMatch]:
    """한 블록 텍스트에서 모든 패턴 출현(겹침 · 반복 포함)을 찾는다. 같은 (항목, 구간)은 1건으로 합치고 패턴을 모은다."""
    norm, offsets = normalize(text, lang)
    found: dict[tuple[str, int, int], RawMatch] = {}
    for spec in specs:
        npat = normalize_pattern(spec.pattern, lang)
        L = len(npat)
        i = norm.find(npat)
        while i != -1:
            q = i + L
            ok = _starts_at_char(offsets, i) and _ends_at_char(offsets, q)
            start, end = offsets[i], offsets[q - 1] + 1
            if ok and lang == "ko":
                ok = _left_ok_ko(text, start, spec.price_suffix)
            elif ok:
                if _is_word(npat[0]) and i > 0 and _is_word(norm[i - 1]):
                    ok = False
                if _is_word(npat[-1]) and q < len(norm) and _is_word(norm[q]):
                    ok = False
            if ok:
                key = (spec.external_id, start, end)
                m = found.get(key)
                if m is None:
                    m = found[key] = RawMatch(spec.external_id, block_key, lang, start, end, text[start:end])
                if spec.pattern not in m.patterns:
                    m.patterns.append(spec.pattern)
            i = norm.find(npat, i + 1)
    return sorted(found.values(), key=lambda m: (m.start, m.end, m.external_id))


@dataclass(frozen=True)
class Suppression:
    match_key: str
    rule: Literal["allowed_wrap", "contained"]
    by: tuple[str, ...]  # 억제 원인 매칭 키(모두)


def resolve_overlaps(matches: list[RawMatch], allowed_ids: set[str]) -> tuple[list[RawMatch], list[Suppression]]:
    """5.23 3~5번. 반환: (억제되지 않은 비허용 매칭, 억제 기록). allowed 매칭은 정책 판정 대상이 아니므로 생존 목록에 넣지 않는다.
    블록 · 언어가 다른 매칭은 서로 비교하지 않는다. match_key는 호출 전에 채워 둔다."""
    keys = [m.match_key for m in matches]
    if not all(keys) or len(set(keys)) != len(keys):
        raise ValueError("resolve_overlaps: match_key가 비었거나 중복이다")
    groups: dict[tuple[str, str], list[RawMatch]] = {}
    for m in matches:
        groups.setdefault((m.block_key, m.language), []).append(m)
    survivors: list[RawMatch] = []
    sups: list[Suppression] = []
    for ms in groups.values():
        allowed = [m for m in ms if m.external_id in allowed_ids]
        others = [m for m in ms if m.external_id not in allowed_ids]
        for o in others:
            same = [a.external_id for a in allowed if (a.start, a.end) == (o.start, o.end)]
            if same:
                raise MatchConflictError(
                    f"블록 {o.block_key} [{o.start},{o.end}) {o.matched_text!r}: 비허용 {o.external_id}와 allowed {same}가 같은 구간이다"
                )
        stage1: list[RawMatch] = []
        for o in others:
            by = tuple(a.match_key for a in allowed if a.start <= o.start and o.end <= a.end)
            if by:
                sups.append(Suppression(o.match_key, "allowed_wrap", by))
            else:
                stage1.append(o)

        def strictly_contains(a: RawMatch, b: RawMatch) -> bool:
            return a.start <= b.start and b.end <= a.end and (a.start, a.end) != (b.start, b.end)

        contained = {o.match_key for o in stage1 if any(strictly_contains(x, o) for x in stage1)}
        for o in stage1:
            if o.match_key in contained:
                by = tuple(x.match_key for x in stage1 if x.match_key not in contained and strictly_contains(x, o))
                sups.append(Suppression(o.match_key, "contained", by))
            else:
                survivors.append(o)
    order = {k: i for i, k in enumerate(keys)}
    survivors.sort(key=lambda m: order[m.match_key])
    sups.sort(key=lambda s: order[s.match_key])
    return survivors, sups
