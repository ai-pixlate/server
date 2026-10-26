"""모델·DB 의존성이 없는 입력 확인, 평가 이력과 집계."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import shutil
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

IDS = tuple(f"GS-{g:02d}_{i:03d}" for g, n in ((1, 2), (2, 14), (3, 18)) for i in range(1, n + 1))
GRADES = ("A", "B", "C", "판단 불가", "해당 없음", "산출물 없음")
ROLES = ("primary", "independent", "human")
REGIONS = (
    ("K01", "GS-01_002", [0, 5808, 1000, 6774]),
    ("K02", "GS-01_002", [0, 22303, 1000, 24460]),
    ("K03", "GS-01_002", [0, 33684, 1000, 36365]),
    ("K04", "GS-02_002", [0, 0, 1000, 2117]),
    ("K05", "GS-02_007", [0, 230, 830, 2687]),
    ("K06", "GS-02_014", [0, 10738, 830, 14010]),
    ("K07", "GS-03_002", [0, 5311, 1000, 8167]),
    ("K08", "GS-03_018", [0, 4499, 1000, 7493]),
    ("K09", "GS-03_018", [0, 9155, 1000, 10785]),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str, allow_nan=False)


def append(path, data):
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str, allow_nan=False) + "\n")
        f.flush()


def rows(path):
    p = Path(path)
    return [json.loads(s) for s in p.read_text(encoding="utf-8").splitlines() if s.strip()] if p.exists() else []


def template():
    return {"run_id": None, "product_root": None, "product_commit": None,
            "protocol": "v1", "input_root": None, "reference_file": None,
            "reference_confirmed_by": None, "data_confirmed_by": None,
            "evaluation_model": None, "evaluation_prompt_file": None,
            "viewing_conditions": None, "calibration_confirmed_by": None,
            "human_reviewer": None, "budget": None,
            "environment": {"hardware": None, "resource_limits": None, "concurrent_work": None, "model_assets": []},
            "products": {f"GS-{g:02d}": {"product_name": None, "regulatory_class": None,
                         "internal_category": None, "name_ko": None, "name_en": None,
                         "target_country": "US", "target_lang": "en"} for g in range(1, 4)}}


def inspect_inputs(config):
    """사용자가 지정한 폴더만 읽는다. 실행 결과나 기준 자료를 정답으로 생성하지 않는다."""
    from PIL import Image
    if not config.get("input_root"):
        return [], ["input_root: 입력 묶음 지정 필요"]
    root = Path(config["input_root"]).resolve()
    found, issues = [], []
    hashes = set()
    for sid in IDS:
        p = root / sid[:5] / (sid + ".jpg")
        if not p.is_file():
            issues.append(f"{sid}: 파일 없음")
            continue
        if not p.resolve().is_relative_to(root):
            issues.append(f"{sid}: 입력 폴더 밖 링크")
            continue
        try:
            with Image.open(p) as im:
                width, height = im.size
                im.verify()
            sha = digest(p)
            if sha in hashes:
                issues.append(f"{sid}: 다른 원본과 동일한 바이트 — 확인 필요")
            hashes.add(sha)
            found.append({"source_id": sid, "path": str(p), "sha256": sha,
                          "width": width, "height": height, "area": width * height,
                          "bytes": p.stat().st_size})
            for rid, source, box in REGIONS:
                if source == sid and (box[2] > width or box[3] > height):
                    issues.append(f"{rid}: 원본 크기 밖 좌표")
        except (OSError, ValueError) as e:
            issues.append(f"{sid}: 이미지 검증 실패({type(e).__name__})")
    return found, issues


def prepare(config, out):
    """공식 실행이 아닌 빈 기록 묶음. 결과를 만들거나 평가하지 않는다."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    inputs, missing = inspect_inputs(config)
    for field in ("run_id", "product_root", "product_commit", "reference_file", "reference_confirmed_by",
                  "data_confirmed_by", "evaluation_model", "evaluation_prompt_file", "viewing_conditions",
                  "calibration_confirmed_by", "human_reviewer", "budget"):
        if not config.get(field):
            missing.append(f"{field}: 제공 또는 확인 필요")
    for group in ("GS-01", "GS-02", "GS-03"):
        for field in ("product_name", "regulatory_class", "internal_category", "name_ko", "name_en"):
            if not config.get("products", {}).get(group, {}).get(field):
                missing.append(f"{group}.{field}: 제공 필요")
    for key in ("hardware", "resource_limits", "concurrent_work", "model_assets"):
        if not config.get("environment", {}).get(key):
            missing.append(f"environment.{key}: 제공 필요")
    for field in ("reference_file", "evaluation_prompt_file"):
        if config.get(field) and not Path(config[field]).is_file():
            missing.append(f"{field}: 파일 없음")
    frozen = {}
    for field in ("reference_file", "evaluation_prompt_file"):
        if config.get(field) and Path(config[field]).is_file():
            src = Path(config[field])
            dest = out / (field + src.suffix)
            shutil.copyfile(src, dest)
            frozen[field] = {"file": dest.name, "sha256": digest(dest)}
    protocol_root = Path(__file__).resolve().parents[2] / "docs" / "ai-baseline" / "protocols" / "v1"
    # tools/baseline -> tools -> repository
    for src in sorted(protocol_root.glob("*.md")):
        dest = out / "protocol" / src.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(src, dest)
        frozen["protocol/" + src.name] = {"file": "protocol/" + src.name, "sha256": digest(dest)}
    write_new(out / "frozen.json", frozen)
    if "reference_file" in frozen:
        from .audit import policy_regions
        try:
            if policy_regions(out) is None:
                missing.append("reference_file.policy_regions: 기준 영역 목록 필요")
        except (ValueError, KeyError, TypeError):
            missing.append("reference_file: 구조화된 기준 자료 확인 필요")
    write_new(out / "config.json", config)
    write_new(out / "inputs.json", inputs)
    write_new(out / "preparation.json", {"kind": "preparation", "created_at": now(),
              "missing": missing, "config_sha256": digest(out / "config.json"),
              "inputs_sha256": digest(out / "inputs.json"),
              "execution_verified": False, "formal_measurement": False})
    write_new(out / "regions.json", [{"region_id": r, "source_id": s, "box": b} for r, s, b in REGIONS])
    with (out / "evaluation-sheet.csv").open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_id", "metric", "grade", "evidence", "reason", "reviewer"])
        writer.writerows((sid, metric, "", "", "", "") for sid in IDS for metric in range(1, 8))
    with (out / "reference-sheet.csv").open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_id", "key_facts", "protected_regions", "expected_actions", "policy_basis", "original_defects", "confirmed_by"])
        writer.writerows((sid, "", "", "", "", "", "") for sid in IDS)
    for filename in ("evaluations.jsonl", "execution.jsonl", "usage.jsonl", "defects.jsonl", "policy.jsonl"):
        (out / filename).touch(exist_ok=False)
    return missing


