# 베이스는 digest 로 고정한다(같은 태그가 다른 이미지를 가리켜도 빌드가 바뀌지 않게). 운영 빌드가 쓴 값과 같다:
# python 3.12.15-slim-trixie 다중 아키텍처 index(linux/amd64 포함). 올릴 때는 이 줄만 바꾼다.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

COPY requirements.txt constraints.txt ./
COPY pipeline/requirements.txt ./pipeline/requirements.txt
# BE + AI 파이프라인 기본 의존성(워커가 ④⑤⑦·③-1·analyze 를 실제로 호출한다). OCR(PaddleOCR)·GPU(LaMa)는 별도 이미지
# 버전은 constraints.txt(운영 이미지 pip freeze)로 고정한다 — 설치 결과 대조는 CI(ci-develop.yml)가 한다
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt -r pipeline/requirements.txt

# ⑨ 미리보기 · N6 최종 렌더(app/html_render.py): Playwright 가 고정한 Chromium headless shell 만(브라우저 · 버전은 playwright 버전이 정한다).
# libatomic1: Playwright 의 node 드라이버가 요구(trixie slim 에 없음). --with-deps 가 Chromium 실행 라이브러리 · 기본 글꼴을 함께 깐다.
# 조판 글꼴은 이 이미지의 Pretendard 고정본(app/assets/fonts)을 쓰고 시스템 글꼴로 대체 촬영하지 않는다(측정 엔진이 대체를 실패로 본다).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
