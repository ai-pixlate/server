# GPU 서버 환경 (PyTorch 기본)

학교 GPU 서버(KubeSphere · V100 32GB)에서 쓸 PyTorch 기본 컨테이너입니다.
이미지에는 PyTorch·SSH·OCR 런타임 라이브러리만 있고, 서버 코드와 워커 venv는 `/data`에 둡니다(4장).

## 구성

| 파일 | 내용 |
| --- | --- |
| `Dockerfile` | CUDA 베이스 + Python venv(`/opt/venv`) + PyTorch·torchvision + SSH |
| `opt-venv.constraints.txt` | `/opt/venv` 패키지 버전 고정(2026-10-13 운영 pod 기준, 1장) |
| `entrypoint.sh` | 컨테이너 시작 시 SSH 공개키·호스트키·PaddleX 캐시 연결 준비 후 `sshd` 실행 (PID 1) |
| `sshd_config` | 키 인증만 허용, 비밀번호 로그인 차단 |
| `check_gpu.py` | PyTorch·GPU·`/data` 볼륨 상태 확인 |
| `worker.sh` · `env_check.py` · `lama_check.py` | gpu·ocr 큐 워커 관리와 점검(4장) |
| `k8s/pvc.yaml` | 데이터 볼륨 (가이드 예제1) |
| `k8s/workload.yaml` | GPU 컨테이너 (가이드 예제2) |
| `k8s/service.yaml` | SSH 포트 개방 (가이드 예제3) |

| 항목 | 값 |
| --- | --- |
| 이미지 | `yeram109/pixlate-gpu:torch2.14-cu126` (Docker Hub, public) |
| 베이스 이미지 | `nvidia/cuda:12.6.3-base-ubuntu24.04` (`Dockerfile`에서 digest로 고정) |
| PyTorch | `2.14.0+cu126` (V100 `sm_70` 지원 확인) |
| Python | 3.12 |

### 서버 환경 (2026-09 확인)

| 항목 | 값 |
| --- | --- |
| 웹 콘솔 | `http://210.125.70.71:30880` |
| 프로젝트(namespace) | `estsoft33` (워크스페이스 `estsoft-project`) |
| GPU | Tesla V100-SXM2-32GB |
| 드라이버 | 580.126.20 (CUDA 13.0까지 지원) |
| 할당량 | CPU 4코어, 메모리 30720Mi (프로젝트 내 컨테이너 합계), 스토리지 512GB |
| Storage Class | `sandbox-container-sc` |

## 꼭 알아둘 점

- **컨테이너가 재시작되면 `/data`(PVC) 외의 모든 변경이 사라집니다.** 코드·데이터·모델 가중치는 `/data` 아래에 둡니다.
- 캐시 경로는 `/data`로 잡혀 있어 재시작해도 다시 받지 않습니다.
  - `HF_HOME=/data/.cache/huggingface`, `TORCH_HOME=/data/.cache/torch`, `PIP_CACHE_DIR=/data/.cache/pip`
- 컨테이너 안에서 `pip install` 한 패키지도 재시작 시 사라집니다. 계속 쓸 패키지는 `Dockerfile`에 추가해 이미지를 다시 빌드합니다.
- SSH 포트는 공인 IP(`210.125.70.71`)로 외부에 열립니다. 비밀번호 로그인은 막혀 있고 공개키로만 접속됩니다.
- 서버 계정 ID·비밀번호, 개인키, 접속 포트는 레포에 커밋하지 않습니다. 팀 공유 문서에 기록합니다.

### 가이드 PDF와 다른 점

| 가이드 | 실제 |
| --- | --- |
| 프로젝트 `virl` | 계정별 프로젝트 (`estsoft33`) |
| CPU·Memory 제한 없음 | 할당량이 있어 **requests·limits를 모두 적어야** 생성됨 (`must specify requests.memory, limits.cpu ...` 오류) |
| Storage Class `sandbox-container` | `sandbox-container-sc` |
| Start Command에 `sleep infinity` | 넣지 않음 (이미지가 `sshd`를 PID 1로 실행) |
| 컨테이너 안에서 `apt install openssh-server` | 불필요 (이미지에 포함) |
| 폼에서 GPU Limit 입력 | 폼에 칸이 없어 Edit YAML로 `nvidia.com/gpu: 1` 입력 |

