# EC2 배포 (백엔드)

AWS에 올라가 있는 백엔드 구성과 배포·점검 방법입니다. 2026-10-12 기준입니다.

주소·계정 ID·비밀번호·버킷 이름은 이 레포에 두지 않습니다. 팀 공유 문서에서 확인합니다.

## 구성

```
사용자 ─ ALB(pixlate-alb, HTTP:80) ─ 대상 그룹 Pixlate-tg(HTTP:8000, /health)
                                        │
                                   EC2 pixate-server (t3.micro, 탄력적 IP)
                                   ├ pixlate-api          (8000)
                                   ├ pixlate-redis        (6379, 비밀번호)
                                   ├ pixlate-worker-cpu   (cpu 큐)
                                   └ pixlate-recovery     (DB 기준 작업 복구)
                                        │
                     RDS pixlate-db (PostgreSQL 16) · S3 · ECR pixlate-api

학교 GPU pod(KubeSphere) ─ gpu 워커(gpu 큐, LaMa) · ocr 워커(ocr 큐, 분석)
                          ─→ EC2 Redis 6379 · RDS 5432 · S3   (gpu/README.md 4장)
```

- `ocr` 큐(분석: 섹션 분해·OCR·병합)는 학교 GPU pod가 소비합니다. PaddleOCR은 섹션 하나에 수 GiB~18GiB를 써서
  t3.micro(총 909MiB)에서 돌릴 수 없습니다(측정값은 `gpu/README.md` 4장).
- EC2에는 gpu·ocr 워커를 두지 않습니다. 예전 `pixlate-worker-ocr` 컨테이너가 남아 있으면 `start-pixlate.sh`가 멈춰 둡니다(삭제하지 않음).

| 자원 | 이름 | 비고 |
| --- | --- | --- |
| EC2 | `pixate-server` | Ubuntu 24.04, t3.micro, 탄력적 IP. BE 레포는 `~/pixlate-api` |
| 이미지 | ECR `pixlate-api:<SHA 태그>` | 배포는 **digest로 고정**한다. `latest`는 쓰지 않는다 |
| DB | RDS `pixlate-db` | PostgreSQL 16, 기본 VPC, 퍼블릭 액세스 예(학교 GPU 접속용) |
| 스토리지 | S3 | 버킷 이름은 `S3_BUCKET`. DB에는 키만 저장하고 조회 때 presigned URL |
| 로드 밸런서 | ALB `pixlate-alb` → `Pixlate-tg` | HTTP만. HTTPS는 도메인·인증서 준비 후 |
| AMI | `pixlate-be-ami-v2` | 2026-09-28 구성(옛 시작 스크립트). 새 스크립트 반영 후 다시 굽는다 |
| IAM 역할 | `pixate-ec2-s3-role` | S3 + ECR(push 가능 확인). 키 없이 인증 |

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
| `deploy/start-pixlate.sh` | `/opt/pixlate/start-pixlate.sh` | 배포 모드에 따라 컨테이너를 멈추거나 digest 고정 이미지로 띄운다 |
| `deploy/pixlate.service` | `/etc/systemd/system/pixlate.service` | 부팅 때 위 스크립트 실행 |
| `deploy/bin/pixlate-mode` | `/opt/pixlate/bin/pixlate-mode` | 모드 기록과 적용을 한 번에 |
| `deploy/bin/redact_run.py` | `/opt/pixlate/bin/redact_run.py` | 로그 정제 래퍼(아래) |
| `deploy/bin/gemini_check.py` | `/opt/pixlate/bin/gemini_check.py` | Gemini 키 주입 확인 / 실제 요청 1회 |
| `deploy/bin/ocr_measure.py` | (학교 GPU `/data/pixlate/bin/`) | OCR 1섹션 메모리·시간 실측 |
| `deploy/pixlate.env.example` | `/etc/pixlate/pixlate.env` (root 600) | API·cpu 워커·복구 공용 환경변수. 실제 값은 서버에만 |
| — | `/etc/pixlate/worker-gemini.env` (root:root 600) | `GEMINI_API_KEY` 한 줄. **cpu 워커에만** 연결(API·복구에는 없음) |
| — | `/etc/pixlate/release.conf` | `RELEASE_SHA=<40자리>`, `API_DIGEST=sha256:<64자리>` (비밀 아님) |
| — | `/etc/pixlate/deploy.mode` | `maintenance` · `validate` · `live` 한 단어 |

### 배포 모드

| 모드 | API | cpu 워커·복구 | 재부팅 시 |
| --- | --- | --- | --- |
| `maintenance` | 정지 | 정지 | 아무것도 자동 시작하지 않는다(Redis만) |
| `validate` | `127.0.0.1:8000`에만(ALB·외부 유입 없음) | 정지 | 자동 시작 안 함(서비스가 다시 띄움) |
| `live` | `:8000` 외부 공개 | 실행(API → cpu → 복구 순) | Docker restart 정책 + 서비스 재실행 |

