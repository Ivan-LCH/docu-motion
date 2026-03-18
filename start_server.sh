#!/bin/bash
# DocuMotion Studio - Start Server (SSH에서 수동 실행용)
# 실행 후 즉시 리턴됩니다 (SSH 세션에 영향 없음)
set -e

echo "🎬 DocuMotion Studio 시작 중..."

# React 빌드
echo "📦 프론트엔드 빌드 중..."
cd /app/frontend && npm install && npm run build
cd /app

# 디렉토리 보장
mkdir -p /app/data /app/outputs /app/logs

# 실행 중인 uvicorn 중지 (포트 충돌 방지)
echo "🔍 기존 서버 프로세스 확인 및 정리..."
bash /app/stop_server.sh > /dev/null 2>&1 || true

# FastAPI 백그라운드 시작 (스크립트는 즉시 리턴)
export TTS_SERVER_URL="http://qwen_tts_server:8000"
export TTS_VOICE_NAME="myvoice"
echo "🚀 FastAPI 서버 시작 (포트 8000)..."
nohup uvicorn backend.main:app --host 0.0.0.0 --port 8000 \
    > /app/logs/server.log 2>&1 &
echo $! > /app/server.pid
echo "✅ 서버 시작 완료 (PID: $(cat /app/server.pid))"
echo "📄 로그 확인: tail -f /app/logs/server.log"