## 1. 이미지 빌드·푸시 (로컬 PC)

`Dockerfile`을 바꿨을 때만 필요합니다. 이미 올라간 이미지를 쓸 거면 2번으로 넘어갑니다.

주요 의존성을 고정했습니다. 베이스 이미지는 digest로, `/opt/venv`는 `opt-venv.constraints.txt`로 고정합니다.
`/data/venv`(gpu 워커)가 `.pth`로 `/opt/venv`의 torch를 공유하고, 두 워커 venv 모두 이미지의 `/usr/bin/python3.12`를 쓰기 때문입니다.
apt 패키지는 고정하지 않으므로(우분투 저장소가 이전 버전을 지웁니다) 같은 이미지가 다시 나온다는 보장은 아닙니다.
그래서 빌드한 뒤 푸시 전에 로컬 점검을 돌립니다(GPU 없이 가능, Docker 와 Git Bash 필요).

```bash
bash gpu/tests/image_check.sh <빌드한 이미지>   # 마지막 줄 RESULT ... fail=0 이어야 한다
```

| 구분 | 확인 |
| --- | --- |
| A. 이미지 | OCR 런타임 라이브러리(`ldconfig -p`), `/usr/bin/python3.12` = 3.12.3, `/opt/venv` freeze = 고정 파일, torch CUDA 빌드, sshd 설정 |
| B. entrypoint | PaddleX 캐시 링크 — 빈 `/data`·기존 캐시·캐시 자리가 파일·`/root/.paddlex` 가 이미 있음·다른 링크·마운트 없음, 재시작 반복. 모든 경우 sshd 가 PID 1 |
| C. 워커 Python | `gpu/baseline/` 의 운영 freeze 그대로 두 venv 를 `/tmp` 에 재현해 `pip check`·import(레포 코드 포함) |

GPU·LaMa 가중치가 없으므로 CUDA·가중치·모델 로드는 이 점검에 없습니다. pod 교체 뒤 실제 워커 Python(`/data/venv`·`/data/venv-ocr`)으로 확인합니다.
`gpu/baseline/` 은 2026-10-13 운영 pod 의 `/data/venv`·`/data/venv-ocr` `pip freeze` 입니다. `/data` venv 를 다시 설치했으면 새 freeze 로 바꿉니다.

태그는 빌드마다 새로 붙이고 이미 쓰는 태그에 덮어쓰지 않습니다. 같은 태그를 다시 푸시하면 이전 이미지로 되돌릴 기준이 사라지고,
재시작 때 어떤 이미지를 받는지가 pull 정책·노드 캐시에 따라 달라집니다. 푸시한 뒤 digest를 기록하고 Workload에는 digest로 지정합니다.

```bash
cd gpu
docker login
docker build -t yeram109/pixlate-gpu:torch2.14-cu126-<YYYYMMDD> .
docker push yeram109/pixlate-gpu:torch2.14-cu126-<YYYYMMDD>
docker buildx imagetools inspect yeram109/pixlate-gpu:torch2.14-cu126-<YYYYMMDD>   # Digest 를 기록
```

이미지에는 비밀 값이 없으므로 Docker Hub 저장소를 public으로 둡니다. private이면 서버가 이미지를 받지 못합니다.

## 2. 서버 배포 (KubeSphere 콘솔)

접속: `http://210.125.70.71:30880` → 워크스페이스 `estsoft-project` → 프로젝트 `estsoft33`

1. **볼륨 생성** — `Storage > Persistent Volume Claims > Create`
   - `k8s/pvc.yaml` 참고. 이름 `pixlate-data`, 200Gi, ReadWriteOnce
2. **SSH 키 준비** (로컬 PC, 최초 1회)
   ```bash
   ssh-keygen -t ed25519 -C "pixlate-gpu" -f ~/.ssh/pixlate_gpu
   ```
   `~/.ssh/pixlate_gpu.pub` 내용(공개키)을 복사합니다. 개인키(`pixlate_gpu`)는 공유하지 않습니다.
3. **컨테이너 생성** — `Application Workloads > Workloads > Create`
   - 이름과 이미지를 입력한 뒤 **Edit YAML**을 켜고 `k8s/workload.yaml` 내용으로 교체합니다
   - `SSH_PUBLIC_KEY` 값을 본인 공개키로 바꿉니다
   - Status가 `Running`이 될 때까지 이미지(약 3.9GB)를 받느라 몇 분 걸립니다
