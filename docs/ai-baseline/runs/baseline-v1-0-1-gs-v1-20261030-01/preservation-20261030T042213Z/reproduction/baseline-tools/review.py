"""저장된 판정의 검수 대상과 비교표. 사람 검수 완료를 추정하지 않는다."""
import random
from .core import IDS, REGIONS, latest_evaluations, resolved_original, adopted_evaluations


def review_plan(history, seed, expanded=(), baseline=()):
    latest = latest_evaluations(history)
    pending, independent, agreed_b = {}, {}, {}
    def add(target, key, reason):
        target.setdefault(key, set()).add(reason)
    for rid, sid, _ in REGIONS:
        for metric in range(1, 8):
            add(pending, (sid, metric, rid), "핵심 영역: 해당 없음도 명시적으로 확인")
            add(independent, (sid, metric, rid), "핵심 영역 독립 재검토")
    for e in adopted_evaluations(baseline).values():
        if e["grade"] not in ("B", "C"):
            continue
        scopes = e.get("defect_ids", []) if e["scope"] == "original" else [e["scope"]]
        for scope in scopes:
            scope = scope if scope.startswith(("K", "D-")) else "D-" + scope
            key = (e["source_id"], e["metric"], scope)
            add(independent, key, "baseline 문제 영역 독립 재검토")
            if e["grade"] == "C":
                add(pending, key, "baseline C 영역 필수 검수")
    keys = {k[:3] for k in latest}
    for key in sorted(keys):
        p, i, h = [latest.get((*key, role)) for role in ("primary", "independent", "human")]
        if p and p["grade"] in ("B", "C", "판단 불가") and not i:
            add(independent, key, "1차 B/C 또는 판단 불가")
        if any(e and e["grade"] in ("C", "판단 불가") for e in (p, i)):
            add(pending, key, "C 또는 판단 불가")
        if p and i:
            if p["grade"] != i["grade"] or set(p.get("defect_ids", [])) != set(i.get("defect_ids", [])):
                add(pending, key, "AI 판정 또는 결함 판단 불일치")
            elif p["grade"] == "B":
                group = (key[0][:5], key[1], p["defect_type"])
                agreed_b.setdefault(group, []).append(key)
        if h and h.get("misgrade_detected"):
            expanded = (*expanded, (key[0][:5], key[1]))
    for candidates in agreed_b.values():
        chosen = next((k for k in candidates if (*k, "human") in latest), sorted(candidates)[0])
        add(pending, chosen, "동의 B 유형별 표본")
    rng = random.Random(str(seed))
    for group in ("GS-01", "GS-02", "GS-03"):
        candidates = [sid for sid in IDS if sid.startswith(group) and
                      latest.get((sid, 1, "original", "primary"), {}).get("grade") == "A"]
        if candidates:
            sid = rng.choice(candidates)
            add(pending, (sid, 1, "original"), "최종 A 상품별 무작위 표본")
            for m in range(2, 8):
                if latest.get((sid, m, "original", "primary"), {}).get("grade") == "A":
                    add(pending, (sid, m, "original"), "최종 A 표본의 연결 단계")
    for group, metric in expanded:
        for sid in IDS:
            if sid.startswith(group):
                add(pending, (sid, metric, "original"), "오판 발견에 따른 확대 검수")
    def flatten(mapping):
        return [{"source_id": k[0], "metric": k[1], "scope": k[2], "reasons": sorted(v),
                 "human_record_present": (*k, "human") in latest,
                 "independent_record_present": (*k, "independent") in latest} for k, v in mapping.items()]
    return {"sample_seed": str(seed), "human": flatten(pending), "independent": flatten(independent),
            "completion": "미확정 — 핵심 영역 전체·후속 결함·확대 범위와 백업은 책임자가 확인"}


def transitions(before, after):
    a, b = latest_evaluations(before), latest_evaluations(after)
    def grade(data, sid, m):
        return resolved_original(data, sid, m)
    result = {}
    for m in range(1, 8):
        matrix = {x: {y: 0 for y in "ABC"} for x in "ABC"}
        other = []
        for sid in IDS:
            x, y = grade(a, sid, m), grade(b, sid, m)
            if x in matrix and y in matrix[x]:
                matrix[x][y] += 1
            else:
                other.append({"source_id": sid, "before": x, "after": y})
        result[str(m)] = {"matrix": matrix, "other": other}
    return result
