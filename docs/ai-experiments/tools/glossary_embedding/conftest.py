"""pytest — 이 도구 폴더를 import 경로에 넣는다(`import experiments_eval` · `import config` 가 되게)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