4. **포트 개방** — `Application Workloads > Services > Create > Specify Workload`
   - `k8s/service.yaml` 참고. 워크로드 `pixlate-gpu`, TCP 22 → 22, Access Mode `NodePort`
   - 생성 후 서비스 상세의 External Access 포트 번호(`<NodePort>`)를 팀 공유 문서에 기록합니다

## 3. 접속·확인

```bash
ssh -i ~/.ssh/pixlate_gpu -p <NodePort> root@210.125.70.71
python /opt/pixlate/check_gpu.py
```

마지막 줄에 `결과: 정상`이 나오면 GPU와 볼륨이 정상입니다.
SSH가 안 되면 콘솔의 `Workloads > pixlate-gpu > Pods > Terminal`로 들어가 같은 스크립트를 실행합니다.

매번 옵션을 입력하지 않으려면 `~/.ssh/config`에 추가합니다. 이후 `ssh pixlate-gpu`로 접속하고, VS Code Remote-SSH에서도 같은 이름으로 연결할 수 있습니다.

```
Host pixlate-gpu
    HostName 210.125.70.71
    Port <NodePort>
    User root
    IdentityFile ~/.ssh/pixlate_gpu
    IdentitiesOnly yes
```

팀원 키를 추가하려면 컨테이너 안에서 공개키를 `/data/.ssh/authorized_keys`(재시작 후 유지)와 `/root/.ssh/authorized_keys`(즉시 반영) **두 곳에** 한 줄씩 넣습니다.
`entrypoint.sh`는 시작할 때만 `/data` 쪽을 복사하므로, `/data`에만 넣으면 재시작 전까지 반영되지 않습니다. 워커가 꺼지므로 키 추가만을 위해 Workload를 재시작하지 않습니다.

### 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| `FailedCreate` · `must specify requests.memory, limits.cpu ...` | Workload YAML에 CPU·메모리 requests·limits가 모두 있는지 |
| `failed quota: estsoft-quota` · exceeded | 프로젝트 안 다른 컨테이너와 limits 합계가 CPU 4코어·메모리 30720Mi를 넘는지 |
| `torch.cuda.is_available() = False` | Workload에 `nvidia.com/gpu` limit이 있는지 |
| SSH `Connection refused` (웹 콘솔 30880도 안 열림) | 서버 쪽 네트워크 문제. 잠시 후 재시도하거나 다른 네트워크에서 시도 |
| SSH `Permission denied (publickey)` | `ssh -v`의 `Offering public key` 지문이 등록한 공개키 지문(`ssh-keygen -lf <pub>`)과 같은지, `/root/.ssh/authorized_keys`에도 들어 있는지 |
| 재배포 후 `REMOTE HOST IDENTIFICATION HAS CHANGED` | `/data/.ssh-host-keys`가 지워졌는지 확인 후 `ssh-keygen -R "[210.125.70.71]:<NodePort>"` |

### CUDA 11.8 빌드 (참고)

현재 서버 드라이버는 CUDA 13.0까지 지원하므로 필요 없습니다.
드라이버가 CUDA 11.x만 지원하는 서버로 옮길 경우에만 아래처럼 빌드합니다.

```bash
docker build \
  --build-arg BASE_IMAGE=nvidia/cuda:11.8.0-base-ubuntu22.04 \
  --build-arg TORCH_VERSION=2.7.1 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu118 \
  --build-arg OPT_VENV_LOCK=0 \
  -t yeram109/pixlate-gpu:torch2.7-cu118 .
```

`OPT_VENV_LOCK=0`은 CUDA 12.6 기준 버전 고정을 끕니다. 이 빌드를 계속 쓰게 되면 그 환경의 `pip freeze`로 고정 파일을 따로 만듭니다.

## 4. 백엔드 연동 (gpu·ocr 큐 워커)

API는 인페인팅(`app.tasks.run_inpaint`)을 **`gpu` 큐**에, 분석(`app.tasks.run_analyze`, ①섹션 분해·②OCR·③병합)을 **`ocr` 큐**에 넣습니다.
이 서버의 워커 두 개가 **EC2 Redis에 접속해** 가져갑니다. 학교 서버는 인바운드가 막혀 있어, 워커가 EC2로 나가는 방향만 씁니다.
EC2에는 `cpu` 워커·API·복구 프로세스만 둡니다(`deploy/README.md`).

