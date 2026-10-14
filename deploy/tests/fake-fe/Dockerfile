# FE 배포 시험용 가짜 FE(busybox). 3000 에서 /jobs/new 를 응답한다(healthcheck 경로).
#   TERM_MODE=exit   : TERM 을 받으면 0 으로 끝난다
#   TERM_MODE=ignore : TERM 을 무시한다(정지 대기 초과·강제 종료 없음 확인용)
#   /tmp/crash 를 만들면 그 표시를 지우고 3 으로 끝난다(실행 중 비정상 종료 흉내 — 같은 컨테이너를 다시 띄우면 정상 기동)
FROM busybox:1.36
ARG TERM_MODE=exit
RUN mkdir -p /w/jobs && echo ok > /w/jobs/new \
 && printf '#!/bin/sh\nif [ "%s" = ignore ]; then trap "" TERM; else trap "exit 0" TERM; fi\nhttpd -p 3000 -h /w\nwhile [ ! -e /tmp/crash ]; do sleep 1; done\nrm -f /tmp/crash\nexit 3\n' "$TERM_MODE" > /fe.sh \
 && chmod +x /fe.sh
CMD ["/fe.sh"]