def record_evaluation(out, event):
    """평가 추가만 허용한다. AI·사람 판정과 수정 이력을 덮어쓰지 않는다."""
    required = ("id", "source_id", "metric", "role", "reviewer", "grade", "scope", "evidence", "reason", "session")
    if any(k not in event or event[k] in (None, "", []) for k in required):
        raise ValueError("평가 식별자·범위·평가자·근거·이유·세션을 모두 기록해야 합니다")
    if event["source_id"] not in IDS or type(event["metric"]) is not int or event["metric"] not in range(1, 8):
        raise ValueError("원본 또는 지표 범위 오류")
    if event["grade"] not in GRADES or event["role"] not in ROLES:
        raise ValueError("판정 또는 평가자 역할 오류")
    if event.get("phase", "formal") not in ("formal", "calibration"):
        raise ValueError("평가 구분 오류")
    if event["scope"] != "original" and not (event["scope"] in {x[0] for x in REGIONS} or re.fullmatch(r"D-[A-Za-z0-9_-]+", event["scope"])):
        raise ValueError("범위는 original, K01~K09 또는 D-결함ID")
    if event["scope"] in {x[0] for x in REGIONS} and not any(r == event["scope"] and s == event["source_id"] for r, s, _ in REGIONS):
        raise ValueError("핵심 영역과 원본 불일치")
    if event["grade"] in ("B", "C") and not all(event.get(k) for k in ("defect_ids", "locations", "observation", "expected", "defect_type")):
        raise ValueError("B/C에는 결함 ID·위치·관찰·기대 조건·유형 필요")
    if event["grade"] == "A" and event.get("coverage_complete") is not True:
        raise ValueError("A에는 해당 평가 범위의 확인 완료 표시 필요")
    history = rows(Path(out) / "evaluations.jsonl")
    if any(x["id"] == event["id"] for x in history):
        raise ValueError("평가 ID 중복")
    key = lambda x: (x["source_id"], x["metric"], x["scope"], x["role"], x.get("phase", "formal"))
    previous = [x for x in history if key(x) == key(event)]
    if previous and (event.get("supersedes") != previous[-1]["id"] or not event.get("revision_reason")):
        raise ValueError("재평가는 직전 판정 ID와 수정/기술적 재시도 이유 필요")
    if event["role"] == "independent":
        primary = [x for x in history if key(x)[:3] == key(event)[:3] and x["role"] == "primary" and key(x)[4] == key(event)[4]]
        if event.get("blind_review") is not True or any(x["session"] == event["session"] for x in primary):
            raise ValueError("재검토는 1차와 별도 세션·비공개 판정 필요")
    from .audit import evidence_issues
    for rid in event.get("region_evaluation_ids", []):
        matching = [x for x in history if x["id"] == rid and x["source_id"] == event["source_id"] and x["metric"] == event["metric"] and x["scope"] != "original" and x.get("phase", "formal") == "formal"]
        if not matching:
            raise ValueError("원본 재판정의 영역 근거 ID 오류")
    issues = evidence_issues(out, [event])
    if issues:
        raise ValueError("; ".join(issues))
    append(Path(out) / "evaluations.jsonl", {**event, "recorded_at": now()})


