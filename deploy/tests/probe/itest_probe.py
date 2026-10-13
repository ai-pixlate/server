"""배포 회귀시험 전용 탐침 태스크 — 앱 코드가 아니다. 기존 배포 이미지는 변경하지 않고, 시험 워커에서만 등록한다
(worker_import.sh 가 시험 컨테이너에 마운트하고 --include 로 불러온다). 향후 빌드(`COPY . .`)에 이 파일이 포함되더라도
운영 워커는 app.tasks 만 불러오므로(app/celery_app.py include) 이 태스크가 자동 등록되지는 않는다.

cpu 워커의 실제 태스크(run_judge 등)는 실행 시점에 pipeline 을 지연 import 한다(app/ai_adapters.py 등).
이 태스크도 같은 시점에 같은 모듈을 import 해서, 워커 실행 방식에 따라 import 경로가 유지되는지 확인한다.
"""
import sys

from app.celery_app import celery_app


@celery_app.task(name="itest.lazy_pipeline")
def lazy_pipeline() -> dict:
    try:
        import pipeline  # noqa: F401
        from pipeline.config import load_config  # noqa: F401 — app/ai_adapters.py 와 같은 지연 import
        from pipeline.handoff.judgment import run_judgment  # noqa: F401 — ③-1 judge 경로
        return {"ok": True, "pipeline": pipeline.__file__, "app_dir_in_path": any(p in ("", "/app") for p in sys.path)}
    except ModuleNotFoundError as e:
        return {"ok": False, "error": f"ModuleNotFoundError: {e}", "app_dir_in_path": any(p in ("", "/app") for p in sys.path)}