| 워커 | venv | 로그 | 명령(`gpu/worker.sh`가 실행) |
| --- | --- | --- | --- |
| gpu | `/data/venv` (+ `/opt/venv` torch를 `.pth`로 공유) | `/data/gpu-worker.log` | `celery -A app.celery_app:celery_app worker -Q gpu --pool=solo --concurrency=1 -n gpu@%h` |
| ocr | `/data/venv-ocr` (PaddleOCR CPU 전용, torch 없음) | `/data/ocr-worker.log` | `celery -A app.celery_app:celery_app worker -Q ocr --pool=solo --concurrency=1 -n ocr@%h` |

- 두 워커 모두 `--pool=solo --concurrency=1`입니다. prefork로 띄우지 않습니다(LaMa는 spawn 자식 프로세스를 쓰고, OCR은 프로세스 하나가 수 GiB를 씁니다).
- 두 venv를 섞지 않습니다. OCR venv에 `requirements-gpu.txt`·torch·iopaint를 넣지 않고, gpu venv에 PaddleOCR을 넣지 않습니다.
- 코드는 `/data/server` 하나를 같이 씁니다. 배포 SHA는 `/data/pixlate/release.sha`에 적고, 코드가 그 SHA와 다르면 워커가 시작되지 않습니다.

### 파일 배치

| 레포 파일 | 서버 위치 | 역할 |
| --- | --- | --- |
| `gpu/worker.sh` | `/data/pixlate/worker.sh` | 워커 시작·웜 종료·상태(`<gpu\|ocr> <start\|stop\|status>`) |
| `deploy/bin/redact_run.py` | `/data/pixlate/bin/redact_run.py` | 로그 정제 래퍼(연결 주소·비밀을 가린 줄만 로그에 쓴다, 시그널 전달) |
| `gpu/env_check.py` | `/data/pixlate/bin/env_check.py` | 브로커·결과 저장소 PING, DB revision, S3 접근·리전 확인(값 출력 없음) |
| `gpu/lama_check.py` | `/data/pixlate/bin/lama_check.py` | torch·CUDA·`big-lama.pt` MD5·LaMa 로드(추론 없음) |
| `deploy/bin/ocr_measure.py` | `/data/pixlate/bin/ocr_measure.py` | OCR 1섹션 메모리·시간 실측 |
| (서버에만) `/data/server.env` | root 600 | 브로커·결과 저장소·DB(+ S3 자격, 아래) |
| (서버에만) `/data/pixlate/worker-gemini.env` | root 600 | `GEMINI_API_KEY` 한 줄. **ocr 워커에만** 연결 |
| (서버에만) `/data/pixlate/release.sha` | | 배포 SHA 40자리 |
| (서버에만) `/data/pixlate/<gpu\|ocr>.enabled` | | 게이트 파일. 없으면 워커를 시작하지 않는다 |

### 설치 (최초 1회, 모두 `/data` 아래)

```bash
# 코드: 배포 SHA로 고정
git clone https://github.com/ai-pixlate/server.git /data/server && git -C /data/server checkout <배포 SHA>
git -C /data/server rev-parse HEAD > /data/pixlate/release.sha

# gpu 워커 venv(이미지의 PyTorch 연결)
python -m venv /data/venv
echo /opt/venv/lib/python3.12/site-packages > /data/venv/lib/python3.12/site-packages/opt-venv.pth
cd /data/server && /data/venv/bin/pip install -r requirements.txt -r pipeline/requirements.txt

# ocr 워커 venv(시스템 Python, /opt/venv 와 분리)
/usr/bin/python3.12 -m venv /data/venv-ocr
cd /data/server && /data/venv-ocr/bin/pip install -r requirements.txt -r pipeline/requirements-ocr.txt
/data/venv-ocr/bin/pip check

# PaddleX 모델 캐시를 /data 에(재시작 후 다시 받지 않게). entrypoint.sh 가 시작할 때마다 링크를 만든다(아래)
mkdir -p /data/.cache/paddlex && [ -e /root/.paddlex ] || ln -s /data/.cache/paddlex /root/.paddlex
```

