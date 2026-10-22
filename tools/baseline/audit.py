"""공식 결과의 완전성을 추정하지 않는 근거·비용·비교 검사."""
from collections import Counter
from pathlib import Path
import math

from .core import IDS, digest, read, rows, append, now, adopted_evaluations


def policy_regions(run):
    frozen = read(Path(run) / "frozen.json")
    entry = frozen.get("reference_file")
    if not entry:
        return None
    path = Path(run) / entry["file"]
    if digest(path) != entry["sha256"]:
        raise ValueError("기준 자료 해시 불일치")
    data = read(path)
    if not isinstance(data, dict):
        raise ValueError("기준 자료는 JSON 객체여야 합니다")
    regions = data.get("policy_regions")
    if regions is None:
        return None
    coverage = data.get("policy_coverage", [])
    sources = [e.get("source_id") for e in coverage]
    if len(sources) != len(IDS) or set(sources) != set(IDS):
        raise ValueError("34장 각각의 정책 기준 작성 완료/대상 없음 확인 필요")
    for e in coverage:
        has_regions = any(r.get("source_id") == e["source_id"] for r in regions)
        if not e.get("confirmed_by") or not e.get("reason"):
            raise ValueError("정책 기준 범위의 확인자와 근거 필요")
        if e.get("status") not in ("complete", "not_applicable"):
            raise ValueError("정책 기준 범위 상태 오류")
        if (e["status"] == "complete") != has_regions:
            raise ValueError("기준 작성 완료/대상 없음과 영역 목록 불일치")
    keys = [(e["source_id"], e["region_id"]) for e in regions]
    if len(keys) != len(set(keys)) or any(s not in IDS for s, _ in keys):
        raise ValueError("기준 영역 중복 또는 원본 오류")
    for e in regions:
        if not all(k in e for k in ("expected_exclude", "expected_replace", "location", "text", "basis")):
            raise ValueError("기준 영역의 기대 처리·위치·문구·근거 필요")
        if any(type(e[k]) is not bool for k in ("expected_exclude", "expected_replace")):
            raise ValueError("동결 기대값은 bool이어야 합니다")
    return regions


def evidence_issues(run, events):
    """판정 근거는 묶음 내부 파일과 해시·원본 ID로 연결한다."""
    root = Path(run).resolve()
    issues = []
    bindings = rows(root / "bindings.jsonl")
    by_source = {e["source_id"]: e for e in bindings}
    if len(by_source) != len(bindings):
        issues.append("원본/작업 연결 중복")
    for e in events:
        for ref in e.get("evidence", []):
            if not isinstance(ref, dict):
                issues.append(f"{e.get('id')}: 구조화된 근거 필요")
                continue
            p = (root / ref.get("path", "")).resolve()
            if not p.is_relative_to(root) or not p.is_file():
                issues.append(f"{e.get('id')}: 근거 파일 없음/범위 밖")
                continue
            if ref.get("source_id") != e.get("source_id") or ref.get("sha256") != digest(p):
                issues.append(f"{e.get('id')}: 근거 원본/해시 불일치")
                continue
            if p.parent.name == "objects" and "object_key" not in ref:
                issues.append(f"{e.get('id')}: 객체 파일의 object_key 필요")
            if "object_key" in ref:
                objects = rows(root / "objects" / "objects.jsonl")
                candidates = [o for o in objects if o["key"] == ref["object_key"] and o["sha256"] == ref["sha256"]]
                binding = by_source.get(e["source_id"])
                if not candidates or not binding:
                    issues.append(f"{e.get('id')}: 객체/작업 연결 없음")
                else:
                    evidence = root / "evidence" / (e["source_id"] + ".json")
                    def contains(v):
                        if isinstance(v, dict):
                            return any(contains(x) for x in v.values())
                        if isinstance(v, list):
                            return any(contains(x) for x in v)
                        return v == ref["object_key"]
                    if not evidence.is_file() or not contains(read(evidence)):
                        issues.append(f"{e.get('id')}: 해당 원본 DB 증거에 객체 키 없음")
            elif p.parent.name == "evidence":
                if p.stem != e.get("source_id"):
                    issues.append(f"{e.get('id')}: 다른 원본의 DB 증거")
                binding = by_source.get(e.get("source_id"))
                data = read(p)
                if not binding or not any(j.get("id") == binding["job_id"] for j in data.get("job", [])):
                    issues.append(f"{e.get('id')}: DB 증거의 작업 ID 불일치")
    return issues


