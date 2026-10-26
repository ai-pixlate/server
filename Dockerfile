# 베이스는 digest 로 고정한다(같은 태그가 다른 이미지를 가리켜도 빌드가 바뀌지 않게). 운영 빌드가 쓴 값과 같다:
# python 3.12.15-slim-trixie 다중 아키텍처 index(linux/amd64 포함). 올릴 때는 이 줄만 바꾼다.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

COPY requirements.txt ./
COPY pipeline/requirements.txt ./pipeline/requirements.txt
# BE + AI 파이프라인 기본 의존성(워커가 ④⑤⑦·③-1·analyze 를 실제로 호출한다). OCR(PaddleOCR)·GPU(LaMa)는 별도 이미지
RUN pip install --no-cache-dir -r requirements.txt -r pipeline/requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
