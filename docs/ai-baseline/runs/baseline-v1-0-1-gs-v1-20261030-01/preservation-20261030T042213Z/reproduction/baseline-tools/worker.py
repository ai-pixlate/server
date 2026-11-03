"""측정 전용 프로세스 진입점. 승인 옵션 없이는 제품 코드를 로드하지 않는다."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="격리 baseline 작업 프로세스")
    parser.add_argument("run", type=Path)
    parser.add_argument("--execute-models", action="store_true", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.baseline.be import _run_prepared
    _run_prepared(args.run, args.execute_models)


if __name__ == "__main__":
    main()
