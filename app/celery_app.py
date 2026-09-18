"""Celery 앱 설정.

broker·backend는 Redis. 환경변수로 주입하며 기본값은 docker 네트워크상의
pixlate-redis 컨테이너. 태스크는 app.tasks 에서 자동 로드(include).

워커 큐 3분할(목표 아키텍처): 자원 종류별로 큐를 나눠 워커를 따로 띄운다.
  - ocr : N2 분석/OCR (PaddleOCR 예정)          → run_analyze
  - gpu : N4 인페인팅 (LaMa GPU 예정)           → run_inpaint
  - cpu : N4 번역(LLM 호출)·N6 렌더(Pillow)     → run_translate, run_render
라우팅은 task_routes(설정)로 처리하므로 API는 그대로 .delay() 만 호출한다.
"""
import os

from celery import Celery
from kombu import Queue

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://pixlate-redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://pixlate-redis:6379/1")

celery_app = Celery(
    "pixlate",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    # 큐 3분할 + 태스크→큐 라우팅
    task_default_queue="cpu",
    task_queues=(
        Queue("ocr"),
        Queue("cpu"),
        Queue("gpu"),
    ),
    task_routes={
        "app.tasks.run_analyze": {"queue": "ocr"},
        "app.tasks.run_inpaint": {"queue": "gpu"},
        "app.tasks.run_translate": {"queue": "cpu"},
        "app.tasks.run_render": {"queue": "cpu"},
    },
)
