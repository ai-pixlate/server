"""pytest 루트 설정 — 리포 루트를 import 경로에 넣어 `import app` 가 되게 한다."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
