# GPU 워커 연결 테스트 (Celery + Redis)

학교 GPU 서버에서 Celery 워커가 Redis의 `gpu` 큐 작업을 꺼내 처리하는 구조가 동작하는지 확인한 테스트입니다. (2026-09-18)

| 파일 | 내용 |
| --- | --- |
| `worker_test.py` | 테스트용 Celery 앱과 가짜 인페인팅 작업 |
| `send.py` | 작업을 보내고 걸린 시간 측정 (API 서버 역할) |
| `kill_test.sh` | 처리 중 워커 강제 종료 테스트 (학교 서버 안에서 실행) |

## 1. 목적과 계획

### 목적

BE는 인페인팅 작업(`run_inpaint`)을 Redis의 `gpu` 큐로 보내도록 구현해 두었습니다([`app/celery_app.py`](../../app/celery_app.py)).
학교 GPU 서버는 학교 IP에서만 접속할 수 있어 AWS → 학교 서버 요청은 불가능하므로, **학교 서버의 워커가 AWS의 Redis로 접속해 작업을 가져가는 방식**이어야 합니다.
이 방식이 실제로 동작하는지, 운영 전에 보완할 점이 있는지 확인합니다.

### 방법

- 큐 구성과 라우팅은 `app/celery_app.py`와 같게 맞춥니다.
- 실제 인페인팅 대신 **지정한 시간 동안 GPU 행렬곱만 하는 가짜 작업**(`fake_inpaint`)을 씁니다. 모델과 무관하게 작업 전달 흐름만 확인하기 위해서입니다.
- DB·S3는 쓰지 않습니다.

### 확인 항목

1. 학교 서버의 워커가 `gpu` 큐에서 작업을 꺼내 GPU로 처리하는지
2. 작업을 넘기고 결과를 받는 데 걸리는 시간
3. 처리 중에 워커가 강제 종료되면 작업이 어떻게 되는지 (학교 서버 점검·컨테이너 재시작 상황)
4. 워커가 꺼져 있을 때 보낸 작업이 보존되는지

### 구성

```
[로컬 PC = API 역할]                     [학교 GPU 서버 컨테이너]
 send.py ── SSH 포트 포워딩(-L) ──▶ Redis ◀── Celery 워커 (-Q gpu, V100)
```

EC2를 건드리지 않도록 Redis를 학교 서버 안에 띄웠습니다. 따라서 **워커 ↔ Redis 사이의 인터넷 구간은 이 테스트에 포함되지 않습니다.** 학교 서버에서 외부 6379·5432·443 포트로 나가는 연결과 S3 접근이 되는 것은 별도로 확인했습니다.

## 2. 진행 중 발견한 문제와 처리

| 문제 | 원인 | 처리 |
| --- | --- | --- |
| 처음 계획한 구성(Redis를 로컬 PC에 두고 학교 서버가 SSH 역방향 터널로 접속)을 쓸 수 없음 | 외부에서 로컬 PC로 들어오는 통로가 생겨 보안 정책상 차단됨 | Redis를 학교 서버에 두고 **로컬 PC가 학교 서버로 접속**(`ssh -L`)하는 방향으로 변경 |
| `/data`에 만든 가상환경에서 PyTorch를 찾지 못함 | `python -m venv --system-site-packages`는 이미지의 PyTorch(`/opt/venv`)가 아니라 우분투 기본 Python을 가리킴 | `.pth` 파일로 `/opt/venv`의 패키지를 연결 (아래 "다시 실행하는 방법" 참고) |
| 워커가 켜지지 않음 | `pkill -f "celery -A worker_test"`가 같은 문자열을 포함한 실행 명령 자체까지 종료함 | 패턴을 `"[w]orker_test worker"`처럼 자기 자신과 겹치지 않게 수정. 그사이 보낸 작업이 큐에 남아 있어 확인 항목 4를 먼저 확인함 |
| 워커 로그에 `.> gpu exchange=cpu(direct) key=cpu`로 표시됨 | 작업이 `ocr`·`cpu`·`gpu` 세 큐에 모두 복사되는 설정일 수 있다고 의심 | 세 큐를 모두 선언한 상태에서 `gpu` 작업을 보내 확인 → `gpu` 큐에만 1건 들어감. **복사되지 않음** |
| 재처리 설정(`acks_late`) 테스트가 실패처럼 보임 | 재전달까지 약 90초가 걸리는데 스크립트가 그 전에 대기를 끝냄 | 대기 시간을 180초로 늘려 재실행 → 성공 |

