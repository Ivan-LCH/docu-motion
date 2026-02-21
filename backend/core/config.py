"""
DocuMotion - App Configuration
환경변수 기반 설정 관리
"""
import os
from pathlib import Path

# ─────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent.parent  # /app
DATA_DIR    = BASE_DIR / "data"
OUTPUTS_DIR = BASE_DIR / "outputs"
LOGS_DIR    = BASE_DIR / "logs"
RESOURCES_DIR = BASE_DIR / "resources"

# ─────────────────────────────────────────────
# 환경변수
# ─────────────────────────────────────────────
DATABASE_URL     = f"sqlite:///{DATA_DIR}/docu_motion.db"
TTS_SERVER_URL   = os.getenv("TTS_SERVER_URL", "http://localhost:8002")
TTS_VOICE_NAME   = os.getenv("TTS_VOICE_NAME", "myvoice")
YOUTUBE_TOKEN    = os.getenv("YOUTUBE_TOKEN_JSON", "")
DEBUG            = os.getenv("DEBUG", "false").lower() == "true"

# ─────────────────────────────────────────────
# 앱 정보
# ─────────────────────────────────────────────
APP_NAME = "DocuMotion Studio"
VERSION  = "5.0.0 (FastAPI)"

# ─────────────────────────────────────────────
# 렌더링 설정
# ─────────────────────────────────────────────
CANVAS_SIZE = (1280, 720)
FONT_PATH   = str(RESOURCES_DIR / "font.ttf")
FONT_SIZE   = 28
