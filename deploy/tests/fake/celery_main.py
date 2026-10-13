# 가짜 `python -m celery` — 가짜 celery 실행 파일(fakeproc)로 바꿔 실행한다(같은 PID 라 TERM·웜 종료 흉내가 그대로 유지된다).
# 실제 이미지의 import 경로 시험은 tests/worker_import.sh 가 따로 한다.
import os
import sys

os.execv("/usr/local/bin/celery", ["celery", *sys.argv[1:]])
