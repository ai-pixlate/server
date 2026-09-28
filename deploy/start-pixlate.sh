#!/usr/bin/env bash
# EC2 부팅 시 pixlate.service 가 실행하는 스크립트. 설치 위치: /opt/pixlate/start-pixlate.sh
# ECR 최신 이미지를 받아 redis·api·워커(ocr·cpu) 컨테이너를 새로 띄운다.
# EC2 의 실제 파일과 같고, 계정 ID 만 하드코딩 대신 IAM 역할로 조회한다.
set -euo pipefail
REGION=ap-northeast-2
AWS=/snap/bin/aws
ACCOUNT=$($AWS sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
IMAGE=$REGISTRY/pixlate-api:latest
ENVFILE=/etc/pixlate/pixlate.env
REDIS_PASSWORD=$(grep '^REDIS_PASSWORD=' "$ENVFILE" | cut -d= -f2-)
[ -n "$REDIS_PASSWORD" ] || { echo "REDIS_PASSWORD 없음 - 중단"; exit 1; }

$AWS ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker pull "$IMAGE"
docker network inspect pixlate-net >/dev/null 2>&1 || docker network create pixlate-net

# Redis: 비밀번호 필수 + 6379 외부 게시 (보안 그룹으로 학교 GPU 출구 IP만 허용)
docker rm -f pixlate-redis 2>/dev/null || true
docker run -d --name pixlate-redis --network pixlate-net -p 6379:6379 --restart unless-stopped \
  redis:7-alpine redis-server --requirepass "$REDIS_PASSWORD"

docker rm -f pixlate-api 2>/dev/null || true
docker run -d --name pixlate-api --network pixlate-net -p 8000:8000 --restart unless-stopped --env-file "$ENVFILE" "$IMAGE"

# 워커: EC2 는 ocr·cpu 만. gpu 큐는 학교 GPU 워커가 가져간다(gpu/README.md 4장).
docker rm -f pixlate-worker-gpu 2>/dev/null || true
for Q in ocr cpu; do
  docker rm -f "pixlate-worker-$Q" 2>/dev/null || true
  docker run -d --name "pixlate-worker-$Q" --network pixlate-net --restart unless-stopped --env-file "$ENVFILE" "$IMAGE" \
    celery -A app.celery_app:celery_app worker -Q "$Q" --pool=solo -n "$Q@%h" --loglevel=info
done
echo "pixlate stack started."
