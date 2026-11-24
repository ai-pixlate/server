"""python -m tools.baseline.run --help. 기본 명령은 모델·DB를 로드하지 않는다."""
import argparse
import importlib.util
import json
from pathlib import Path

from .core import prepare, template, read, write_new, record_evaluation, record_detail, policy_summary, rows, quality_summary, execution_summary, digest
from .review import review_plan, transitions
from .audit import policy_regions, evidence_issues, record_cost, cost_summary, defect_summary, load_resolved_runs
from . import protocol


def main(argv=None):
    p = argparse.ArgumentParser(description="baseline 준비·격리 실행·평가 기록")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("template", help="실제 값이 비어 있는 회차 설정 생성")
    s.add_argument("output", type=Path)
    s = sub.add_parser("prepare", help="입력 확인·빈 평가표 생성. 모델·DB 호출 없음")
    s.add_argument("config", type=Path)
    s.add_argument("output", type=Path)
    s = sub.add_parser("doctor", help="패키지 설치 여부만 확인. import·연결 없음")
    s.add_argument('--input-root', type=Path)
    s.add_argument('--product-root', type=Path)
    s.add_argument('--expected-commit')
    s.add_argument('--asset-manifest', type=Path)
    s = sub.add_parser('estimate', help='과거 저장 기록만 읽는 호출량 시나리오')
    s.add_argument('repository', type=Path)
    s = sub.add_parser('capture', help='기존 산출물 불변 복사. 모델·렌더 호출 없음')
    s.add_argument('run', type=Path)
    s.add_argument('metadata', type=Path)
    s.add_argument('file', type=Path)
    s = sub.add_parser("validate", help="최신 회차의 파일·해시·참조·상태 검증")
    s.add_argument("run", type=Path)
    s = sub.add_parser("execute", help="별도 실측 승인 후에만 사용")
    s.add_argument("run", type=Path)
    s.add_argument("--execute-models", action="store_true")
    s = sub.add_parser("record", help="별도 평가자가 작성한 판정 1건 추가")
    s.add_argument("run", type=Path)
    s.add_argument("event", type=Path)
    s.add_argument("--kind", choices=tuple(protocol.FIELDS), default="evaluations")
    s = sub.add_parser("summary", help="미판정·미검수 상태를 보존한 잠정 집계")
    s.add_argument("run", type=Path)
    s.add_argument("output", type=Path)
    s.add_argument("--baseline", type=Path)
    s = sub.add_parser("packet", help="1차 판정이 없는 독립 평가 자료 색인")
    s.add_argument("run", type=Path)
    s.add_argument("output", type=Path)
    s = sub.add_parser("compare", help="동일 입력 회차의 등급 전이표")
    s.add_argument("before", type=Path)
    s.add_argument("after", type=Path)
    s.add_argument("output", type=Path)
    s.add_argument("--resolved-run", type=Path, action="append", default=[])
    a = p.parse_args(argv)
    if a.command == "template":
        write_new(a.output, protocol.template())
    elif a.command == "doctor":
        if a.input_root:
            from .preflight import inspect
            print(json.dumps(inspect(a.input_root, a.product_root, a.expected_commit, a.asset_manifest), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({m: importlib.util.find_spec(m) is not None for m in
              ("PIL", "sqlalchemy", "psycopg", "alembic", "celery", "fastapi", "boto3", "paddle", "torch")}, indent=2))
    elif a.command == 'estimate':
        from .estimate import historical
        print(json.dumps(historical(a.repository), ensure_ascii=False, indent=2))
    elif a.command == 'capture':
        protocol.capture_artifact(a.run, read(a.metadata), a.file)
    elif a.command == "prepare":
        missing = protocol.prepare(read(a.config), a.output)
        print(json.dumps({"missing": missing, "executed": False}, ensure_ascii=False, indent=2))
    elif a.command == "execute":
        from .be import run_prepared
        run_prepared(a.run, execute=a.execute_models)
    elif a.command == "validate":
        issues = protocol.validate(a.run)
        print(json.dumps({"issues": issues, "executed": False}, ensure_ascii=False, indent=2))
        return 1 if issues else 0
    elif a.command == "record":
        protocol.record(a.run, a.kind, read(a.event))
    elif a.command == "summary":
        if (a.run / 'manifest.json').exists():
            protocol.export(a.run, a.output)
            return 0
        raise ValueError('구형 7지표 묶음은 최신 공식 집계로 사용할 수 없습니다')
    elif a.command == "packet":
        if (a.run / 'manifest.json').exists():
            manifest = read(a.run / 'manifest.json')
            write_new(a.output, {"instructions": "Codex 채팅 직접 검수. H01/H05 추정 금지. H02/H03 등급은 결함에서 계산.",
                               "manifest": manifest, "reference_units": rows(a.run / 'reference_units.jsonl')})
            return 0
        config = read(a.run / "config.json")
        frozen = read(a.run / "frozen.json")
        write_new(a.output, {"instructions": "원본·증거·확정 명세로 독립 판정. 1차 판정 파일은 열지 않는다.",
                  "inputs": read(a.run / "inputs.json"), "regions": read(a.run / "regions.json"),
                  "frozen": {k: str((a.run / v["file"]).resolve()) for k, v in frozen.items()},
                  "viewing_conditions": config["viewing_conditions"],
                  "evidence_files": [str(p.resolve()) for p in sorted((a.run / "evidence").glob("*.json"))],
                  "stage_records": str((a.run / "stages.jsonl").resolve()),
                  "objects": str((a.run / "objects").resolve()),
                  "object_index": str((a.run / "object-index.json").resolve())})
    elif a.command == "compare":
        before, after = read(a.before / "inputs.json"), read(a.after / "inputs.json")
        if len(before) != 34 or [(i["source_id"], i["sha256"]) for i in before] != [(i["source_id"], i["sha256"]) for i in after]:
            raise ValueError("동일한 34장 원본 묶음이 아닙니다")
        ca, cb = read(a.before / "config.json"), read(a.after / "config.json")
        for key in ("protocol", "evaluation_model", "viewing_conditions", "products"):
            if ca.get(key) != cb.get(key):
                raise ValueError(f"비교 조건 불일치: {key}")
        fa, fb = read(a.before / "frozen.json"), read(a.after / "frozen.json")
        for root, frozen in ((a.before, fa), (a.after, fb)):
            if any(digest(root / e["file"]) != e["sha256"] for e in frozen.values()):
                raise ValueError("비교 자료의 실제 파일 해시 불일치")
        if {k: v["sha256"] for k, v in fa.items()} != {k: v["sha256"] for k, v in fb.items()}:
            raise ValueError("평가 명세·기준 자료·평가 프롬프트 해시가 다릅니다")
        write_new(a.output, {"formal_complete": False, "transitions": transitions(
            rows(a.before / "evaluations.jsonl"), rows(a.after / "evaluations.jsonl")),
            "defects": defect_summary(rows(a.before / "defects.jsonl"), rows(a.after / "defects.jsonl"), rows(a.before / "evaluations.jsonl"),
                                      load_resolved_runs(a.resolved_run, a.before, a.after) if a.resolved_run else {},
                                      after_evaluations=rows(a.after / "evaluations.jsonl")),
            "review": review_plan(rows(a.after / "evaluations.jsonl"), cb.get("run_id"),
                                  baseline=rows(a.before / "evaluations.jsonl")),
            "evidence_issues": {"before": evidence_issues(a.before, rows(a.before / "evaluations.jsonl")),
                                "after": evidence_issues(a.after, rows(a.after / "evaluations.jsonl"))}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
