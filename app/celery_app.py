"""Celery 앱 설정.

broker·backend는 Redis. 환경변수로 주입하며 기본값은 docker 네트워크상의
pixlate-redis 컨테이너. 태스크는 app.tasks 에서 자동 로드(include).
"""
import os

from celery import Celery

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
)
