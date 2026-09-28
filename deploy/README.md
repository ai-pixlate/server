# EC2 배포 (백엔드)

AWS에 올라가 있는 백엔드 구성과 배포·점검 방법입니다. 2026-09-28 기준입니다.

주소·계정 ID·비밀번호는 이 레포에 두지 않습니다. 팀 공유 문서에서 확인합니다.

## 구성

```
사용자 ─ ALB(pixlate-alb, HTTP:80) ─ 대상 그룹 Pixlate-tg(HTTP:8000, /health)
                                        │
                                   EC2 pixate-server (t3.micro, 탄력적 IP)
                                   ├ pixlate-api          (8000)
                                   ├ pixlate-redis        (6379, 비밀번호)
                                   ├ pixlate-worker-ocr   (ocr 큐)
                                   └ pixlate-worker-cpu   (cpu 큐)
                                        │
                     RDS pixlate-db (PostgreSQL 16) · S3 · ECR pixlate-api

학교 GPU(KubeSphere) ─ gpu 워커 ─→ EC2 Redis 6379 · RDS 5432  (gpu 큐, gpu/README.md 4장)
```

| 자원 | 이름 | 비고 |
| --- | --- | --- |
| EC2 | `pixate-server` | Ubuntu 24.04, t3.micro, 탄력적 IP. BE 레포는 `~/pixlate-api` |
| 이미지 | ECR `pixlate-api:latest` | EC2에서 빌드해 푸시 |
| DB | RDS `pixlate-db` | PostgreSQL 16, 기본 VPC, 퍼블릭 액세스 예(학교 GPU 접속용) |
| 스토리지 | S3 | 버킷 이름은 `S3_BUCKET`. DB에는 키만 저장하고 조회 때 presigned URL |
| 로드 밸런서 | ALB `pixlate-alb` → `Pixlate-tg` | HTTP만. HTTPS는 도메인·인증서 준비 후 |
| AMI | `pixlate-be-ami-v2` | 이 문서의 구성 그대로. 프라이빗 유지(env 파일에 비밀번호 포함) |
| IAM 역할 | `pixate-ec2-s3-role` | S3 + ECR PowerUser. 키 없이 인증 |

### 보안 그룹

| 보안 그룹 | 인바운드 | 소스 |
| --- | --- | --- |
| `pixlate-alb-sg` (ALB) | 80 | 0.0.0.0/0 |
| `launch-wizard-1` (EC2) | 6379 (Redis) | 학교 GPU 출구 IP /32 |
| `pixlate-rds-sg` (RDS) | 5432 | EC2 보안 그룹, 학교 GPU 출구 IP /32 |

- EC2의 8000(ALB → API)·22(인스턴스 연결) 규칙은 콘솔 설정을 기준으로 합니다.
- 6379·5432는 `0.0.0.0/0`으로 열지 않습니다.
- 학교 GPU 출구 IP는 클러스터 공용이라, Redis·RDS 비밀번호가 실제 방어선입니다.

## 서버 파일

| 레포 파일 | EC2 위치 | 역할 |
| --- | --- | --- |
| `deploy/start-pixlate.sh` | `/opt/pixlate/start-pixlate.sh` | ECR 로그인·pull 후 컨테이너 교체 |
| `deploy/pixlate.service` | `/etc/systemd/system/pixlate.service` | 부팅 때 위 스크립트 실행 |
| `deploy/pixlate.env.example` | `/etc/pixlate/pixlate.env` (root 600) | 환경변수. 실제 값은 서버에만 |

EC2에는 gpu 워커를 띄우지 않습니다. gpu 큐는 학교 GPU 워커만 소비합니다.

## 배포

EC2 콘솔 → 인스턴스 연결(사용자 `ubuntu`)에서 실행합니다. 기준 브랜치는 `develop`입니다.

```bash
cd ~/pixlate-api && git pull origin develop && docker build -t pixlate-api .
```

```bash
REGION=ap-northeast-2; REGISTRY=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$REGION.amazonaws.com; aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY; docker tag pixlate-api:latest $REGISTRY/pixlate-api:latest; docker push $REGISTRY/pixlate-api:latest
```

```bash
sudo systemctl restart pixlate
```

- DB 스키마가 바뀐 배포면 재시작 뒤 마이그레이션을 적용합니다.
  `sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api alembic upgrade head`
- Redis 컨테이너는 볼륨 없이 새로 뜨므로, 재시작하면 큐에 쌓여 있던 작업은 사라집니다.

## 점검

```bash
# 컨테이너 4개(api·redis·worker-ocr·worker-cpu) Up
docker ps --format "table {{.Names}}\t{{.Status}}"

# 워커: ocr·cpu·gpu 3 nodes online
docker exec pixlate-worker-ocr celery -A app.celery_app inspect ping

# 백엔드 테스트 (AI 파이프라인 테스트는 numpy 등이 이미지에 없어 제외)
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api python -m pytest tests -q --ignore-glob='tests/test_pipeline_*'

# gpu 큐 대기 개수 (GPU 워커가 붙어 있으면 0)
RP=$(sudo grep '^REDIS_PASSWORD=' /etc/pixlate/pixlate.env | cut -d= -f2-); docker exec -e REDISCLI_AUTH="$RP" pixlate-redis redis-cli -n 0 LLEN gpu; unset RP
```

컨테이너가 안 뜨면 `sudo journalctl -u pixlate -n 50 --no-pager`로 스크립트 로그를 봅니다.

## AMI로 새 인스턴스 띄우기

아래 세 가지는 AMI에 들어가지 않으므로 시작할 때 직접 지정합니다.

1. IAM 역할 `pixate-ec2-s3-role`
2. 보안 그룹 `launch-wizard-1` (RDS 접근)
3. 인스턴스 메타데이터 **홉 제한 2** — 1이면 컨테이너가 IAM 역할을 못 써서 S3가 실패합니다

부팅만으로 `pixlate.service`가 컨테이너를 띄웁니다.

**서버를 2대 이상 동시에 띄우지 않습니다.** 인스턴스마다 Redis가 따로 떠서, GPU 워커가 붙지 않은 쪽의 gpu 작업은 처리되지 않습니다. 여러 대로 늘리려면 Redis를 공용(ElastiCache 등)으로 먼저 옮깁니다.

## 알아둘 점

- `/etc/pixlate/pixlate.env`는 root 600이므로 `docker run --env-file`에는 `sudo`가 필요합니다.
- `less` 화면에서 나올 때는 영문 입력으로 `q`를 누릅니다. 페이지 없이 보려면 `export AWS_PAGER=""`.
- AMI 설명에는 ASCII만 넣을 수 있습니다(한글이 있으면 생성 실패).

## 남은 작업

- 비밀번호 교체 후 Secrets Manager로 이전 (지금은 서버 env 파일)
- HTTPS (도메인·ACM 인증서)
- Redis 영속화 또는 ElastiCache
- GPU 워커 자동 기동 (pod 재시작 대응)
- CI/CD
