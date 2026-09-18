"""학교 GPU 서버에서 Celery 워커가 Redis의 gpu 큐 작업을 꺼내 처리하는지 확인하는 테스트용 앱.

큐 구성과 라우팅은 app/celery_app.py 와 같게 맞추고, 실제 인페인팅 대신
지정한 시간 동안 GPU 연산만 하는 가짜 작업(fake_inpaint)을 쓴다. DB·S3는 쓰지 않는다.

환경변수
  CELERY_BROKER_URL / CELERY_RESULT_BACKEND  Redis 주소 (기본: localhost)
  ACKS_LATE=1          작업을 끝낸 뒤에 완료 처리 (워커가 죽으면 작업을 다시 큐로 돌림)
  VISIBILITY_TIMEOUT   완료 처리되지 않은 작업을 다시 큐로 돌리기까지 기다리는 초 (기본 3600)
"""

import os
import socket
import time

from celery import Celery
from kombu import Queue

ACKS_LATE = os.getenv("ACKS_LATE", "0") == "1"

app = Celery(
    "pixlate_gpu_test",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    task_default_queue="cpu",
    task_queues=(Queue("ocr"), Queue("cpu"), Queue("gpu")),
    task_routes={"gpu_test.fake_inpaint": {"queue": "gpu"}},
    task_acks_late=ACKS_LATE,
    task_reject_on_worker_lost=ACKS_LATE,
    worker_prefetch_multiplier=1 if ACKS_LATE else 4,
    broker_transport_options={"visibility_timeout": int(os.getenv("VISIBILITY_TIMEOUT", "3600"))},
)


@app.task(name="gpu_test.fake_inpaint")
def fake_inpaint(section_id: int, seconds: float = 3.0) -> dict:
    """seconds 동안 GPU 행렬곱을 반복해 인페인팅 처리 시간을 흉내 낸다."""
    import torch

    started = time.time()
    x = torch.randn(4096, 4096, device="cuda")
    loops = 0
    while time.time() - started < seconds:
        x = torch.tanh(x @ x)
        torch.cuda.synchronize()
        loops += 1

    return {
        "sectionId": section_id,
        "host": socket.gethostname(),
        "gpu": torch.cuda.get_device_name(0),
        "processSeconds": round(time.time() - started, 3),
        "loops": loops,
    }