def record_cost(run, e):
    required = ("id", "category", "basis", "currency", "applied_at", "evidence", "reviewer")
    if any(not e.get(k) for k in required):
        raise ValueError("비용 식별자·구분·근거·통화·적용일·증거·확인자 필요")
    if e["category"] not in ("generation", "evaluation", "infrastructure", "calibration"):
        raise ValueError("비용 구분 오류")
    if e.get("kind") not in ("actual", "estimate", "unpriced"):
        raise ValueError("실청구/추정/미산정 구분 필요")
    amount = e.get("amount")
    if e["kind"] == "unpriced":
        if amount is not None or not e.get("reason"):
            raise ValueError("미산정은 금액 null과 사유 필요")
    elif type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
        raise ValueError("유효한 금액 필요")
    if e["kind"] == "estimate" and not e.get("rate_source"):
        raise ValueError("추정 비용의 단가 출처 필요")
    history = rows(Path(run) / "costs.jsonl")
    if any(x["id"] == e["id"] for x in history):
        raise ValueError("비용 ID 중복")
    current = [x for x in history if x["category"] == e["category"]]
    if current and (e.get("supersedes") != current[-1]["id"] or not e.get("revision_reason")):
        raise ValueError("구분별 회차 총액 정정은 이전 ID와 사유 필요")
    issues = evidence_issues(run, [e])
    if issues:
        raise ValueError("; ".join(issues))
    append(Path(run) / "costs.jsonl", {**e, "recorded_at": now()})


def cost_summary(events, execution, usage):
    latest = {e["category"]: e for e in events}
    categories = {k: latest.get(k) for k in ("generation", "evaluation", "infrastructure", "calibration")}
    selected = [categories[k] for k in ("generation", "infrastructure")]
    priced = all(e and e.get("amount") is not None for e in selected)
    same_currency = priced and len({e["currency"] for e in selected}) == 1
    total = sum(e["amount"] for e in selected) if same_currency else None
    attempted = len({e["source_id"] for e in execution if e.get("source_id") in IDS})
    completed = len({e["source_id"] for e in execution if e.get("event") == "finished" and e["status"] in ("completed", "all_excluded")})
    return {"categories": categories, "execution_total": total,
            "currency": selected[0]["currency"] if same_currency else None,
            "reason": None if same_currency else "미산정 항목 또는 통화 불일치",
            "attempted": attempted, "completed": completed,
            "per_attempted": total / attempted if total is not None and attempted else None,
            "per_completed": total / completed if total is not None and completed else None,
            "calls_by_model": dict(Counter(e.get("model") or "확인 불가" for e in usage)),
            "failed_calls": sum(e.get("status") == "failed" for e in usage),
            "usage_missing": sum(e.get("usage") is None for e in usage)}


def load_resolved_runs(paths, baseline, current):
    """명시적으로 지정된 이전 회차만 읽고 시점·입력·기준·근거를 대조한다."""
    from datetime import datetime
    baseline, current = Path(baseline), Path(current)
    bc, cc = read(baseline / "config.json"), read(current / "config.json")
    start = datetime.fromisoformat(read(baseline / "started.json")["at"])
    end = datetime.fromisoformat(read(current / "started.json")["at"])
    inputs = read(baseline / "inputs.json")
    frozen = read(baseline / "frozen.json")
    result = {}
    for root in map(Path, paths):
        cfg = read(root / "config.json")
        rid = cfg["run_id"]
        at = datetime.fromisoformat(read(root / "started.json")["at"])
        if rid in (bc["run_id"], cc["run_id"]) or rid in result or not start < at < end:
            raise ValueError("재발 근거는 baseline 이후·현재 이전의 별도 회차여야 합니다")
        if [(e["source_id"], e["sha256"]) for e in read(root / "inputs.json")] != [(e["source_id"], e["sha256"]) for e in inputs]:
            raise ValueError("해결 회차의 입력 불일치")
        for key in ("protocol", "evaluation_model", "viewing_conditions", "products"):
            if cfg.get(key) != bc.get(key):
                raise ValueError("해결 회차의 평가 조건 불일치")
        rf = read(root / "frozen.json")
        if {k: v["sha256"] for k, v in rf.items()} != {k: v["sha256"] for k, v in frozen.items()} or any(digest(root / e["file"]) != e["sha256"] for e in rf.values()):
            raise ValueError("해결 회차의 동결 자료 불일치")
        events = rows(root / "defects.jsonl")
        if any(not e.get("evidence") or not e.get("reviewer") for e in events) or evidence_issues(root, events):
            raise ValueError("해결 회차의 결함 근거 누락/불일치")
        result[rid] = events
    return result


