FROM python:3.11-slim

# Tesseract 및 의존성 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng libtesseract-dev libleptonica-dev \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Seoul
WORKDIR /app

# Python 의존성
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스
COPY . .

# 기본 엔트리포인트: cli.py
ENTRYPOINT ["python", "cli.py"]
