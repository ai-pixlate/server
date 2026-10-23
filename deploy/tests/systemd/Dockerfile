# FE 배포 systemd 연동 시험용 — EC2 와 같은 Ubuntu 24.04(systemd 255) 안에서 systemd 가 Docker·pixlate.service·pixlate-fe.service 를 관리한다.
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus docker.io docker-compose-v2 ca-certificates procps \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-resolved.service systemd-networkd.service getty@tty1.service console-getty.service systemd-logind.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
