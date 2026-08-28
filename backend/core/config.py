"""
DocuMotion - App Configuration
환경변수 기반 설정 관리
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

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
GOOGLE_API_KEY   = os.getenv("GOOGLE_API_KEY", "")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")
DEBUG            = os.getenv("DEBUG", "false").lower() == "true"

# ─────────────────────────────────────────────
# 네이버 지역검색 (장소 실데이터) + Gemini (장소 설명 생성)
# ─────────────────────────────────────────────
NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
NAVER_LOCAL_BASE    = os.getenv("NAVER_LOCAL_BASE", "https://openapi.naver.com/v1/search/local.json")
GEMINI_MODEL        = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ─────────────────────────────────────────────
# 지도 (Route/Place 슬라이드)
# - Geocoding 1순위: 카카오 Local API (한국 POI 강점, 무료)
# - Geocoding 폴백 / 라우팅 / 타일: 무료 OSM 계열
# ─────────────────────────────────────────────
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_LOCAL_BASE   = os.getenv("KAKAO_LOCAL_BASE", "https://dapi.kakao.com/v2/local")
OSRM_BASE        = os.getenv("OSRM_BASE", "https://router.project-osrm.org")
NOMINATIM_BASE   = os.getenv("NOMINATIM_BASE", "https://nominatim.openstreetmap.org")
OVERPASS_BASE    = os.getenv("OVERPASS_BASE", "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
MAP_USER_AGENT   = os.getenv("MAP_USER_AGENT", "DocuMotionStudio/1.0")

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
# 자막용 굵은 폰트 (작은 크기에서도 가독성 확보). 파일 없으면 기본 폰트로 폴백.
_bold = RESOURCES_DIR / "NanumGothic-ExtraBold.ttf"
FONT_BOLD_PATH = str(_bold) if _bold.exists() else FONT_PATH
FONT_SIZE   = 30
