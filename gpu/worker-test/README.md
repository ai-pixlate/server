# GPU 워커 연결 테스트 (Celery + Redis)

학교 GPU 서버에서 Celery 워커가 Redis의 `gpu` 큐 작업을 꺼내 처리하는 구조가 동작하는지 확인한 테스트입니다. (2026-09-18)

| 파일 | 내용 |
| --- | --- |
| `worker_test.py` | 테스트용 Celery 앱과 가짜 인페인팅 작업 |
| `send.py` | 작업을 보내고 걸린 시간 측정 (API 서버 역할) |
| `kill_test.sh` | 처리 중 워커 강제 종료 테스트 (학교 서버 안에서 실행) |

## 1. 배경 — 왜 대기열 방식인가

학교 GPU 서버는 **학교 IP에서만 접속을 허용**합니다. AWS에서 학교 서버로 요청을 보낼 수 없고, 학교 서버에서 AWS로 나가는 연결만 가능합니다.

| 방향 | 가능 여부 | 확인 방법 |
| --- | --- | --- |
| AWS → 학교 서버 | 불가 | 학교 IP 외 네트워크에서 접속 시 `Connection refused` |
| 학교 서버 → AWS | 가능 | 컨테이너에서 외부 443·5432·6379 포트 연결과 S3 접근 확인 |

따라서 **AWS가 GPU 서버에 일을 시키는 방식은 쓸 수 없고, GPU 서버가 일을 가지러 가는 방식**이어야 합니다. BE는 이미 Celery + Redis로 `run_inpaint`를 `gpu` 큐에 보내도록 구현해 두었고([`app/celery_app.py`](../../app/celery_app.py)), 이 방식은 워커가 Redis에 접속해 작업을 꺼내가므로 위 제약에 걸리지 않습니다.

웹소켓처럼 연결을 직접 여는 방식도 방향은 같지만, 작업 보존·재처리·여러 워커 분배를 직접 만들어야 합니다. 대기열 도구는 이를 기본으로 제공합니다.

## 2. 목적과 확인 항목

BE가 만든 대기열 구조에 학교 GPU 서버를 붙일 수 있는지, 운영 전에 보완할 점이 무엇인지 확인합니다.

| ID | 확인 항목 |
| --- | --- |
| T1 | 학교 서버의 워커가 `gpu` 큐에서 작업을 꺼내 GPU로 처리하는지 |
| T2 | 작업을 넘기고 결과를 받는 데 걸리는 시간 |
| T3 | 처리 중 워커가 강제 종료되면 작업이 어떻게 되는지 (점검·컨테이너 재시작 상황) |
| T4 | 워커가 꺼져 있을 때 보낸 작업이 보존되는지 |

실제 인페인팅 대신 **지정한 시간 동안 GPU 행렬곱만 하는 가짜 작업**(`fake_inpaint`)을 씁니다. 모델과 무관하게 작업 전달 흐름만 확인하기 위해서입니다. 큐 구성과 라우팅은 `app/celery_app.py`와 같게 맞추고, DB·S3는 쓰지 않습니다.

## 3. 테스트 구성

```
[로컬 PC = API 역할]                     [학교 GPU 서버 컨테이너]
 send.py ── SSH 포트 포워딩(-L) ──▶ Redis ◀── Celery 워커 (-Q gpu, V100)
```

BE의 EC2 설정을 건드리지 않기 위해 Redis를 학교 서버 안에 띄웠습니다.

### 3.1 실행 환경

| 항목 | 값 |
| --- | --- |
| 컨테이너 이미지 | `yeram109/pixlate-gpu:torch2.14-cu126` |
| GPU | Tesla V100-SXM2-32GB (드라이버 580.126.20) |
| 컨테이너 자원 | CPU 1~4코어, 메모리 8~30GB, GPU 1 |
| Python · Celery | 3.12 · 5.6.3 |
| Redis | Ubuntu 24.04 apt 기본 `redis-server` (컨테이너 안, 영속성 끔) |
| 워커 옵션 | `-Q gpu --concurrency=1` (prefork) |
| 가짜 작업 | 4096×4096 행렬곱 + `tanh` 반복, 지정 시간까지 (3초에 약 259회) |

### 3.2 실제였던 것 / 대역이었던 것

| 항목 | 이 테스트 | 실제 운영 |
| --- | --- | --- |
| 워커가 실행된 곳 | **학교 GPU 서버 컨테이너** (실제) | 같음 |
| GPU 연산 | **Tesla V100** (실제) | 같음 |
| Celery·큐 설정 | BE 코드와 동일 | 같음 |
| Redis 위치 | 학교 서버 안 (대역) | AWS EC2 |
| 작업을 보낸 쪽 | 로컬 PC (대역) | AWS의 API 서버 |
| 처리 내용 | 가짜 작업 (대역) | 실제 인페인팅 모델 |
| DB · S3 | 사용 안 함 | 사용 |

## 4. 테스트 케이스

