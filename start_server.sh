#!/bin/bash
# DocuMotion Studio - Start Server
set -e

echo "🎬 DocuMotion Studio 시작 중..."

# React 빌드 (무조건 빌드 진행. 이미 설치된 노드 모듈 활용)
echo "📦 프론트엔드 빌드 중..."
cd /app/frontend && npm install && npm run build
cd /app

# 디렉토리 보장
mkdir -p /app/data /app/outputs /app/logs

# SSH 시작 (이미 실행 중이면 무시)
/usr/sbin/sshd 2>/dev/null || true

# 실행 중인 FastAPI 중지 (포트 8000 충돌 방지)
echo "🔍 기존 서버 프로세스 확인 및 정리..."
bash /app/stop_server.sh >/dev/null 2>&1 || true

# FastAPI 시작
echo "🚀 FastAPI 서버 시작 (포트 8000)..."
export TTS_SERVER_URL="http://qwen_tts_server:8000"
export TTS_VOICE_NAME="myvoice"
nohup uvicorn backend.main:app --host 0.0.0.0 --port 8000 > /app/logs/server.log 2>&1 &
echo $! > /app/server.pid
echo "✅ 서버 시작 완료 (PID: $(cat /app/server.pid))"
echo "📄 로그 파일 위치: /app/logs/server.log"
echo "👀 실시간 오류 로그를 확인합니다 (Ctrl+C를 누르면 로그 보기만 종료되며 서버는 계속 실행됩니다)."

trap 'echo "로그 보기가 종료되었습니다. 서버는 백그라운드에서 계속 실행 중입니다."; exit 0' INT

tail -f /app/logs/server.log