- OCR 런타임 라이브러리(`libgomp1 libgl1 libglib2.0-0t64`)는 `Dockerfile`에, PaddleX 캐시 링크(`/root/.paddlex` → `/data/.cache/paddlex`)는
  `entrypoint.sh`에 넣었습니다. 링크는 이미 있는 디렉터리·파일·다른 링크를 바꾸지 않고 경고만 남깁니다(`kubectl logs`·콘솔 로그).
  이 변경이 들어간 이미지로 교체하기 전에는 pod가 재시작될 때마다 아래 두 가지를 손으로 해야 합니다(`worker.sh`는 라이브러리가 없으면 시작을 거부).
  `apt-get update && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0t64`, 위의 `ln -s` 한 줄.
- 같은 SHA라도 `pipeline/requirements.txt`가 버전을 고정하지 않으므로 설치 후 `pip freeze`를 남겨 둡니다.

### 환경변수 파일

`/data/server.env`(root 600). `«»` 자리는 팀 공유 문서의 값으로 채웁니다. 키 이름만 이 문서에 둡니다.

```
CELERY_BROKER_URL=redis://:«Redis비번»@«EC2주소»:6379/0
CELERY_RESULT_BACKEND=redis://:«Redis비번»@«EC2주소»:6379/1
DATABASE_URL=postgresql+psycopg://«DB사용자»:«RDS비번»@«RDS주소»:5432/«DB이름»
CELERY_VISIBILITY_TIMEOUT=3600
AWS_ACCESS_KEY_ID=«학교 워커 전용 IAM 사용자 액세스 키 ID»
AWS_SECRET_ACCESS_KEY=«같은 키의 비밀 키»
AWS_REGION=ap-northeast-2
```

- 비밀번호에 `@ : / # % ?`가 있으면 URL 인코딩합니다(예: `@` → `%40`, `#` → `%23`).
- «EC2주소»는 EC2 탄력적 IP입니다. docker 내부 이름(`pixlate-redis`)은 이 서버에서 해석되지 않습니다.
- `CELERY_VISIBILITY_TIMEOUT`은 EC2(API·워커)와 같은 값이어야 합니다.
- **S3 자격: 전용 IAM 사용자 액세스 키(2026-10-12 결정·적용).** 분석(원본 이미지 읽기)·인페인팅(결과 업로드) 모두 S3가 필요하고,
  학교 서버는 IAM 역할을 쓸 수 없어 아래 정책만 붙인 전용 IAM 사용자의 액세스 키를 위 `AWS_*` 세 줄로 둡니다.
  - 권한 범위는 [`iam/school-worker-s3-policy.json`](iam/school-worker-s3-policy.json)으로 한정합니다(`<BUCKET>`만 바꿔 씀).
    `source/`·`staging/`·`verified/` 읽기, `staging/`·`verified/` 쓰기, 점검용 접두사 목록·리전 조회. 삭제·다른 접두사는 없습니다.
  - 장기 키이므로 **발급 후 90일 안에 교체**합니다(새 키 발급 → 이 파일 교체 → `env_check.py` 확인 → 워커 재시작 → 옛 키 비활성·삭제).
    임시 자격(IAM Roles Anywhere 등)으로 바꾸는 것은 후속 검토 대상입니다.
  - 적용 후 `env_check.py`로 접근·리전·버킷 해시(EC2 값과 같음)와 최소 권한 거부(다른 접두사·전체 목록)를 확인했습니다.
    2026-10-13 운영 통합 검증에서 ocr(원본 읽기)·gpu(인페인팅 결과 업로드) 모두 이 자격으로 동작했습니다.
- Gemini 키는 이 파일에 넣지 않습니다(gpu 워커에는 필요 없음). `/data/pixlate/worker-gemini.env`에만 둡니다.

### 점검·시작·정지

```bash
cd /data/server
(set -a; . /data/server.env; set +a; /data/venv/bin/python /data/pixlate/bin/env_check.py)        # gpu 환경
(set -a; . /data/server.env; set +a; /data/venv-ocr/bin/python /data/pixlate/bin/env_check.py)    # ocr 환경
/data/venv/bin/python /data/pixlate/bin/lama_check.py

touch /data/pixlate/gpu.enabled /data/pixlate/ocr.enabled    # 소비 재개를 결정한 뒤에만
bash /data/pixlate/worker.sh gpu start
bash /data/pixlate/worker.sh ocr start
bash /data/pixlate/worker.sh ocr status
bash /data/pixlate/worker.sh ocr stop       # TERM → 웜 종료(처리 중 작업은 끝까지). kill -9 를 쓰지 않는다
```

