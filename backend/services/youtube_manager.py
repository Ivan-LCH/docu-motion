# ===================================================================================================================
# Import
# ===================================================================================================================

import os
import json
import googleapiclient.discovery
import googleapiclient.errors

# Google OAuth가 기존 granted 스코프를 포함해서 반환할 때 스코프 불일치 에러 방지
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload

from backend.core.config import BASE_DIR
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ===================================================================================================================
# Global Variables
# ===================================================================================================================

# YouTube 인증 범위
YOUTUBE_SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]
# token.json 로드 시 전체 스코프 (유효성 검사용)
SCOPES = YOUTUBE_SCOPES

TOKEN_PATH = BASE_DIR / "token.json"


# ===================================================================================================================
# Credentials helper
# ===================================================================================================================

def _get_credentials() -> Credentials | None:
    """토큰 로드 + 만료 시 자동 갱신. 실패 시 None 반환."""
    if not TOKEN_PATH.exists():
        logger.warning("token.json not found")
        return None

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))

    # Access Token 만료 → refresh_token으로 자동 갱신 (4-1-1)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # 갱신된 토큰 저장
            TOKEN_PATH.write_text(creds.to_json())
            logger.info("Access token refreshed successfully")
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return None

    # refresh_token 자체가 없거나 유효하지 않으면 None
    if not creds.valid:
        logger.warning("Credentials invalid and could not be refreshed")
        return None

    return creds


def get_auth_status() -> dict:
    """현재 인증 상태 반환."""
    if not TOKEN_PATH.exists():
        return {"authenticated": False, "reason": "no_token"}

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if creds.valid:
            return {"authenticated": True}
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return {"authenticated": True}
        return {"authenticated": False, "reason": "token_invalid"}
    except Exception as e:
        return {"authenticated": False, "reason": str(e)}


def generate_auth_url(redirect_uri: str) -> str:
    """OAuth2 인증 URL 생성 (4-1-2)."""
    from google_auth_oauthlib.flow import Flow

    # client_secret 정보를 token.json에서 추출 (client_id, client_secret 포함)
    client_config = _load_client_config()
    if not client_config:
        raise ValueError("Cannot build OAuth flow: no client credentials found")

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',  # 기존 Photos 권한 유지
    )
    return auth_url


def exchange_code(code: str, redirect_uri: str) -> bool:
    """Authorization code → token.json 갱신 (4-1-3)."""
    from google_auth_oauthlib.flow import Flow

    client_config = _load_client_config()
    if not client_config:
        raise ValueError("Cannot build OAuth flow: no client credentials found")

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    TOKEN_PATH.write_text(creds.to_json())
    logger.info("OAuth token exchanged and saved")
    return True


def _load_client_config() -> dict | None:
    """OAuth client 설정 로드 — Flow.from_client_config에 직접 전달 가능한 형태."""
    # 1. client_secret.json (GCP에서 다운로드한 원본)
    secret_path = BASE_DIR / "client_secret.json"
    if secret_path.exists():
        try:
            data = json.loads(secret_path.read_text())
            if "web" in data or "installed" in data:
                return data
        except Exception:
            pass
    # 2. token.json에서 client_id/secret 추출 (레거시 호환)
    if TOKEN_PATH.exists():
        try:
            data = json.loads(TOKEN_PATH.read_text())
            if data.get("client_id") and data.get("client_secret"):
                return {"web": {
                    "client_id": data["client_id"],
                    "client_secret": data["client_secret"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }}
        except Exception:
            pass

    return None


# ===================================================================================================================
# upload shorts
# ===================================================================================================================

def upload_short(file_path, title, description, tags=None, category_id='28'):
    logger.info(f"YouTube upload started: {title}")

    creds = _get_credentials()
    if not creds:
        raise PermissionError("YouTube 인증이 필요합니다. 재인증을 진행해주세요.")

    youtube = googleapiclient.discovery.build("youtube", "v3", credentials=creds)

    # 설명 안전장치
    safe_description = description.replace("<", "[").replace(">", "]")
    if len(safe_description) > 4500:
        logger.warning(f"Description truncated: {len(safe_description)} -> 4500 chars")
        safe_description = safe_description[:4500] + "\n\n...(내용이 길어 생략되었습니다.)"

    # 태그 처리
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]

    body = {
        'snippet': {
            'title': title[:100],
            'description': safe_description,
            'tags': tags,
            'categoryId': category_id,
            'defaultLanguage': 'ko',
            'defaultAudioLanguage': 'ko'
        },
        'status': {
            'privacyStatus': 'public',
            'selfDeclaredMadeForKids': False
        }
    }

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"Upload progress: {int(status.progress() * 100)}%")

        video_id = response['id']
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info(f"Upload complete: {video_url}")
        return video_url

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise e


# ===================================================================================================================
# End of program
# ===================================================================================================================
