# 학교 GPU 서버(KubeSphere, V100)용 PyTorch 기본 이미지
# 기본값은 CUDA 12.6 빌드 (서버 드라이버 580, CUDA 13.0 지원에서 동작 확인)
# 주요 의존성 고정: 베이스는 digest 로, /opt/venv 는 opt-venv.constraints.txt 로 고정한다(같은 이미지 재현 보장은 아니다).
# apt 패키지는 고정하지 않는다(우분투 저장소가 이전 버전을 지운다). 다시 빌드하면 tests/image_check.sh 로 확인한다
ARG BASE_IMAGE=nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e
FROM ${BASE_IMAGE}

ARG TORCH_VERSION=2.14.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126
# 1 이면 opt-venv.constraints.txt(CUDA 12.6 기준)를 적용한다. 다른 CUDA 로 빌드할 때만 0
ARG OPT_VENV_LOCK=1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    HF_HOME=/data/.cache/huggingface \
    TORCH_HOME=/data/.cache/torch \
    PIP_CACHE_DIR=/data/.cache/pip

# libgomp1·libgl1·libglib2.0-0t64: OCR 워커(/data/venv-ocr, PaddlePaddle CPU·OpenCV) 런타임. pod 재시작 후에도 있어야 한다
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        openssh-server ca-certificates \
        git curl wget vim tmux htop \
        libgomp1 libgl1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# gpu 워커 venv(/data/venv)가 .pth 로 이 site-packages 를 공유한다. 버전이 바뀌면 LaMa 가 깨질 수 있다
COPY opt-venv.constraints.txt /tmp/opt-venv.constraints.txt
RUN set -eu; \
    if [ "$OPT_VENV_LOCK" = 1 ]; then LOCK="-c /tmp/opt-venv.constraints.txt"; else LOCK=""; fi; \
    python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir $LOCK --upgrade pip \
    && pip install --no-cache-dir $LOCK --index-url "$TORCH_INDEX_URL" \
        "torch==${TORCH_VERSION}" torchvision numpy pillow \
    && pip check \
    && rm -f /tmp/opt-venv.constraints.txt

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
