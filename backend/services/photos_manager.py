"""
DocuMotion - Google Photos Picker API 연동
세션 기반 미디어 선택 및 원본 다운로드
(2025.04 이후 Library API readonly 스코프 폐지 → Picker API로 전환)
"""
import os
import json
import requests as http_requests

# Google OAuth가 기존 granted 스코프를 포함해서 반환할 때 스코프 불일치 에러 방지
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from backend.core.config import BASE_DIR
from backend.core.logger import get_logger

logger = get_logger(__name__)

# Picker API 스코프 (비제한 — 별도 심사 불필요)
PICKER_SCOPE = 'https://www.googleapis.com/auth/photospicker.mediaitems.readonly'

# YouTube + Picker 통합 스코프
ALL_SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.force-ssl',
    PICKER_SCOPE,
]

TOKEN_PATH = BASE_DIR / "token.json"
PICKER_API = "https://photospicker.googleapis.com/v1"


# ═══════════════════════════════════════════════
# 인증
# ═══════════════════════════════════════════════

def _get_credentials() -> Credentials | None:
    """토큰 로드 + 만료 시 자동 갱신. 스코프 제한 없이 로드."""
    if not TOKEN_PATH.exists():
        return None
    # 스코프 지정 없이 로드 → 기존 토큰의 스코프 그대로 사용
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            logger.info("Photos Picker: token refreshed")
        except Exception as e:
            logger.error(f"Photos Picker: token refresh failed: {e}")
            return None
    if not creds.valid:
        return None
    return creds


def _get_headers() -> dict:
    creds = _get_credentials()
    if not creds:
        raise PermissionError("Google Photos 인증이 필요합니다.")
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }


