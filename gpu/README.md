# GPU 서버 환경 (PyTorch 기본)

학교 GPU 서버(KubeSphere · V100 32GB)에서 쓸 PyTorch 기본 컨테이너입니다.
OCR·인페인팅 모델 의존성은 아직 넣지 않았고, GPU 인식과 SSH 접속까지만 갖춘 상태입니다.

## 구성

| 파일 | 내용 |
| --- | --- |
| `Dockerfile` | CUDA 베이스 + Python venv(`/opt/venv`) + PyTorch·torchvision + SSH |
| `entrypoint.sh` | 컨테이너 시작 시 SSH 공개키·호스트키 준비 후 `sshd` 실행 (PID 1) |
| `sshd_config` | 키 인증만 허용, 비밀번호 로그인 차단 |
| `check_gpu.py` | PyTorch·GPU·`/data` 볼륨 상태 확인 |
| `k8s/pvc.yaml` | 데이터 볼륨 (가이드 예제1) |
| `k8s/workload.yaml` | GPU 컨테이너 (가이드 예제2) |
| `k8s/service.yaml` | SSH 포트 개방 (가이드 예제3) |

| 항목 | 값 |
| --- | --- |
| 이미지 | `yeram109/pixlate-gpu:torch2.14-cu126` (Docker Hub, public) |
| 베이스 이미지 | `nvidia/cuda:12.6.3-base-ubuntu24.04` |
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

```bash
cd gpu
docker login
docker build -t yeram109/pixlate-gpu:torch2.14-cu126 .
docker push yeram109/pixlate-gpu:torch2.14-cu126
```

이미지에는 비밀 값이 없으므로 Docker Hub 저장소를 public으로 둡니다. private이면 서버가 이미지를 받지 못합니다.

같은 태그로 다시 푸시한 경우 `imagePullPolicy: Always`라서 Workload를 재시작하면 새 이미지를 받습니다.

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

팀원 키를 추가하려면 컨테이너 안에서 `/data/.ssh/authorized_keys`에 공개키를 한 줄씩 넣고 Workload를 재시작합니다.

### 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| `FailedCreate` · `must specify requests.memory, limits.cpu ...` | Workload YAML에 CPU·메모리 requests·limits가 모두 있는지 |
| `failed quota: estsoft-quota` · exceeded | 프로젝트 안 다른 컨테이너와 limits 합계가 CPU 4코어·메모리 30720Mi를 넘는지 |
| `torch.cuda.is_available() = False` | Workload에 `nvidia.com/gpu` limit이 있는지 |
| SSH `Connection refused` (웹 콘솔 30880도 안 열림) | 서버 쪽 네트워크 문제. 잠시 후 재시도하거나 다른 네트워크에서 시도 |
| SSH `Permission denied (publickey)` | `SSH_PUBLIC_KEY` 값, 로컬에서 쓰는 개인키가 짝이 맞는지 |
| 재배포 후 `REMOTE HOST IDENTIFICATION HAS CHANGED` | `/data/.ssh-host-keys`가 지워졌는지 확인 후 `ssh-keygen -R "[210.125.70.71]:<NodePort>"` |

### CUDA 11.8 빌드 (참고)

현재 서버 드라이버는 CUDA 13.0까지 지원하므로 필요 없습니다.
드라이버가 CUDA 11.x만 지원하는 서버로 옮길 경우에만 아래처럼 빌드합니다.

```bash
docker build \
  --build-arg BASE_IMAGE=nvidia/cuda:11.8.0-base-ubuntu22.04 \
  --build-arg TORCH_VERSION=2.7.1 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu118 \
  -t yeram109/pixlate-gpu:torch2.7-cu118 .
```