## 3. 결과

### 예상과 실제 비교

| 확인 항목 | 예상 | 실제 | 판정 |
| --- | --- | --- | --- |
| 1. 작업 처리 | 워커가 꺼내 GPU로 처리 | Tesla V100으로 처리하고 결과 반환 | ✅ 일치 |
| 2. 전달 시간 | 수십 ms | **약 25ms** (첫 작업만 0.5초) | ✅ 일치 |
| 3. 강제 종료 · 기본 설정 | 작업이 사라지거나 "처리 중"으로 멈춤 | **작업이 사라지고 상태가 `STARTED`로 영구히 남음** | ✅ 일치 (예상한 문제가 실제로 있음) |
| 3. 강제 종료 · `acks_late` | `visibility_timeout`(20초) 뒤 재처리 | 재처리되어 완료되지만 **약 90초** 걸림 | ⚠️ 시간이 예상보다 김 |
| 4. 워커가 꺼져 있을 때 보낸 작업 | 큐에 보존 | 큐에 남아 있다가 워커가 켜지자 처리 | ✅ 일치 |
| (추가) 다른 큐로 복사 여부 | 복사될 수 있음 | 복사되지 않음 | ✅ 문제없음 |

**요약:** 구조와 속도는 문제없습니다. 다만 **워커가 도중에 꺼지면 작업이 사라지는 문제**가 있어 운영 전에 BE 설정 보완이 필요합니다.

### 상세: 전달 시간

3초짜리 작업 5건을 보냈을 때, 전체 걸린 시간에서 GPU 처리 시간을 뺀 값입니다. 두 서버의 시계 차이와 무관합니다.

| 작업 | 전체 | GPU 처리 | 전달 (큐 + 결과) |
| --- | --- | --- | --- |
| 1번째 | 3.50s | 3.00s | 496ms (워커가 처음 PyTorch를 불러오는 시간 포함) |
| 2~5번째 | 3.03~3.04s | 3.00s | 22~28ms |

Celery 워커는 Redis에 연결을 열어 두고 작업이 들어오면 바로 받기 때문에 전달 지연은 무시할 수준입니다. 실제 서비스에서는 모델 처리 시간과 S3 이미지 전송 시간이 대부분을 차지할 것입니다.

### 상세: 처리 중 워커 강제 종료

15초짜리 작업을 5초 처리하다가 워커를 `kill -9`로 종료하고 다시 켰습니다.

| 설정 | 종료 직후 | 워커 재시작 후 |
| --- | --- | --- |
| **기본** (`app/celery_app.py`와 동일) | 큐 0건, 미완료 0건 | 작업이 사라짐. 상태 `STARTED`로 남음 |
| `task_acks_late=True` + `visibility_timeout=20` | 미완료 1건 | 약 90초 뒤 다시 전달되어 `SUCCESS` |

- 기본 설정은 워커가 작업을 **받는 순간** 완료 처리하므로, 처리 도중 워커가 죽으면 복구되지 않습니다. 실제 서비스라면 해당 섹션이 계속 "인페인팅 중"으로 남습니다.
- `visibility_timeout`의 기본값은 3600초(1시간)입니다.

## BE에 전달할 내용

1. **워커가 도중에 죽으면 작업이 사라집니다.** 학교 서버는 점검·재시작을 통제할 수 없으므로 아래 중 하나 이상이 필요합니다.
   - `task_acks_late=True`, `task_reject_on_worker_lost=True`, 적절한 `visibility_timeout`으로 작업 재처리
   - 일정 시간 이상 `running`에 머문 작업을 실패 처리하는 시간 제한 (`task_time_limit` 또는 DB 기준 점검)
2. **재처리를 켜면 같은 작업이 두 번 실행될 수 있습니다.** 지금 `run_inpaint`는 실행할 때마다 `job_async_task` 행을 새로 INSERT하므로, 두 번 실행돼도 문제없게(멱등) 바꿔야 합니다.
3. 워커가 살아 있는지, `gpu` 큐에 작업이 쌓이는지 확인할 수단(Flower 등)이 있으면 좋습니다.

## 다시 실행하는 방법

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
