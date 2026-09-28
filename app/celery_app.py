"""Celery 앱 설정.

broker·backend는 Redis. 환경변수로 주입하며 기본값은 docker 네트워크상의
pixlate-redis 컨테이너. 태스크는 app.tasks 에서 자동 로드(include).

워커 큐 3분할(목표 아키텍처): 자원 종류별로 큐를 나눠 워커를 따로 띄운다.
  - ocr : N2 분석/OCR (PaddleOCR 예정)          → run_analyze
  - gpu : N4 인페인팅 (LaMa GPU 예정)           → run_inpaint
  - cpu : N4 번역(LLM 호출)·N6 렌더(Pillow)     → run_translate, run_render
라우팅은 task_routes(설정)로 처리하므로 API는 그대로 .delay() 만 호출한다.

gpu 큐는 EC2가 아니라 학교 GPU 워커가 EC2 Redis로 접속해 소비한다. 학교 서버는
점검·재시작을 우리가 통제할 수 없으므로, 워커가 처리 도중 죽어도 작업이 큐로
돌아오게 acks_late 로 둔다(gpu/worker-test T3 결과). 같은 작업이 두 번 실행될 수
있으므로 태스크는 멱등이어야 한다(app.tasks.run_inpaint).

broker/backend 주소·비밀번호는 코드에 두지 않고 CELERY_BROKER_URL /
CELERY_RESULT_BACKEND 로만 받는다. 기본값은 EC2 docker 네트워크 내부 개발용이며,
학교 GPU에서는 해석되지 않으므로 반드시 환경변수로 EC2 도달 주소를 넣는다.
"""
import os

from celery import Celery
from kombu import Queue

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://pixlate-redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://pixlate-redis:6379/1")

# 완료 신호(ack) 없이 이 시간이 지나면 Redis가 작업을 다른 워커에게 다시 준다.
# 모든 큐에 공통 적용되므로 **가장 긴 태스크보다 길어야** 한다. 짧으면 아직 처리 중인
# 작업이 재전달돼 중복 실행된다(run_analyze·run_translate는 멱등이 아니라 행이 중복됨).
# 실제 OCR은 긴 원본을 타일로 나눠 처리해 수 분이 걸릴 수 있어 Celery 기본값 3600초.
#
# 대가: 워커 전체가 처리 도중 죽으면 그 작업은 이 시간이 지나야 다시 전달된다.
# 로컬 실측(celery 5.6.3, kill 후 새 워커 기동): 10·45초 → 93초, 150초 → 192초.
# 약 90초 이하는 바닥 시간이 있고, 그보다 크면 대략 이 값 + 40초가 걸린다.
# 멈춘 작업 감지는 재전달이 아니라 DB 기준 점검으로 분리한다(후속).
# 값을 바꿀 때는 API·EC2 워커·GPU 워커가 같은 값을 쓰게 한다.
VISIBILITY_TIMEOUT = int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "3600"))

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
    # 워커가 처리 도중 죽어도 작업이 사라지지 않게: 끝난 뒤에 ack, 워커 유실 시 재큐잉
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # acks_late 에서 미리 여러 건을 가져가 두면 워커가 죽을 때 그만큼 재처리가 늦어진다
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT},
)