def latest_evaluations(history):
    result = {}
    for e in history:
        if e.get("phase", "formal") != "formal":
            continue
        result[(e["source_id"], e["metric"], e["scope"], e["role"])] = e
    return result


def adopted_evaluations(history):
    """같은 범위의 최종 채택 판정. 사람 정정 전 AI 불일치는 채택하지 않는다."""
    latest = latest_evaluations(history)
    result = {}
    for key in {k[:3] for k in latest}:
        p, i, h = [latest.get((*key, role)) for role in ROLES]
        if h:
            result[key] = h
        elif p and not (i and (p["grade"] != i["grade"] or set(p.get("defect_ids", [])) != set(i.get("defect_ids", [])))):
            result[key] = p
    return result


def record_detail(out, kind, event):
    """영역 기대값과 결함 대응은 외부 평가자가 작성한다. 자동 추론하지 않는다."""
    if kind not in ("policy", "defects"):
        raise ValueError("지원하지 않는 기록 종류")
    for key in ("id", "source_id", "evidence", "reviewer"):
        if not event.get(key):
            raise ValueError(f"필수 기록: {key}")
    if event["source_id"] not in IDS:
        raise ValueError("평가셋 밖 원본")
    if kind == "policy":
        if not event.get("region_id") or not event.get("location") or not event.get("text"):
            raise ValueError("정책 기준 영역 ID·좌표·문구 필요")
        for key in ("expected_exclude", "actual_exclude", "expected_replace", "actual_replace"):
            if key not in event or (event[key] is not None and type(event[key]) is not bool):
                raise ValueError("정책 기대/실제 값은 true·false·null")
        if not event.get("basis"):
            raise ValueError("정책 기대 처리 근거 필요")
        if any(event[k] is None for k in ("actual_exclude", "actual_replace")) and not event.get("exception_reason"):
            raise ValueError("판정 불가·산출물 없음 사유 필요")
        if any(event[k] is None for k in ("actual_exclude", "actual_replace")) and event.get("exception_state") not in ("판단 불가", "산출물 없음"):
            raise ValueError("판단 불가/산출물 없음 구분 필요")
    else:
        for key in ("defect_id", "location", "defect_type", "observation", "expected", "metric"):
            if not event.get(key):
                raise ValueError(f"결함 기록 필요: {key}")
        if event["metric"] not in range(1, 8):
            raise ValueError("품질 지표 번호 오류")
        if event.get("change") not in (None, "해결", "완화", "유지", "악화", "평가 불가", "신규", "기존 결함 뒤늦은 발견"):
            raise ValueError("결함 변화 상태 오류")
        if event.get("change") in ("해결", "완화", "유지", "악화", "평가 불가") and not event.get("baseline_defect_id"):
            raise ValueError("baseline 결함 대응 ID 필요")
        if event.get("recurrence") and not event.get("previous_resolved_run"):
            raise ValueError("재발에는 이전 후속 회차의 해결 기록 필요")
    path = Path(out) / (kind + ".jsonl")
    old = rows(path)
    if any(x["id"] == event["id"] for x in old):
        raise ValueError("기록 ID 중복")
    target = "region_id" if kind == "policy" else "defect_id"
    previous = [x for x in old if (x["source_id"], x[target]) == (event["source_id"], event[target])]
    if previous and (event.get("supersedes") != previous[-1]["id"] or not event.get("revision_reason")):
        raise ValueError("동일 영역/결함 수정에는 직전 ID·사유 필요")
    from .audit import evidence_issues
    issues = evidence_issues(out, [event])
    if issues:
        raise ValueError("; ".join(issues))
    append(path, {**event, "recorded_at": now()})


