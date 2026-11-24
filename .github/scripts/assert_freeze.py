"""설치된 패키지 목록(pip freeze --all)이 constraints.txt 와 정확히 같은지 판정한다(CI 러너·이미지 공용).

이름은 PEP 503 으로 정규화해 (이름, 버전) 집합으로 비교한다. 목록에 없는 패키지(추가)·버전 차이·목록에 있는데
설치되지 않은 패키지(누락) 중 하나라도 있으면 실패. `이름==버전` 이 아닌 줄(편집 설치·URL 등)도 실패.
사용: python assert_freeze.py constraints.txt pip-freeze.txt
"""
import re
import sys


def parse(path: str) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    bad: list[str] = []
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line)
        if not m:
            bad.append(line)
            continue
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if name in pins:
            bad.append(line)
        pins[name] = m.group(2)
    return pins, bad


def main(constraints: str, freeze: str) -> int:
    want, bad_want = parse(constraints)
    got, bad_got = parse(freeze)
    extra = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    differ = sorted(n for n in set(want) & set(got) if want[n] != got[n])
    print(f"기준 {len(want)} · 설치 {len(got)} · 추가 {len(extra)} · 누락 {len(missing)} · 버전 차이 {len(differ)}")
    for n in extra:
        print(f"  추가: {n}=={got[n]}")
    for n in missing:
        print(f"  누락: {n}=={want[n]}")
    for n in differ:
        print(f"  버전 차이: {n} 기준 {want[n]} · 설치 {got[n]}")
    for line in bad_want:
        print(f"  기준 파일에서 읽을 수 없는 줄(또는 중복): {line}")
    for line in bad_got:
        print(f"  설치 목록에서 읽을 수 없는 줄(또는 중복): {line}")
    ok = bool(want) and not (extra or missing or differ or bad_want or bad_got)
    if not ok:
        print("의존성 판정 실패: 설치 결과가 constraints.txt 와 다르다")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
