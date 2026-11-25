"""
DocuMotion - Google 드라이브 가져오기 (Google Picker + Drive API v3, drive.file 스코프)

drive.file 은 비제한(non-restricted) 스코프다: 사용자가 Picker 에서 고른 파일에만 접근할 수 있다.
OAuth 클라이언트/토큰은 Google 포토 Picker 와 같은 client_secret.json · token.json 을 쓴다.
인증 URL 은 photospicker + drive.file 을 함께 요청하고 include_granted_scopes 로 기존
YouTube/포토 권한을 유지한다. refresh_token 보존은 photos_manager.exchange_code 를 그대로 쓴다.

⚠ GET /drive/token 은 브라우저 Picker 에 access token 을 넘긴다. token.json 의 토큰은
   통합 토큰이라 YouTube/포토 스코프도 함께 담겨 있다 — Tailscale 내부 개인용 앱 전제.
   (공개 배포 시에는 drive.file 전용 토큰을 따로 발급해야 한다.)

환경변수:
  GOOGLE_PICKER_API_KEY       Picker 용 브라우저 API 키 (Picker API 로 제한 + HTTP referrer 제한)
  GOOGLE_CLOUD_PROJECT_NUMBER Picker setAppId (없으면 OAuth client_id 앞 숫자 = 프로젝트 번호)
"""
from __future__ import annotations

import os
import threading
from typing import Optional

import requests as http_requests

from backend.core.logger import get_logger
from backend.services import photos_manager

logger = get_logger(__name__)

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_SCOPES = [photos_manager.PICKER_SCOPE, DRIVE_FILE_SCOPE]
DRIVE_API = "https://www.googleapis.com/drive/v3"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
MAX_DOWNLOAD_BYTES = 2 * 1024 ** 3            # 파일당 2GB 상한
CHUNK = 1024 * 1024
FILE_FIELDS = ("id,name,mimeType,size,createdTime,thumbnailLink,"
               "imageMediaMetadata(time,location),videoMediaMetadata(durationMillis)")

_scope_cache: dict[str, set] = {}
_scope_lock = threading.Lock()


