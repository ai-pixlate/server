"""로컬 FE·BE 브라우저 시험 전용 가짜 OAuth 제공자(카카오·Google). 운영 이미지에서 제외한다(.dockerignore).

실행은 `python -m tools.local_oauth.serve --local-fake-providers` 하나뿐이다. `app/` 은 이 패키지를 import 하지 않는다.
"""
