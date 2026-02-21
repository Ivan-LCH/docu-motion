# ─────────────────────────────────────────────────────────────────────────── #
# DocuMotion Studio - Dockerfile
# Python 3.11 + Node.js 20 + FFmpeg
# ─────────────────────────────────────────────────────────────────────────── #

FROM python:3.11-slim

# 시스템 패키지 설치 (Node.js + FFmpeg + SSH)
RUN apt-get update && apt-get install -y \
    openssh-server \
    curl \
    git \
    vim \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    imagemagick \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ImageMagick 정책 수정 (MoviePy TextClip 자막 생성에 필수)
RUN sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-7/policy.xml 2>/dev/null || true
RUN sed -i 's/<policy domain="path" rights="none" pattern="@\*"\/>/<policy domain="path" rights="read|write" pattern="@*"\/>/' /etc/ImageMagick-7/policy.xml 2>/dev/null || true
RUN sed -i 's/<policy domain="coder" rights="none" pattern="TEXT"\/>/<policy domain="coder" rights="read|write" pattern="TEXT"\/>/' /etc/ImageMagick-7/policy.xml 2>/dev/null || true
RUN sed -i 's/<policy domain="coder" rights="none" pattern="LABEL"\/>/<policy domain="coder" rights="read|write" pattern="LABEL"\/>/' /etc/ImageMagick-7/policy.xml 2>/dev/null || true

# SSH 설정
RUN mkdir -p /var/run/sshd
RUN echo 'root:password' | chpasswd
RUN sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config

# 작업 디렉토리
WORKDIR /app

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 프론트엔드 의존성 설치 및 빌드
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm install

# 소스 코드 복사
COPY . .

# React 빌드
RUN cd frontend && npm run build

# 디렉토리 생성
RUN mkdir -p /app/data /app/outputs /app/logs /app/resources

# 실행 스크립트 권한
RUN chmod +x /app/start_server.sh /app/stop_server.sh 2>/dev/null || true

# 포트: 8000(FastAPI), 22(SSH)
EXPOSE 8000 22

# 기본: SSH + FastAPI 동시 실행
CMD ["/bin/bash", "-c", "/usr/sbin/sshd && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
