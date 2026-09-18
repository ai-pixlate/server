#!/usr/bin/env bash
# 컨테이너 PID 1: SSH 공개키·호스트키를 준비한 뒤 sshd를 포그라운드로 실행한다.
# 컨테이너가 재시작되면 /data(PVC) 밖은 초기화되므로 유지할 값은 /data에서 복원한다.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
HOST_KEY_DIR="$DATA_DIR/.ssh-host-keys"

mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh

# 공개키: 환경변수 SSH_PUBLIC_KEY + /data/.ssh/authorized_keys
: > /root/.ssh/authorized_keys
if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
    printf '%s\n' "$SSH_PUBLIC_KEY" >> /root/.ssh/authorized_keys
fi
if [ -f "$DATA_DIR/.ssh/authorized_keys" ]; then
    cat "$DATA_DIR/.ssh/authorized_keys" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
if [ ! -s /root/.ssh/authorized_keys ]; then
    echo "[entrypoint] 등록된 SSH 공개키가 없습니다. KubeSphere 웹 터미널로만 접속할 수 있습니다." >&2
fi

# 호스트키: 재시작해도 같은 키를 쓰도록 /data에 보관 (known_hosts 경고 방지)
if [ -d "$DATA_DIR" ]; then
    mkdir -p "$HOST_KEY_DIR"
    chmod 700 "$HOST_KEY_DIR"
    if compgen -G "$HOST_KEY_DIR/ssh_host_*_key" > /dev/null; then
        cp -p "$HOST_KEY_DIR"/ssh_host_* /etc/ssh/
    else
        ssh-keygen -A
        cp -p /etc/ssh/ssh_host_* "$HOST_KEY_DIR"/
    fi
else
    echo "[entrypoint] $DATA_DIR 가 마운트되지 않았습니다. 재시작 시 데이터와 호스트키가 사라집니다." >&2
    ssh-keygen -A
fi
chmod 600 /etc/ssh/ssh_host_*_key

mkdir -p "${HF_HOME:-$DATA_DIR/.cache/huggingface}" "${TORCH_HOME:-$DATA_DIR/.cache/torch}" 2>/dev/null || true

exec /usr/sbin/sshd -D -e