class DriveError(Exception):
    """사용자에게 보여줄 수 있는 드라이브 오류 (status: HTTP 코드)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ═══════════════════════════════════════════════
# 설정 / 인증
# ═══════════════════════════════════════════════
def _client_id() -> str:
    cfg = photos_manager._load_client_config() or {}
    section = cfg.get("web") or cfg.get("installed") or {}
    return str(section.get("client_id") or "")


def _app_id(client_id: str) -> str:
    env = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    if env:
        return env
    head = client_id.split("-", 1)[0]
    return head if head.isdigit() else ""


def token_scopes(access_token: str) -> set:
    """access token 에 실제로 부여된 스코프 (tokeninfo). 실패 시 빈 집합. 토큰별 캐시."""
    with _scope_lock:
        hit = _scope_cache.get(access_token)
    if hit is not None:
        return hit
    try:
        r = http_requests.get(TOKENINFO_URL, params={"access_token": access_token}, timeout=10)
        if r.status_code != 200:
            return set()
        scopes = set(str(r.json().get("scope") or "").split())
    except Exception as e:
        logger.warning(f"Drive: tokeninfo 실패: {e}")
        return set()
    with _scope_lock:
        if len(_scope_cache) > 32:
            _scope_cache.clear()
        _scope_cache[access_token] = scopes
    return scopes


def _drive_credentials():
    """drive.file 이 부여된 유효 자격 증명. 없으면 DriveError(401)."""
    creds = photos_manager._get_credentials()        # 만료 시 refresh + token.json 저장
    if not creds or not creds.token:
        raise DriveError("Google 인증이 필요합니다 (드라이브)", 401)
    if DRIVE_FILE_SCOPE not in token_scopes(creds.token):
        raise DriveError("드라이브 권한(drive.file)이 없습니다. 다시 인증해 주세요", 401)
    return creds


def get_config() -> dict:
    """프론트 Picker 설정. enabled=false 면 reason 에 이유 (관리자 설정 필요)."""
    client_id = _client_id()
    api_key = os.getenv("GOOGLE_PICKER_API_KEY", "").strip()
    app_id = _app_id(client_id)
    missing = []
    if not client_id:
        missing.append("OAuth 클라이언트(client_secret.json)")
    if not api_key:
        missing.append("GOOGLE_PICKER_API_KEY")
    if not app_id:
        missing.append("GOOGLE_CLOUD_PROJECT_NUMBER")
    out = {"enabled": not missing, "client_id": client_id, "api_key": api_key, "app_id": app_id,
           "authenticated": False, "reason": ""}
    if missing:
        out["reason"] = "환경 설정 없음: " + ", ".join(missing)
        return out
    try:
        _drive_credentials()
        out["authenticated"] = True
    except DriveError as e:
        out["reason"] = str(e)
    return out


def get_access_token() -> dict:
    """Picker setOAuthToken 용 단기 access token (drive.file 포함 확인 후)."""
    creds = _drive_credentials()
    exp = getattr(creds, "expiry", None)
    return {"access_token": creds.token,
            "expires_at": int(exp.timestamp()) if exp else None}


def generate_auth_url(redirect_uri: str) -> str:
    return photos_manager.generate_auth_url(redirect_uri, scopes=AUTH_SCOPES)


def exchange_code(code: str, redirect_uri: str) -> bool:
    ok = photos_manager.exchange_code(code, redirect_uri, scopes=AUTH_SCOPES)
    with _scope_lock:
        _scope_cache.clear()
    return ok


# ═══════════════════════════════════════════════
# 파일 메타 / 다운로드
# ═══════════════════════════════════════════════
def _headers() -> dict:
    return {"Authorization": f"Bearer {_drive_credentials().token}"}


def get_file_meta(file_id: str) -> dict:
    r = http_requests.get(f"{DRIVE_API}/files/{file_id}",
                          params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
                          headers=_headers(), timeout=15)
    if r.status_code == 404:
        raise DriveError("파일을 찾을 수 없거나 접근 권한이 없습니다", 404)
    if r.status_code != 200:
        raise DriveError(f"드라이브 API 오류 ({r.status_code})", 502)
    return r.json()


def _stream_to(resp, dest_path: str, max_bytes: int) -> int:
    written = 0
    try:
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise DriveError(f"파일이 너무 큽니다 (최대 {max_bytes // 1024 ** 3}GB)", 413)
                f.write(chunk)
    except BaseException:
        try:
            os.unlink(dest_path)
        except OSError:
            pass
        raise
    return written


def download_file(file_id: str, dest_path: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
    """files.get?alt=media → dest_path 로 청크 스트리밍 저장. 반환: 바이트 수."""
    with http_requests.get(f"{DRIVE_API}/files/{file_id}",
                           params={"alt": "media", "supportsAllDrives": "true"},
                           headers=_headers(), stream=True, timeout=(15, 300)) as r:
        if r.status_code != 200:
            raise DriveError(f"다운로드 실패 ({r.status_code})", 502)
        n = _stream_to(r, dest_path, max_bytes)
    logger.info(f"Drive: downloaded {file_id} → {dest_path} ({n} bytes)")
    return n


def download_thumbnail(meta: dict, dest_path: str, size: int = 2560) -> bool:
    """HEIC 변환 실패 시 폴백: 드라이브가 만든 JPEG 썸네일을 큰 크기(=s2560)로 받는다."""
    link = str(meta.get("thumbnailLink") or "")
    if not link:
        return False
    url = link.rsplit("=s", 1)[0] + f"=s{size}" if "=s" in link else link
    try:
        with http_requests.get(url, headers=_headers(), stream=True, timeout=(15, 120)) as r:
            if r.status_code != 200:
                return False
            return _stream_to(r, dest_path, 64 * 1024 ** 2) > 1000
    except Exception as e:
        logger.warning(f"Drive: 썸네일 폴백 실패: {e}")
        return False


def captured_hint(meta: dict) -> Optional[str]:
    """imageMediaMetadata.time ('2026:10:25 10:30:00') → ISO. 없으면 None."""
    t = str(((meta.get("imageMediaMetadata") or {}).get("time")) or "").strip()
    if len(t) >= 19 and t[4] == ":" and t[7] == ":":
        return f"{t[0:4]}-{t[5:7]}-{t[8:10]}T{t[11:19]}"
    return None


def gps_hint(meta: dict) -> Optional[dict]:
    loc = (meta.get("imageMediaMetadata") or {}).get("location") or {}
    try:
        if loc.get("latitude") is not None and loc.get("longitude") is not None:
            return {"lat": float(loc["latitude"]), "lng": float(loc["longitude"])}
    except (TypeError, ValueError):
        pass
    return None
