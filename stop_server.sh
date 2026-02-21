#!/bin/bash
# DocuMotion Studio - Stop Server
if [ -f /app/server.pid ]; then
    PID=$(cat /app/server.pid)
    kill $PID 2>/dev/null && echo "✅ 서버 종료 신호 전송 (PID: $PID)" || echo "⚠️ PID 프로세스 없음"
    # Wait for process to exit
    for i in {1..20}; do
        if ! kill -0 $PID 2>/dev/null; then break; fi
        sleep 0.5
    done
    if kill -0 $PID 2>/dev/null; then
        kill -9 $PID 2>/dev/null
        echo "🚨 서버 강제 종료 (SIGKILL)"
    fi
    rm -f /app/server.pid
else
    # Find uvicorn and kill it safely
    UVICORN_PID=$(pgrep -f "uvicorn backend.main:app")
    if [ ! -z "$UVICORN_PID" ]; then
        kill $UVICORN_PID 2>/dev/null
        echo "✅ 서버 종료 신호 전송 (PID: $UVICORN_PID)"
        for i in {1..20}; do
            if ! kill -0 $UVICORN_PID 2>/dev/null; then break; fi
            sleep 0.5
        done
        if kill -0 $UVICORN_PID 2>/dev/null; then
            kill -9 $UVICORN_PID 2>/dev/null
            echo "🚨 서버 강제 종료 (SIGKILL)"
        fi
    else
        echo "⚠️ 실행 중인 서버 없음"
    fi
fi