- **pod가 재시작되면 워커는 꺼진 채로 남습니다**(PID 1은 sshd). 검증 전에 자동으로 소비하지 않게 하려는 의도입니다. 재시작 뒤에는 위 시작 명령을 다시 실행합니다.
- 관리 밖에서 띄운 옛 워커(예: `nohup celery ... -Q gpu`)가 있으면 `worker.sh`가 시작을 거부합니다. 옛 워커는 TERM으로 웜 종료합니다.
- 워커가 처리 도중 죽어도 작업은 큐로 돌아와 다시 실행됩니다(`task_acks_late`). 워커 전체가 죽으면 `CELERY_VISIBILITY_TIMEOUT`(기본 3600초) 뒤 재전달되고, 실행 권한 만료는 EC2 복구 프로세스가 DB 기준으로 정리합니다.
- EC2 Redis가 재시작되면 워커는 스스로 다시 붙습니다.

### OCR 자원 (2026-10-12 실측, e1291ab)

pod 한도 CPU 4코어·메모리 30720Mi(**프로젝트 안 모든 컨테이너 합계**, 스왑 없음). OCR CPU 실행, 새 프로세스, 모델 캐시 있음. 합성 입력.

| 섹션(폭×높이) | 결과 | 시간 | 최대 RSS |
| --- | --- | --- | --- |
| 600×560 | 종료 0, 영역 5 | 21.4초(캐시 없음 44.3초) | 2.34GiB |
| 860×1,000 | 종료 0 | 35.4초 | 4.84GiB |
| 860×2,000 | 종료 0 | 64.2초 | 9.03GiB |
| 860×4,000 | 종료 0 | 117.3초 | 17.70GiB |
| 1080×4,000 | 측정 감시(20GiB)로 중단 | — | 20GiB 초과 |

- 메모리는 글자 양이 아니라 **면적**에 비례합니다(빈 배경 860×4,000도 17.83GiB). 약 100만 픽셀당 5GiB입니다.
- **운영 범위: 폭 860px 이하·높이 4,000px 이하가 실측 통과 범위입니다.** 폭 1080 이상의 긴 섹션은 약 22GiB 이상으로 추정되며,
  같은 pod의 LaMa와 동시에 돌면 메모리 한도를 넘을 수 있습니다. 4,000px 타일 처리(#31·#32)가 들어오면 다시 측정합니다.
- 실제 상품 이미지로는 아직 측정하지 않았습니다. 빈 배경 860×4,000에서 글자 1줄이 검출되지 않은(영역 0) 점은 AI 담당 확인 대상입니다.
- 측정: `cd /data/server && /data/venv-ocr/bin/python /data/pixlate/bin/ocr_measure.py <split.json> <section> <out> [한도GiB]`

### 연동 확인 결과 (2026-09-28, 이전 구성 기록)

| 확인 (EC2에서 실행) | 결과 |
| --- | --- |
| `docker exec pixlate-worker-ocr celery -A app.celery_app inspect ping` | `ocr@…`, `cpu@…`, `gpu@pixlate-gpu-…` — 3 nodes online |
| `redis-cli -n 0 LLEN gpu` | 대기 1건 → 0 |
| 테스트(`pytest tests --ignore-glob='tests/test_pipeline_*'`) 후 `job_async_task`(inpaint) | 새 행 `done`, 소요 약 2초(스텁 `sleep(2)`) |
| EC2 재부팅 후 `inspect ping` | `gpu@…` 자동 재접속 |

EC2에는 gpu 워커가 없으므로 gpu 큐를 비운 것은 학교 GPU 워커입니다. 당시 `run_inpaint`는 스텁(2초 대기 후 결과 경로만 기록)이라, 이 결과는 **EC2 Redis ↔ GPU ↔ RDS 경로가 열렸다**는 뜻이었습니다.
현재 `run_inpaint`는 실제 LaMa 추론입니다. 2026-10-13 운영 통합 검증에서 학교 V100의 실제 인페인팅, S3 결과 업로드, EC2 채택과 산출물 검증까지 확인했습니다([`../deploy/integration-verification-2026-10-13.md`](../deploy/integration-verification-2026-10-13.md)).
