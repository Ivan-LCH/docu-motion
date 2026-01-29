# -----------------------------------------------------------------------------------------------------------------------------#
# Dockerfile for DocuMotion
# -----------------------------------------------------------------------------------------------------------------------------#
FROM python:3.11-slim

WORKDIR /app

# Ensure realtime logs
ENV PYTHONUNBUFFERED=1

# 1. 필수 시스템 패키지 및 폰트 설치
RUN apt-get update && apt-get install -y \
    imagemagick \
    ffmpeg \
    fonts-noto-cjk \
    findutils \
    libsndfile1 \
    git \
    sox \
    && rm -rf /var/lib/apt/lists/*

# 2. MoviePy 전용 ImageMagick 보안 정책 완화 (제공된 로직 반영)
RUN find /etc -name "policy.xml" -exec sed -i 's/rights="none" pattern="@\*"/rights="read|write" pattern="@*"/g' {} + && \
    find /etc -name "policy.xml" -exec sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/g' {} +

# 3. Python 라이브러리 설치
# Install CPU-only Torch and Audio first to avoid massive CUDA downloads (prevents OOM/Disk space errors)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 소스 복사
COPY . .

# 5. Streamlit 실행 설정
EXPOSE 8501
CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]

