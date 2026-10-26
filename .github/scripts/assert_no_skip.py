"""JUnit XML 판정 — 시험이 1개 이상 실행되고 건너뜀·실패·오류가 모두 0일 때만 종료 코드 0.

pytest 에는 "건너뛰면 실패" 옵션이 없다. 필수 시험(예: PIXLATE_TEST_DATABASE_URL 이 없으면 통째로 skip 되는
tests/test_exec_recovery.py)이 조용히 건너뛰어도 통과로 끝나지 않게, CI 가 pytest --junitxml 결과를 이것으로 다시 판정한다.

    python .github/scripts/assert_no_skip.py required.xml
"""
import sys
import xml.etree.ElementTree as ET


def main(path: str) -> int:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "skipped", "failures", "errors")}
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    ok = counts["tests"] > 0 and counts["skipped"] == counts["failures"] == counts["errors"] == 0
    if not ok:
        print("필수 시험 판정 실패: 실행 0건이거나 건너뜀·실패·오류가 있다")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
