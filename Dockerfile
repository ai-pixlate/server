FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
COPY pipeline/requirements.txt ./pipeline/requirements.txt
# BE + AI 파이프라인 기본 의존성(워커가 ④⑤⑦·③-1·analyze 를 실제로 호출한다). OCR(PaddleOCR)·GPU(LaMa)는 별도 이미지
RUN pip install --no-cache-dir -r requirements.txt -r pipeline/requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