def check_auth() -> dict:
    """Picker API 인증 상태 확인 — 세션 생성 시도로 검증."""
    try:
        creds = _get_credentials()
        if not creds:
            return {"authenticated": False, "reason": "no_valid_token"}
        headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
        resp = http_requests.post(f"{PICKER_API}/sessions", headers=headers, json={}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            session_id = data.get("id", "")
            if session_id:
                try:
                    http_requests.delete(
                        f"{PICKER_API}/sessions/{session_id}",
                        headers={"Authorization": f"Bearer {creds.token}"},
                        timeout=5,
                    )
                except Exception:
                    pass
            return {"authenticated": True}
        elif resp.status_code in (401, 403):
            return {"authenticated": False, "reason": "scope_missing"}
        else:
            return {"authenticated": False, "reason": f"api_error_{resp.status_code}"}
    except Exception as e:
        return {"authenticated": False, "reason": str(e)}


def generate_auth_url(redirect_uri: str) -> str:
    """Picker 스코프만 요청 + 기존 권한 유지 (include_granted_scopes)."""
    from google_auth_oauthlib.flow import Flow
    client_config = _load_client_config()
    if not client_config:
        raise ValueError("OAuth client 정보를 찾을 수 없습니다.")
    # Picker 스코프만 요청 (non-restricted → 차단 안됨)
    flow = Flow.from_client_config(
        client_config,
        scopes=[PICKER_SCOPE],
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',  # 기존 YouTube 권한 유지
        prompt='consent',                # 항상 동의 화면 → refresh_token 강제 발급
    )
    return auth_url


def exchange_code(code: str, redirect_uri: str) -> bool:
    """Authorization code → token.json 저장.

    새 응답에 refresh_token이 없으면 기존 token.json의 refresh_token을 보존.
    """
    from google_auth_oauthlib.flow import Flow
    client_config = _load_client_config()
    if not client_config:
        raise ValueError("OAuth client 정보를 찾을 수 없습니다.")
    flow = Flow.from_client_config(
        client_config,
        scopes=[PICKER_SCOPE],
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)

    new_data = json.loads(flow.credentials.to_json())
    # refresh_token 보존: 새 토큰에 없으면 기존 token.json에서 가져오기
    if not new_data.get("refresh_token") and TOKEN_PATH.exists():
        try:
            old_data = json.loads(TOKEN_PATH.read_text())
            if old_data.get("refresh_token"):
                new_data["refresh_token"] = old_data["refresh_token"]
                logger.info("Photos Picker: preserved existing refresh_token")
        except Exception:
            pass

    TOKEN_PATH.write_text(json.dumps(new_data))
    has_refresh = "refresh_token" in new_data and new_data["refresh_token"]
    logger.info(f"Photos Picker: OAuth token saved (refresh_token={'yes' if has_refresh else 'NO'})")
    return True


def _load_client_config() -> dict | None:
    """OAuth client 설정 로드 — Flow.from_client_config에 직접 전달 가능한 형태."""
    # 1. client_secret.json (GCP에서 다운로드한 원본)
    secret_path = BASE_DIR / "client_secret.json"
    if secret_path.exists():
        try:
            data = json.loads(secret_path.read_text())
            if "web" in data or "installed" in data:
                return data  # {"web": {...}} 또는 {"installed": {...}} 그대로 반환
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


# ═══════════════════════════════════════════════
# Picker Session 관리
# ═══════════════════════════════════════════════

def create_session() -> dict:
    """
    Picker 세션 생성.
    Returns: { id, pickerUri, mediaItemsSet }
    """
    headers = _get_headers()
    resp = http_requests.post(f"{PICKER_API}/sessions", headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"Picker session created: {data.get('id', '')[:20]}...")
    return {
        "id": data["id"],
        "pickerUri": data["pickerUri"],
        "mediaItemsSet": data.get("mediaItemsSet", False),
    }


def poll_session(session_id: str) -> dict:
    """
    세션 폴링 — 사용자가 선택 완료했는지 확인.
    Returns: { id, pickerUri, mediaItemsSet }
    """
    headers = _get_headers()
    resp = http_requests.get(f"{PICKER_API}/sessions/{session_id}", headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return {
        "id": data["id"],
        "pickerUri": data.get("pickerUri", ""),
        "mediaItemsSet": data.get("mediaItemsSet", False),
    }


def list_picked_items(session_id: str, sort_order: str = "selected") -> list[dict]:
    """
    선택 완료된 세션에서 미디어 아이템 목록 조회.

    sort_order:
        - "selected"   : 사용자가 picker 상에서 선택한 순서 (item.createTime 오름차순)
        - "oldest"     : 촬영 시각 오래된 순 (mediaFileMetadata.creationTime 오름차순)
        - "newest"     : 촬영 시각 최신 순 (mediaFileMetadata.creationTime 내림차순)
        - "api"        : Picker API 응답 순서 그대로 (기본 fallback)
    """
    headers = _get_headers()
    items = []
    page_token = None

    while True:
        params = {"sessionId": session_id}
        if page_token:
            params["pageToken"] = page_token

        resp = http_requests.get(f"{PICKER_API}/mediaItems", headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("mediaItems", []):
            media_file = item.get("mediaFile", {})
            metadata = media_file.get("mediaFileMetadata", {}) or {}
            items.append({
                "id": item.get("id", ""),
                "baseUrl": media_file.get("baseUrl", ""),
                "mimeType": media_file.get("mimeType", ""),
                "filename": media_file.get("filename", ""),
                "isVideo": media_file.get("mimeType", "").startswith("video/"),
                # 정렬용 필드 (둘 다 RFC3339 ISO 문자열, 사전순 = 시간순)
                "selectedTime": item.get("createTime", ""),         # picker 상에서 선택된 시각
                "creationTime": metadata.get("creationTime", ""),   # 사진 원본 촬영 시각
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    # 정렬 적용 — 빈 문자열은 끝으로 보내기
    if sort_order == "selected":
        items.sort(key=lambda i: (i["selectedTime"] == "", i["selectedTime"]))
    elif sort_order == "oldest":
        items.sort(key=lambda i: (i["creationTime"] == "", i["creationTime"]))
    elif sort_order == "newest":
        items.sort(key=lambda i: (i["creationTime"] == "", i["creationTime"]), reverse=True)
    # "api"는 정렬 안 함

    logger.info(f"Picker session {session_id[:12]}...: {len(items)} items, sort={sort_order}")
    return items


def delete_session(session_id: str) -> None:
    """세션 삭제 (정리)."""
    try:
        headers = _get_headers()
        http_requests.delete(f"{PICKER_API}/sessions/{session_id}", headers=headers, timeout=5)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 미디어 다운로드
# ═══════════════════════════════════════════════

def download_item(base_url: str, mime_type: str, dest_path: str) -> str:
    """
    Picker에서 받은 baseUrl로 원본 다운로드.
    사진: baseUrl + =d, 동영상: baseUrl + =dv
    Authorization 헤더 필수.
    """
    headers = _get_headers()
    is_video = mime_type.startswith("video/")
    download_url = base_url + ("=dv" if is_video else "=d")

    with http_requests.get(download_url, headers=headers, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

    logger.info(f"Picker: downloaded -> {dest_path}")
    return dest_path
