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

| 항목 | 기본값 |
| --- | --- |
| 베이스 이미지 | `nvidia/cuda:12.6.3-base-ubuntu24.04` |
| PyTorch | `2.14.0+cu126` |
| Python | 3.12 (Ubuntu 24.04 기본) |

## 꼭 알아둘 점

- **컨테이너가 재시작되면 `/data`(PVC) 외의 모든 변경이 사라집니다.** 코드·데이터·모델 가중치는 `/data` 아래에 둡니다.
- 캐시 경로는 `/data`로 잡혀 있어 재시작해도 다시 받지 않습니다.
  - `HF_HOME=/data/.cache/huggingface`, `TORCH_HOME=/data/.cache/torch`, `PIP_CACHE_DIR=/data/.cache/pip`
- 컨테이너 안에서 `pip install` 한 패키지도 재시작 시 사라집니다. 계속 쓸 패키지는 `Dockerfile`에 추가해 이미지를 다시 빌드합니다.
- SSH 포트는 공인 IP(`210.125.70.71`)로 외부에 열립니다. 비밀번호 로그인은 막혀 있고 공개키로만 접속됩니다.
- 서버 계정 ID·비밀번호, 개인키는 레포에 커밋하지 않습니다.

## 1. 이미지 빌드·푸시 (로컬 PC)

```bash
cd gpu
docker login
docker build -t <DOCKERHUB_USER>/pixlate-gpu:torch2.14-cu126 .
docker push <DOCKERHUB_USER>/pixlate-gpu:torch2.14-cu126
```

이미지에는 비밀 값이 없으므로 Docker Hub 저장소를 public으로 두면 서버에서 별도 인증 없이 받을 수 있습니다.
private으로 두려면 KubeSphere에 이미지 레지스트리 Secret을 등록하고 Workload에 `imagePullSecrets`를 추가해야 합니다.

### CUDA 11.8 빌드 (서버 드라이버가 낮을 때)

컨테이너에서 `nvidia-smi`를 실행해 오른쪽 위 `CUDA Version`을 확인합니다.

| `nvidia-smi`의 CUDA Version | 사용할 빌드 |
| --- | --- |
| 12.x 이상 | 기본 빌드 (cu126) |
| 11.x | 아래 cu118 빌드 |

```bash
docker build \
  --build-arg BASE_IMAGE=nvidia/cuda:11.8.0-base-ubuntu22.04 \
  --build-arg TORCH_VERSION=2.7.1 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu118 \
  -t <DOCKERHUB_USER>/pixlate-gpu:torch2.7-cu118 .
```

## 2. 서버 배포 (KubeSphere 콘솔)

접속: `http://210.125.70.71:30880` → Workspace Management → `virl` → Projects → `virl`

각 단계에서 폼을 채우는 대신 **Edit YAML**에 `k8s/` 파일 내용을 붙여넣어도 됩니다.

1. **볼륨 생성** — `Storage > Persistent Volume Claims > Create`
   - `k8s/pvc.yaml` 참고. Storage Class는 `sandbox-container`, 이름은 `pixlate-data`
2. **SSH 키 준비** (로컬 PC, 최초 1회)
   ```bash
   ssh-keygen -t ed25519 -C "pixlate-gpu"
   ```
   `~/.ssh/id_ed25519.pub` 내용(공개키)을 복사합니다. 개인키(`id_ed25519`)는 공유하지 않습니다.
3. **컨테이너 생성** — `Application Workloads > Workloads > Create`
   - `k8s/workload.yaml`의 `<DOCKERHUB_USER>`, `SSH_PUBLIC_KEY` 값을 바꿔서 사용
   - GPU Limit은 `nvidia.com/gpu: 1`. 필요하면 늘립니다 (컨테이너당 최대 8)
   - 이미지가 `sshd`를 PID 1로 실행하므로 가이드의 `sleep infinity` 명령은 넣지 않습니다
4. **포트 개방** — `Application Workloads > Services > Create`
   - `k8s/service.yaml` 참고. Access Mode는 `NodePort`
   - 생성 후 서비스 상세의 External Access 포트 번호(예: `32570`)를 확인합니다

## 3. 접속·확인

```bash
ssh -p <NodePort> root@210.125.70.71
python /opt/pixlate/check_gpu.py
```

마지막 줄에 `결과: 정상`이 나오면 GPU와 볼륨이 정상입니다.
SSH가 안 되면 콘솔의 `Workloads > pixlate-gpu > Pods > Terminal`로 들어가 같은 스크립트를 실행합니다.

팀원 키를 추가하려면 컨테이너 안에서 `/data/.ssh/authorized_keys`에 공개키를 한 줄씩 넣고 컨테이너를 재시작합니다.

### 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| `torch.cuda.is_available() = False` | Workload에 GPU Limit이 있는지, `nvidia-smi`의 CUDA Version (위 표) |
| `sm_70 (미지원 ...)` | 설치된 PyTorch 빌드가 V100을 지원하지 않음 → cu118 빌드 사용 |
| SSH `Permission denied (publickey)` | `SSH_PUBLIC_KEY` 값, 로컬에서 쓰는 개인키가 짝이 맞는지 |
| 재배포 후 `REMOTE HOST IDENTIFICATION HAS CHANGED` | `/data/.ssh-host-keys`가 지워졌는지 확인 후 `ssh-keygen -R "[210.125.70.71]:<NodePort>"` |
