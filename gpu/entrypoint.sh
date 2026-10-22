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

# PaddleX 모델 캐시(OCR 워커): /root/.paddlex → /data/.cache/paddlex. 재시작마다 모델을 다시 받지 않게 한다.
# 몇 번 실행해도 같은 결과이고, 이미 있는 디렉터리·파일·다른 링크는 바꾸지 않는다(경고만). 실패해도 sshd 는 띄운다.
link_paddlex_cache() {
    local target="$DATA_DIR/.cache/paddlex" link=/root/.paddlex
    [ -d "$DATA_DIR" ] || return 0
    if [ ! -d "$target" ]; then
        if [ -e "$target" ] || [ -L "$target" ]; then
            echo "[entrypoint] $target 가 디렉터리가 아니다. PaddleX 캐시 연결을 건너뛴다" >&2; return 0
        fi
        mkdir -p "$target" || { echo "[entrypoint] $target 생성 실패. PaddleX 캐시 연결을 건너뛴다" >&2; return 0; }
    fi
    if [ -L "$link" ]; then
        [ "$(readlink "$link")" = "$target" ] \
            || echo "[entrypoint] $link 가 다른 곳을 가리킨다. 그대로 둔다" >&2
    elif [ -e "$link" ]; then
        echo "[entrypoint] $link 가 이미 있다(링크 아님). 그대로 둔다" >&2
    else
        ln -s "$target" "$link" || echo "[entrypoint] $link 링크 생성 실패" >&2
    fi
    return 0
}
link_paddlex_cache || true

exec /usr/sbin/sshd -D -e
