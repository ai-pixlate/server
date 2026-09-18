# 학교 GPU 서버(KubeSphere, V100)용 PyTorch 기본 이미지
# 기본값은 CUDA 12.6 빌드 (서버 드라이버 580, CUDA 13.0 지원에서 동작 확인)
ARG BASE_IMAGE=nvidia/cuda:12.6.3-base-ubuntu24.04
FROM ${BASE_IMAGE}

ARG TORCH_VERSION=2.14.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    HF_HOME=/data/.cache/huggingface \
    TORCH_HOME=/data/.cache/torch \
    PIP_CACHE_DIR=/data/.cache/pip

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        openssh-server ca-certificates \
        git curl wget vim tmux htop \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url "$TORCH_INDEX_URL" \
        "torch==${TORCH_VERSION}" torchvision \
    && pip install --no-cache-dir numpy pillow

# SSH: 키 인증만 허용 (공인 IP로 포트가 열리므로 비밀번호 로그인 차단)
COPY sshd_config /etc/ssh/sshd_config.d/pixlate.conf
RUN mkdir -p /run/sshd \
    && rm -f /etc/ssh/ssh_host_*

# SSH 세션은 Docker ENV를 물려받지 않으므로 pam_env용 /etc/environment에 기록
RUN printf '%s="%s"\n' \
        PATH "$PATH" \
        VIRTUAL_ENV "$VIRTUAL_ENV" \
        LD_LIBRARY_PATH "${LD_LIBRARY_PATH:-}" \
        DATA_DIR "$DATA_DIR" \
        HF_HOME "$HF_HOME" \
        TORCH_HOME "$TORCH_HOME" \
        PIP_CACHE_DIR "$PIP_CACHE_DIR" \
        > /etc/environment

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY check_gpu.py /opt/pixlate/check_gpu.py
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /data
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