def policy_summary(events, regions=None):
    latest = {(e["source_id"], e["region_id"]): e for e in events}
    expected_regions = {(e["source_id"], e["region_id"]): e for e in regions} if regions is not None else None
    domain = expected_regions if expected_regions is not None else latest
    result = {}
    for dimension in ("exclude", "replace"):
        counts = Counter({k: 0 for k in ("TP", "FN", "FP", "TN", "판단 불가", "산출물 없음", "미기록", "기준 불일치")})
        for key in domain:
            e = latest.get(key)
            if e is None:
                counts["미기록"] += 1
                continue
            expected, actual = e["expected_" + dimension], e["actual_" + dimension]
            if expected_regions is not None and expected != expected_regions[key].get("expected_" + dimension):
                counts["기준 불일치"] += 1
                continue
            counts[(e.get("exception_state", "판단 불가") if actual is None else "판단 불가") if expected is None or actual is None else
                   ("TP" if actual else "FN") if expected else ("FP" if actual else "TN")] += 1
        result[dimension] = {"counts": dict(counts), "total_regions": len(domain) if regions is not None else None,
                             "recorded_regions": len(latest), "assessable_regions": sum(counts[k] for k in ("TP", "FN", "FP", "TN")),
                             "unexpected_regions": [list(k) for k in latest if k not in domain],
                             "reference_missing": regions is None}
    return result


def resolved_original(latest, sid, metric):
    """영역 정정은 전체 재판정을 요구한다. 다른 지표로 등급을 전파하지 않는다."""
    def choose(scope):
        p, i, h = [latest.get((sid, metric, scope, r)) for r in ROLES]
        conflict = p and i and (p["grade"] != i["grade"] or set(p.get("defect_ids", [])) != set(i.get("defect_ids", [])))
        return h or (None if conflict else p), bool(conflict and not h)
    original, conflict = choose("original")
    if not original or conflict:
        return "미판정"
    acknowledged = set(original.get("region_evaluation_ids", []))
    for scope in {k[2] for k in latest if k[:2] == (sid, metric) and k[2] != "original"}:
        region, conflict = choose(scope)
        if conflict:
            return "미판정"
        if region is None:
            region = latest.get((sid, metric, scope, "independent"))
        human = latest.get((sid, metric, scope, "human"))
        # 사람 정정 후 원본 재판정이 그 기록을 명시적으로 반영해야 한다.
        if human and human.get("id") not in acknowledged:
            return "미판정"
        if region and region["grade"] in ("판단 불가", "산출물 없음") and original["grade"] == "A":
            return "미판정"
        if region and region["grade"] in "BC" and (original["grade"] not in "ABC" or "ABC".index(region["grade"]) > "ABC".index(original["grade"])):
            return "미판정"
    return original["grade"]