def defect_summary(before, after, baseline_evaluations=(), resolved_runs=None, after_evaluations=None):
    # 지표 분리 전에 정정 이력을 접는다. 지표를 옮긴 이전 기록을 되살리지 않는다.
    before = list({(e["source_id"], e["defect_id"]): e for e in before}.values())
    after = list({(e["source_id"], e["defect_id"]): e for e in after}.values())
    current = None if after_evaluations is None else list(adopted_evaluations(after_evaluations).values())
    result = _defect_summary(before, after, baseline_evaluations, resolved_runs or {}, bool(baseline_evaluations), current)
    result["by_metric"] = {str(m): _defect_summary(
        [e for e in before if e.get("metric") == m], [e for e in after if e.get("metric") == m],
        [e for e in baseline_evaluations if e.get("metric") == m], resolved_runs or {}, bool(baseline_evaluations),
        None if current is None else [e for e in current if e.get("metric") == m]) for m in range(1, 8)}
    return result


def _defect_summary(before, after, baseline_evaluations, resolved_runs, authoritative, current):
    base = {(e["source_id"], e["defect_id"]): e for e in before}
    latest = {(e["source_id"], e["defect_id"]): e for e in after}
    matches, issues = {}, []
    excluded = []
    if current is not None:
        active = {(e["source_id"], e["metric"], did) for e in current if e["grade"] in ("B", "C") for did in e.get("defect_ids", [])}
        represented = {(e["source_id"], e["metric"], did) for e in latest.values()
                       for did in (e["defect_id"], e.get("baseline_defect_id")) if did}
        for sid, metric, did in sorted(active - represented):
            issues.append({"defect": [sid, did], "metric": metric, "reason": "후속 최종 판정의 결함 상세 기록 없음"})
        for key, e in list(latest.items()):
            present = any((e["source_id"], e["metric"], did) in active
                          for did in (e["defect_id"], e.get("baseline_defect_id")) if did)
            if e.get("change") in ("신규", "기존 결함 뒤늦은 발견", "완화", "유지", "악화") and not present:
                excluded.append({"source_id": key[0], "defect_id": key[1], "metric": e["metric"], "reason": "후속 최종 B/C 판정에서 채택되지 않음"})
                del latest[key]
            elif e.get("change") == "해결" and present:
                issues.append({"defect": list(key), "reason": "해결 기록과 후속 최종 B/C 판정 불일치"})
                del latest[key]
    adopted = list(adopted_evaluations(baseline_evaluations).values())
    if authoritative:
        active = {(e["source_id"], e["metric"], did) for e in adopted if e["grade"] in ("B", "C") for did in e.get("defect_ids", [])}
        base = {key: e for key, e in base.items() if (key[0], e["metric"], key[1]) in active}
    for e in adopted:
        if e["grade"] in ("B", "C"):
            for did in e.get("defect_ids", []):
                if (e["source_id"], did) not in base:
                    issue = {"defect": [e["source_id"], did], "reason": "baseline 판정의 결함 상세 기록 없음"}
                    if issue not in issues:
                        issues.append(issue)
    for key, e in latest.items():
        if e.get("change") is None:
            issues.append({"defect": list(key), "reason": "후속 결함 변화 상태 미기록"})
        target = (e["source_id"], e.get("baseline_defect_id"))
        if e.get("baseline_defect_id"):
            if target not in base:
                issues.append({"defect": list(key), "reason": "baseline 대응 없음"})
                continue
            if e.get("metric") != base[target].get("metric") or not e.get("match_reason"):
                issues.append({"defect": list(key), "reason": "지표 불일치 또는 대응 근거 없음"})
                continue
            matches.setdefault(target, []).append(e)
    counts = Counter({k: 0 for k in ("해결", "완화", "유지", "악화", "평가 불가")})
    recurrence = 0
    for target in base:
        es = matches.get(target, [])
        states = {e.get("change") for e in es}
        if not es or len(states) != 1 or next(iter(states)) not in counts:
            issues.append({"defect": list(target), "reason": "대응 미기록 또는 분할·병합 상태 불일치"})
            continue
        counts[next(iter(states))] += 1
        verified_recurrence = False
        for e in es:
            if not e.get("recurrence"):
                continue
            previous = resolved_runs.get(e.get("previous_resolved_run"), [])
            previous = {(x["source_id"], x["defect_id"]): x for x in previous}
            related = [x for x in previous.values() if x.get("baseline_defect_id") == target[1] and x["source_id"] == target[0] and x.get("metric") == e.get("metric")]
            if e["change"] not in ("완화", "유지", "악화") or not related or any(x.get("change") != "해결" for x in related):
                issues.append({"defect": list(target), "reason": "재발의 이전 해결 이력 미확인/상태 불일치"})
            else:
                verified_recurrence = True
        recurrence += verified_recurrence
    return {"baseline_defects": len(base), "counts": dict(counts), "recurrence": recurrence,
            "new": sum(e.get("change") == "신규" and not e.get("baseline_defect_id") for e in latest.values()),
            "late_discovered": sum(e.get("change") == "기존 결함 뒤늦은 발견" for e in latest.values()),
            "excluded_current": excluded, "issues": issues}