- 모드 파일이 없거나 값이 잘못되면 `maintenance`로 동작합니다. `live`로 시작하는 일은 없습니다.
- 모드는 `sudo /opt/pixlate/bin/pixlate-mode <모드>`로만 바꿉니다. 파일만 고치고 적용하지 않은 채 재부팅하면, Docker가 이전 모드의 컨테이너를 먼저 켤 수 있습니다.
- **Redis**: 기존 `pixlate-redis` 컨테이너·볼륨을 지우거나 다시 만들지 않습니다. 멈춰 있으면 같은 컨테이너를 시작하고, 없으면 빈 Redis를 만들지 않고 앱 컨테이너를 멈춘 뒤 실패합니다.
- 이미지 digest·env 파일 내용·래퍼·포트·명령이 바뀐 컨테이너만 다시 만듭니다. 같은 사양이면 재사용합니다.
- `live`는 `worker-gemini.env`가 root:root 600·`GEMINI_API_KEY` 한 줄일 때만 진행하고, 공유 env 파일에 Gemini 키가 있으면 거부합니다.

### 로그 정제

모든 앱 컨테이너는 `docker run --init … python /opt/pixlate/bin/redact_run.py -- <명령>`으로 실행합니다.
래퍼가 표준 출력·오류를 한 줄씩 받아 연결 URL·AWS 호스트·서명·키 형태·`password=` 류·IPv4와
환경변수에 실제로 들어 있는 비밀 값(DB·Redis URL, 비밀번호, JWT, Gemini 키, 버킷 이름)을 가린 뒤에만 내보냅니다.
원문은 Docker 로그·journald·파일에 도달하지 않습니다. 정제에 실패한 줄과 64KiB를 넘는 줄은 버립니다.
시그널(TERM·INT 등)은 그대로 자식에게 전달하고, 자식의 종료 코드(시그널 종료는 128+번호)를 그대로 돌려줍니다.

## 배포

### 1. 이미지 (배포 SHA 하나로)

운영 호스트(t3.micro)에서는 빌드하지 않습니다. 개발 PC에서 **배포 SHA의 추적 파일만**으로 빌드합니다.

```bash
git archive <배포 SHA> | tar -x -C <빈 폴더>
docker build --no-cache --pull -t pixlate-api:<짧은 SHA> <빈 폴더>
```

개발 PC 계정에 ECR 권한이 없으면 `docker save` → EC2 `docker load` → EC2 역할로 push 합니다.

```bash
docker save pixlate-api:<짧은 SHA> -o images.tar          # 개발 PC. sha256sum 을 함께 기록
# EC2: 체크섬 확인 → sudo docker load -i images.tar → ECR 로그인 → 새 태그로 push (latest 이동 금지)
```

push 후 ECR digest가 빌드한 이미지 ID와 같은지 확인하고, 그 digest를 `/etc/pixlate/release.conf`의 `API_DIGEST`에 적습니다.

### 2. 전환

```bash
sudo /opt/pixlate/bin/pixlate-mode maintenance     # 유입·소비·복구 정지, Redis 유지
# 학교 GPU 워커도 웜 종료 (gpu/README.md 4장: worker.sh <gpu|ocr> stop)
# 큐(대기·unacked)와 DB 실행·시도 상태 확인 → 실행 중이면 진행하지 않는다
# 백업(아래) → 마이그레이션(아래)
sudo /opt/pixlate/bin/pixlate-mode validate        # API 를 내부에서만 확인
sudo /opt/pixlate/bin/pixlate-mode live            # 정상 유입·소비 재개(별도 결정 후)
```

### 3. 백업

RDS 서버와 같은 버전의 `pg_dump`를 digest로 고정한 `postgres:16.x` 이미지에서 실행합니다. DB URL은 컨테이너 안에서
`PG*` 환경변수로 풀어 쓰고 명령 인자에 넣지 않습니다. 덤프는 `/var/lib/pixlate-backup/<시각>/`(디렉터리 700, 파일 600)에 두며 사용자 데이터가 들어 있습니다.

- 확인: `pg_dump` 종료 코드 0, 크기·SHA-256 기록.
- 복원 시험: 네트워크 없는 임시 `postgres` 컨테이너에 `pg_restore --exit-on-error` 후 테이블별 행 수를 원본과 비교하고 컨테이너를 지웁니다. 운영 DB·로컬 사용 DB에 시험 복원하지 않습니다.
- RDS 스냅샷(선택, RDS 권한자): 콘솔 RDS → 데이터베이스 `pixlate-db` → 작업 → 스냅샷 생성(`pixlate-db-pre-<revision>-<날짜>`),
  상태가 `사용 가능`이 될 때까지 기다립니다. 스냅샷 복원은 **새 DB 인스턴스**를 만들므로(기존 인스턴스를 덮어쓰지 않음),
  복원 뒤 보안 그룹·퍼블릭 액세스를 원본과 같게 맞추고 아래처럼 `DATABASE_URL`을 바꿉니다.