| ID | 절차 | 판정 기준 | 결과 |
| --- | --- | --- | --- |
| T1 | 워커를 켜고 `send.py`로 3초짜리 작업 1건 전송 | 워커 로그에 `received` → `succeeded`, 결과에 V100 이름 포함 | 통과 |
| T2 | 3초짜리 작업 5건을 순차 전송, 전체 시간에서 GPU 처리 시간을 뺌 | 전달 시간이 모델 처리 시간에 비해 무시할 수준 | 통과 (약 25ms) |
| T3 | 15초짜리 작업을 5초 처리하던 중 워커를 `kill -9` → 재시작 → 최대 180초 관찰 (`kill_test.sh`) | 작업이 완료되거나 실패로 정리됨 | **기본 설정 실패** / `acks_late` 통과 |
| T4 | 워커가 없는 상태에서 작업 1건 전송 → 큐 길이 확인 → 워커 기동 | 작업이 큐에 남아 있다가 처리됨 | 통과 |

T2의 전달 시간은 **전체 걸린 시간 − 워커가 보고한 GPU 처리 시간**으로 계산합니다. 두 서버의 시계 차이와 무관합니다.

## 5. 결과

### 5.1 예상과 실제 비교

| 확인 항목 | 예상 | 실제 | 판정 |
| --- | --- | --- | --- |
| T1 작업 처리 | 워커가 꺼내 GPU로 처리 | V100으로 처리하고 결과 반환 | 일치 |
| T2 전달 시간 | 수십 ms | 약 25ms (첫 작업만 0.5초) | 일치 |
| T3 기본 설정 | 작업이 사라지거나 "처리 중"으로 멈춤 | **작업이 사라지고 상태가 `STARTED`로 영구히 남음** | 일치 (예상한 문제가 실재) |
| T3 `acks_late` | `visibility_timeout`(20초) 뒤 재처리 | 재처리되어 완료되지만 **약 90초** 걸림 | 시간이 예상보다 김 |
| T4 큐 보존 | 큐에 보존 | 워커 기동 후 즉시 처리 | 일치 |
| (추가) 다른 큐로 복사 여부 | 복사될 수 있음 | 복사되지 않음 | 문제없음 |

**요약:** 구조와 속도는 문제없습니다. 다만 **워커가 도중에 꺼지면 작업이 사라지는 문제**가 있어 운영 전에 BE 설정 보완이 필요합니다.

### 5.2 T2 — 전달 시간

```
처리: pixlate-gpu-c8b5dd9d4-ltlcw · Tesla V100-SXM2-32GB · GPU 3.00s · 전체 3.50s · 오버헤드 496ms
처리: pixlate-gpu-c8b5dd9d4-ltlcw · Tesla V100-SXM2-32GB · GPU 3.00s · 전체 3.03s · 오버헤드  25ms
처리: pixlate-gpu-c8b5dd9d4-ltlcw · Tesla V100-SXM2-32GB · GPU 3.00s · 전체 3.03s · 오버헤드  22ms
처리: pixlate-gpu-c8b5dd9d4-ltlcw · Tesla V100-SXM2-32GB · GPU 3.01s · 전체 3.03s · 오버헤드  25ms
처리: pixlate-gpu-c8b5dd9d4-ltlcw · Tesla V100-SXM2-32GB · GPU 3.01s · 전체 3.04s · 오버헤드  28ms

오버헤드 평균 119ms · 최소 22ms · 최대 496ms
```

첫 작업의 496ms는 워커 프로세스가 PyTorch를 처음 불러오고 CUDA를 초기화하는 시간입니다. 이후에는 22~28ms입니다. Celery 워커는 Redis에 연결을 열어 두고 작업이 들어오면 바로 받기 때문에 전달 지연은 무시할 수준입니다. 실제 서비스에서는 모델 처리 시간과 S3 이미지 전송 시간이 대부분을 차지할 것입니다.

### 5.3 T3 — 처리 중 워커 강제 종료

| 설정 | 종료 직후 | 워커 재시작 후 |
| --- | --- | --- |
| **기본** (`app/celery_app.py`와 동일) | 큐 0건, 미완료 0건 | 작업이 사라짐. 상태 `STARTED`로 남음 |
| `task_acks_late=True` + `visibility_timeout=20` | 큐 0건, **미완료 1건** | 약 90초 뒤 다시 전달되어 `SUCCESS` |

`acks_late` 모드의 워커 로그 (같은 작업 ID가 두 번 전달됨):

```
[08:59:02] Task gpu_test.fake_inpaint[f61635da-...] received     ← 최초 실행 (5초 뒤 kill)
[09:00:38] Task gpu_test.fake_inpaint[f61635da-...] received     ← 재시작 후 재전달
[09:00:56] Task gpu_test.fake_inpaint[f61635da-...] succeeded
```

- 기본 설정은 워커가 작업을 **받는 순간** 완료 처리하므로, 처리 도중 워커가 죽으면 복구되지 않습니다. 실제 서비스라면 해당 섹션이 계속 "인페인팅 중"으로 남습니다.
- `visibility_timeout`을 20초로 설정했는데도 재전달까지 약 90초가 걸렸습니다. **원인은 확인하지 않았습니다.** 미완료 작업을 큐로 되돌리는 점검 주기가 따로 있는 것으로 보입니다. 확실한 것은 **재처리를 켜도 즉시 복구되지는 않는다**는 점이며, 사용자 대기 시간이나 시간 제한 값을 정할 때 고려해야 합니다.
- `visibility_timeout`의 기본값은 3600초(1시간)입니다.

