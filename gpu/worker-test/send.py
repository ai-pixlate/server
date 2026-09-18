"""gpu 큐에 가짜 인페인팅 작업을 넣고 결과가 돌아오기까지 걸린 시간을 잰다. (API 서버 역할)

사용법
  python send.py [작업 수] [작업당 처리 초] [결과 대기 제한 초]
  python send.py 5 3          # 3초짜리 작업 5건
  python send.py 1 20 0       # 20초짜리 작업 1건을 넣기만 하고 기다리지 않음

오버헤드 = 전체 걸린 시간 - 워커의 GPU 처리 시간
         (큐 전달, 워커가 작업을 꺼내는 시간, 결과 전달을 합친 값. 두 서버의 시계 차이와 무관하다)
"""

import sys
import time

from worker_test import fake_inpaint


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0

    overheads = []
    for i in range(1, count + 1):
        sent = time.time()
        result = fake_inpaint.delay(i, seconds)
        print(f"[{i}] 작업 전송: {result.id}")
        if wait == 0:
            continue

        data = result.get(timeout=wait)
        total = time.time() - sent
        overhead = total - data["processSeconds"]
        overheads.append(overhead)
        print(f"    처리: {data['host']} · {data['gpu']} · GPU {data['processSeconds']:.2f}s "
              f"· 전체 {total:.2f}s · 오버헤드 {overhead * 1000:.0f}ms")

    if overheads:
        avg = sum(overheads) / len(overheads)
        print(f"\n오버헤드 평균 {avg * 1000:.0f}ms · 최소 {min(overheads) * 1000:.0f}ms · 최대 {max(overheads) * 1000:.0f}ms")


if __name__ == "__main__":
    main()