- 실제 복구: 같은 RDS의 새 DB(DB 역할에 createdb 권한 있음) 또는 스냅샷 복원 인스턴스에 복원한 뒤, `pixlate.env`와 학교 GPU `/data/server.env`의 `DATABASE_URL`을 바꾸고 컨테이너·워커를 다시 띄웁니다. Redis 메시지가 복원 시점과 맞는지 먼저 확인합니다.

### 4. 마이그레이션

```bash
sudo docker run --rm --init --env-file /etc/pixlate/pixlate.env \
  -v /opt/pixlate/bin/redact_run.py:/opt/pixlate/bin/redact_run.py:ro \
  <레지스트리>/pixlate-api@<API_DIGEST> python /opt/pixlate/bin/redact_run.py -- alembic upgrade head
```

- 배포 SHA의 Alembic head가 하나인지 먼저 확인합니다. 실패하면 maintenance를 유지하고 실제 revision을 확인합니다.
- 스키마가 올라간 뒤에는 옛 API·워커를 다시 켜지 않습니다.

## 점검

```bash
# 컨테이너: live 이면 api·redis·worker-cpu·recovery Up, worker-ocr 은 Exited(옛 컨테이너)
sudo docker ps -a --format "table {{.Names}}\t{{.Status}}"
sudo /opt/pixlate/bin/pixlate-mode status

# 워커: cpu(EC2)·ocr·gpu(학교 GPU) 3 nodes online
sudo docker exec pixlate-worker-cpu celery -A app.celery_app inspect ping

# 큐 대기 개수(값을 출력하지 않는 방법으로 비밀번호 전달)
RP=$(sudo grep '^REDIS_PASSWORD=' /etc/pixlate/pixlate.env | cut -d= -f2-); sudo docker exec -e REDISCLI_AUTH="$RP" pixlate-redis redis-cli -n 0 LLEN ocr; unset RP
```

컨테이너가 안 뜨면 `sudo journalctl -u pixlate -n 50 --no-pager`로 스크립트 로그를 봅니다.

- `docker inspect`의 Redis `Cmd`·`docker ps --no-trunc`에는 Redis 비밀번호가 평문으로 보입니다. 출력하거나 공유하지 않습니다.

## 용어집 적재

데이터팀 용어집 xlsx를 RDS `glossary`에 넣는 절차(마이그레이션 `0005` → 파일 검증 → 적재 → 확인)는
[glossary-ingestion.md](glossary-ingestion.md)에 있습니다. DB 작업만 하므로 서비스 재시작은 필요 없습니다.

규제사전·현지부적합 사전(`expression_dictionary`) 적재는 [dictionary-ingestion.md](dictionary-ingestion.md)를 봅니다(마이그레이션 `0006`).

## AMI로 새 인스턴스 띄우기

아래 세 가지는 AMI에 들어가지 않으므로 시작할 때 직접 지정합니다.

1. IAM 역할 `pixate-ec2-s3-role`
2. 보안 그룹 `launch-wizard-1` (RDS 접근)
3. 인스턴스 메타데이터 **홉 제한 2** — 1이면 컨테이너가 IAM 역할을 못 써서 S3가 실패합니다

부팅하면 `pixlate.service`가 모드 파일대로 동작합니다(모드 파일이 없으면 maintenance).

**서버를 2대 이상 동시에 띄우지 않습니다.** 인스턴스마다 Redis가 따로 떠서, 학교 GPU 워커가 붙지 않은 쪽의 ocr·gpu 작업은 처리되지 않습니다. 여러 대로 늘리려면 Redis를 공용(ElastiCache 등)으로 먼저 옮깁니다.

## 알아둘 점

- `/etc/pixlate/*.env`는 root 600이므로 `docker run --env-file`에는 `sudo`가 필요합니다.
- `less` 화면에서 나올 때는 영문 입력으로 `q`를 누릅니다. 페이지 없이 보려면 `export AWS_PAGER=""`.
- AMI 설명에는 ASCII만 넣을 수 있습니다(한글이 있으면 생성 실패).

## 남은 작업

- Redis: 익명 볼륨·AOF 꺼짐(RDB만). 명명 볼륨·AOF 전환과 비밀번호 교체(명령 인자 평문 제거)를 함께 별도 작업으로
- 비밀번호 교체 후 Secrets Manager로 이전 (지금은 서버 env 파일)
- 학교 GPU 워커의 S3 자격 제공 방식 결정
- HTTPS (도메인·ACM 인증서)
- CI/CD