## 6. 한계

- **학교 서버 ↔ AWS Redis 구간은 검증되지 않았습니다.** 측정한 25ms에는 이 구간이 빠져 있습니다. 실제로는 인터넷 왕복 시간이 더해집니다.
- 연결이 끊겼다 다시 붙을 때의 워커 동작, DB·S3 접속은 확인하지 않았습니다.
- T2는 5건 측정이며 서로 다른 시간대·네트워크 상태에서 반복하지 않았습니다.
- 가짜 작업이라 **실제 모델의 GPU 메모리 사용량과 이미지 전송 시간은 포함되지 않습니다.**
- 동시 작업 처리(워커 여러 대, 한 워커에 여러 작업)는 확인하지 않았습니다.

## 7. 진행 중 발견한 문제와 처리

| 문제 | 원인 | 처리 |
| --- | --- | --- |
| 처음 계획한 구성(Redis를 로컬 PC에 두고 학교 서버가 SSH 역방향 터널로 접속)을 쓸 수 없음 | 외부에서 로컬 PC로 들어오는 통로가 생겨 보안 정책상 차단됨 | Redis를 학교 서버에 두고 **로컬 PC가 학교 서버로 접속**(`ssh -L`)하는 방향으로 변경 |
| `/data`에 만든 가상환경에서 PyTorch를 찾지 못함 | `python -m venv --system-site-packages`는 이미지의 PyTorch(`/opt/venv`)가 아니라 우분투 기본 Python을 가리킴 | `.pth` 파일로 `/opt/venv`의 패키지를 연결 (아래 "다시 실행하는 방법" 참고) |
| 워커가 켜지지 않음 | `pkill -f "celery -A worker_test"`가 같은 문자열을 포함한 실행 명령 자체까지 종료함 | 패턴을 `"[w]orker_test worker"`처럼 자기 자신과 겹치지 않게 수정. 그사이 보낸 작업이 큐에 남아 있어 T4를 먼저 확인함 |
| 워커 로그에 `.> gpu exchange=cpu(direct) key=cpu`로 표시됨 | 작업이 `ocr`·`cpu`·`gpu` 세 큐에 모두 복사되는 설정일 수 있다고 의심 | 세 큐를 모두 선언한 상태에서 `gpu` 작업을 보내 확인 → `gpu` 큐에만 1건 들어감. **복사되지 않음** |
| 재처리 설정(`acks_late`) 테스트가 실패처럼 보임 | 재전달까지 약 90초가 걸리는데 스크립트가 그 전에 대기를 끝냄 | 대기 시간을 180초로 늘려 재실행 → 성공 |

## 8. BE에 전달할 내용

1. **워커가 도중에 죽으면 작업이 사라집니다.** 학교 서버는 점검·재시작을 통제할 수 없으므로 아래 중 하나 이상이 필요합니다.
   - `task_acks_late=True`, `task_reject_on_worker_lost=True`, 적절한 `visibility_timeout`으로 작업 재처리
   - 일정 시간 이상 `running`에 머문 작업을 실패 처리하는 시간 제한 (`task_time_limit` 또는 DB 기준 점검)
2. **재처리를 켜면 같은 작업이 두 번 실행될 수 있습니다.** 지금 `run_inpaint`는 실행할 때마다 `job_async_task` 행을 새로 INSERT하므로, 두 번 실행돼도 문제없게(멱등) 바꿔야 합니다.
3. 워커가 살아 있는지, `gpu` 큐에 작업이 쌓이는지 확인할 수단(Flower 등)이 있으면 좋습니다.

## 9. 다시 실행하는 방법

학교 서버에서 (최초 1회)

```bash
python -m venv /data/venv
echo /opt/venv/lib/python3.12/site-packages > /data/venv/lib/python3.12/site-packages/opt-venv.pth
/data/venv/bin/pip install "celery[redis]"
apt-get update && apt-get install -y redis-server   # 컨테이너 재시작 시 사라짐
```

`worker_test.py`, `kill_test.sh`를 `/data/worker-test/`에 복사한 뒤

```bash
# 학교 서버: Redis + 워커 실행
redis-server --daemonize yes --bind 127.0.0.1 --save ""
cd /data/worker-test && /data/venv/bin/celery -A worker_test worker -Q gpu -n gpu@%h --concurrency=1

# 로컬 PC: Redis 포트 포워딩 후 작업 전송
ssh -N -L 16379:127.0.0.1:6379 pixlate-gpu
CELERY_BROKER_URL=redis://127.0.0.1:16379/0 CELERY_RESULT_BACKEND=redis://127.0.0.1:16379/1 python send.py 5 3

# 학교 서버: 강제 종료 테스트
bash /data/worker-test/kill_test.sh default
bash /data/worker-test/kill_test.sh acks_late
```
