#!/bin/bash
# 시험용 start-pixlate.sh 흉내(pixlate.service 의 ExecStart). 모드를 읽고 적용 결과(deploy.mode.applied)를 남긴다.
# /etc/pixlate/fake-be 가 fail 이면 live 를 live-incomplete 로 남기고 실패한다(네트워크·가짜 API 는 그대로 남는다).
set -u
CONF=/etc/pixlate
mode=$(tr -d ' \r\n' < $CONF/deploy.mode 2>/dev/null || echo maintenance)
echo "$mode-in-progress" > $CONF/deploy.mode.applied
docker network inspect pixlate-net >/dev/null 2>&1 || docker network create pixlate-net >/dev/null
docker inspect fake-api >/dev/null 2>&1 || docker create --name fake-api --network pixlate-net --network-alias pixlate-api \
  busybox:1.36 sh -c 'mkdir -p /w && echo ok > /w/index.html && exec httpd -f -p 8000 -h /w' >/dev/null
if [ "$mode" = live ]; then
  docker start fake-api >/dev/null
  if [ "$(cat $CONF/fake-be 2>/dev/null)" = fail ]; then echo live-incomplete > $CONF/deploy.mode.applied; echo "[fake-be] live 미완료"; exit 1; fi
  docker update --restart unless-stopped fake-api >/dev/null
  echo live > $CONF/deploy.mode.applied
else
  docker update --restart no fake-api >/dev/null; docker stop -t 5 fake-api >/dev/null
  echo "$mode" > $CONF/deploy.mode.applied
fi
date +%s%N > /tmp/be_done_ns
echo "[fake-be] applied=$(cat $CONF/deploy.mode.applied)"