def quality_summary(history):
    latest = latest_evaluations(history)
    result = {}
    for group in ("all", "GS-01", "GS-02", "GS-03"):
        ids = [s for s in IDS if group == "all" or s.startswith(group)]
        metrics = {}
        for m in range(1, 8):
            counts = Counter()
            for sid in ids:
                counts[resolved_original(latest, sid, m)] += 1
            n = sum(counts[g] for g in ("A", "B", "C"))
            metrics[str(m)] = {"counts": dict(counts), "graded": n, "planned": len(ids),
                               "a_ratio": counts["A"] / n if n else None}
        result[group] = metrics
    return result


def time_summary(values):
    if not values:
        return {"n": 0, "median": None, "p90": None, "max": None}
    values = sorted(values)
    return {"n": len(values), "median": statistics.median(values),
            "p90": values[math.ceil(.9 * len(values)) - 1], "max": values[-1]}


def execution_summary(events):
    latest = {}
    for e in events:
        if e.get("event") == "finished":
            if e["source_id"] not in IDS or e.get("status") not in ("completed", "all_excluded", "partial_failure", "failed", "interrupted"):
                raise ValueError("실행 원본/종료 상태 오류")
            if type(e.get("seconds")) not in (int, float) or not math.isfinite(e["seconds"]) or e["seconds"] < 0:
                raise ValueError("처리 시간 오류")
            if e["source_id"] in latest:
                raise ValueError("같은 원본의 공식 생성 결과가 중복됨")
            latest[e["source_id"]] = e
    counts = Counter(e["status"] for e in latest.values())
    good = [e for e in latest.values() if e["status"] in ("completed", "all_excluded")]
    return {"metric_definitions": {
                "scope": "고정 평가셋 baseline 실행 지표; O1 대시보드 지표와 별개",
                "unit": "원본 이미지 1장",
                "completion_rate": {
                    "label": "원본 이미지 기술 처리 완료 비율",
                    "denominator": "계획된 전체 원본 수",
                    "denominator_count": len(IDS),
                    "included_statuses": ["completed", "all_excluded"],
                    "excluded_statuses": ["partial_failure", "failed", "interrupted"],
                    "unexecuted_in_denominator": True,
                    "completion_condition": "기술 처리 종료; 산출물 생성 완료와 전체 제외 종료는 별도 건수 표시"},
                "cost": {
                    "per_attempted": "시도 원본당 실행 비용: 생성·인프라 비용 합계 / 시도 원본 수",
                    "per_completed": "기술 처리 완료 원본당 실행 비용: 생성·인프라 비용 합계 / 기술 처리 완료 원본 수",
                    "interpretation": "O1 건당 원가가 아님; 미산정 비용은 0원으로 보지 않음"},
                "metric_7": "사전·정책 기대 처리 대조; 규제 검출 재현율 및 NFR-14 충족 근거로 사용하지 않음"},
            "planned": len(IDS), "terminal": len(latest), "unexecuted": len(IDS) - len(latest),
            "first_attempt_completed": sum(e.get("retry_count") == 0 for e in good),
            "completed_after_retry": sum(e.get("retry_count", 0) > 0 for e in good),
            "retry_status_unknown": sum("retry_count" not in e for e in good),
            "counts": dict(counts), "completion_rate": len(good) / len(IDS),
            "completed_seconds": time_summary([e["seconds"] for e in good]),
            "failed_seconds": time_summary([e["seconds"] for e in latest.values() if e not in good])}


class SequentialDispatcher:
    """전달만 교체한다. 재시도·종료 결정은 BE 함수에 남긴다."""
    def __init__(self, resolve, record):
        self.queue = deque()
        self.resolve, self.record = resolve, record

    def __call__(self, task_name, args):
        self.queue.append((task_name, list(args)))

    def drain(self):
        import time
        while self.queue:
            name, args = self.queue.popleft()
            started = time.monotonic()
            self.record({"event": "task_started", "task": name, "args": args, "at": now()})
            try:
                self.resolve(name)(*args)
            finally:
                self.record({"event": "task_finished", "task": name, "args": args,
                             "seconds": time.monotonic() - started, "at": now()})
