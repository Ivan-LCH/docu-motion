"""
DocuMotion - YouTube 인증 · 업로드 (YouTube Data API v3)

token.json 은 포토·드라이브·YouTube 가 같이 쓰는 통합 토큰이다. photos_manager 는 저장된
"scopes" 목록으로 갱신하기 때문에 access token 이 Picker 스코프로 좁혀질 수 있다.
→ YouTube 경로는 저장된 access token 을 쓰지 않고, refresh_token 을 스코프 지정 없이 갱신한다
  (스코프 없이 갱신하면 refresh_token 에 부여된 모든 스코프가 담긴 access token 이 나온다).
→ YouTube 경로는 token.json 을 절대 쓰지 않는다 (인증 코드 교환 exchange_code 만 예외).

오류 (PermissionError 메시지):
  no_token        token.json / refresh_token 없음
  reauth_required refresh_token 만료·취소 (invalid_grant)
  scope_missing   youtube.upload 스코프가 부여되지 않음
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

# Google OAuth가 기존 granted 스코프를 포함해서 반환할 때 스코프 불일치 에러 방지
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

import googleapiclient.discovery
import requests as http_requests
from google.auth import exceptions as google_exceptions
from google.auth.transport.requests import Request
from google.oauth2 import reauth
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload

from backend.core.config import BASE_DIR
from backend.core.logger import get_logger
from backend.services import photos_manager

logger = get_logger(__name__)

# ═══════════════════════════════════════════════
# 상수
# ═══════════════════════════════════════════════
UPLOAD_SCOPE = 'https://www.googleapis.com/auth/youtube.upload'
YOUTUBE_SCOPES = [
    UPLOAD_SCOPE,
    'https://www.googleapis.com/auth/youtube.force-ssl',
]
# 인증 URL: YouTube + 포토 Picker 를 함께 요청 (통합 토큰이라 포토도 계속 동작해야 함)
AUTH_SCOPES = YOUTUBE_SCOPES + [photos_manager.PICKER_SCOPE]
SCOPES = YOUTUBE_SCOPES          # 레거시 이름

TOKEN_PATH = BASE_DIR / "token.json"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

PRIVACY_VALUES = ("private", "unlisted", "public")
TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAG_MAX = 30
TAGS_TOTAL_MAX = 500
CHUNK_SIZE = 8 * 1024 * 1024
CHANNEL_TTL = 600                # 채널 이름 캐시 (초)
EXPIRY_MARGIN = timedelta(minutes=5)

_lock = threading.Lock()
_cache: dict = {}                # {"key", "creds", "scopes"}
_channel_cache: dict = {}        # {"key", "title", "at"}


# ═══════════════════════════════════════════════
# Credentials
# ═══════════════════════════════════════════════
def _token_key() -> Optional[tuple]:
    try:
        st = TOKEN_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_token() -> dict:
    """token.json 읽기 (읽기 전용). client_id/secret 이 없으면 client_secret.json 에서 보충."""
    try:
        data = json.loads(TOKEN_PATH.read_text())
    except Exception:
        raise PermissionError("no_token")
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise PermissionError("no_token")
    if not (data.get("client_id") and data.get("client_secret")):
        cfg = photos_manager._load_client_config() or {}
        section = cfg.get("web") or cfg.get("installed") or {}
        data.setdefault("client_id", section.get("client_id"))
        data.setdefault("client_secret", section.get("client_secret"))
    if not (data.get("client_id") and data.get("client_secret")):
        raise PermissionError("no_token")
    return data


def _tokeninfo_scopes(access_token: str) -> set:
    try:
        r = http_requests.get(TOKENINFO_URL, params={"access_token": access_token}, timeout=10)
        if r.status_code != 200:
            return set()
        return set(str(r.json().get("scope") or "").split())
    except Exception as e:
        logger.warning(f"YouTube: tokeninfo 실패: {e}")
        return set()


def _unscoped_refresh(data: dict) -> tuple[Credentials, set]:
    """refresh_token 을 스코프 지정 없이 갱신 → (메모리 전용 Credentials, 부여된 스코프)."""
    token_uri = data.get("token_uri") or DEFAULT_TOKEN_URI
    try:
        access_token, refresh_token, expiry, grant, _rapt = reauth.refresh_grant(
            Request(), token_uri, data["refresh_token"], data["client_id"], data["client_secret"],
            scopes=None)
    except google_exceptions.RefreshError as e:
        if "invalid_grant" in str(e):
            raise PermissionError("reauth_required")
        raise
    # Credentials.refresh() 는 scopes=None 일 때 granted_scopes 를 채우지 않아서 응답을 직접 본다
    scopes = set(str((grant or {}).get("scope") or "").split()) or _tokeninfo_scopes(access_token)
    creds = Credentials(token=access_token, refresh_token=refresh_token or data["refresh_token"],
                        client_id=data["client_id"], client_secret=data["client_secret"],
                        token_uri=token_uri, scopes=None, expiry=expiry)
    return creds, scopes


def _youtube_credentials() -> Credentials:
    """YouTube 용 Credentials. 만료 직전까지 메모리 캐시. token.json 은 읽기만 한다."""
    key = _token_key()
    if key is None:
        raise PermissionError("no_token")
    with _lock:
        hit = _cache if _cache.get("key") == key else None
        if hit:
            creds, scopes = hit["creds"], hit["scopes"]
            exp = creds.expiry
            if exp is None or exp - EXPIRY_MARGIN > datetime.utcnow():
                if UPLOAD_SCOPE not in scopes:
                    raise PermissionError("scope_missing")
                return creds
        creds, scopes = _unscoped_refresh(_read_token())
        _cache.clear()
        _cache.update({"key": key, "creds": creds, "scopes": scopes})
    if UPLOAD_SCOPE not in scopes:
        logger.warning("YouTube: refresh_token 에 youtube.upload 스코프 없음")
        raise PermissionError("scope_missing")
    return creds


def _cached_scopes() -> set:
    with _lock:
        return set(_cache.get("scopes") or ())


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _channel_cache.clear()


def _build_service(creds: Credentials):
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds, cache_discovery=False)


def _channel_title(creds: Credentials) -> Optional[str]:
    key = creds.refresh_token
    now = time.time()
    with _lock:
        if _channel_cache.get("key") == key and now - _channel_cache.get("at", 0) < CHANNEL_TTL:
            return _channel_cache.get("title")
    title = None
    try:
        resp = _build_service(creds).channels().list(part="snippet", mine=True).execute()
        items = resp.get("items") or []
        if items:
            title = (items[0].get("snippet") or {}).get("title")
    except Exception as e:
        logger.warning(f"YouTube: 채널 조회 실패: {e}")
        return None                       # 실패는 캐시하지 않음
    with _lock:
        _channel_cache.update({"key": key, "title": title, "at": now})
    return title


def check_ready() -> None:
    """업로드 가능 여부. 불가하면 PermissionError(reason)."""
    _youtube_credentials()


def get_auth_status() -> dict:
    """{authenticated, reason, scopes_ok, channel}. 파일을 쓰지 않는다."""
    try:
        creds = _youtube_credentials()
    except PermissionError as e:
        return {"authenticated": False, "reason": str(e), "scopes_ok": False, "channel": None}
    except Exception as e:
        logger.warning(f"YouTube: 인증 확인 실패: {e}")
        return {"authenticated": False, "reason": "error", "scopes_ok": False, "channel": None}
    return {"authenticated": True, "reason": None, "scopes_ok": True, "channel": _channel_title(creds)}


def generate_auth_url(redirect_uri: str) -> str:
    """YouTube + 포토 스코프 인증 URL (prompt=consent, include_granted_scopes)."""
    return photos_manager.generate_auth_url(redirect_uri, scopes=AUTH_SCOPES)


def exchange_code(code: str, redirect_uri: str) -> bool:
    """인증 코드 → token.json (기존 refresh_token 보존은 photos_manager 가 처리)."""
    ok = photos_manager.exchange_code(code, redirect_uri, scopes=AUTH_SCOPES)
    reset_cache()
    return ok


def _load_client_config() -> dict | None:
    return photos_manager._load_client_config()


# ═══════════════════════════════════════════════
# 메타데이터 정리
# ═══════════════════════════════════════════════
def _strip_angle(s) -> str:
    return str(s or "").replace("<", "").replace(">", "")


def clean_title(title, fallback: str = "DocuMotion") -> str:
    t = " ".join(_strip_angle(title).split())[:TITLE_MAX].strip()
    if not t:
        t = " ".join(_strip_angle(fallback).split())[:TITLE_MAX].strip() or "DocuMotion"
    return t


def clean_description(desc) -> str:
    d = _strip_angle(desc).replace("\r\n", "\n").strip()
    raw = d.encode("utf-8")
    if len(raw) > DESCRIPTION_MAX_BYTES:
        d = raw[:DESCRIPTION_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
    return d


def tag_cost(tag: str) -> int:
    """YouTube 태그 길이 규칙: 공백이 있는 태그는 따옴표 2자를 더 센다."""
    return len(tag) + (2 if " " in tag else 0)


def clean_tags(tags, limit: int = TAGS_TOTAL_MAX) -> list[str]:
    """중복·빈 태그 제거, 태그당 30자, 전체 500자(쉼표 포함) 넘는 뒤쪽 태그는 버린다."""
    if tags is None:
        items = []
    elif isinstance(tags, str):
        items = tags.split(",")
    else:
        items = list(tags)
    out: list[str] = []
    seen: set = set()
    total = 0
    for t in items:
        t = " ".join(_strip_angle(t).replace(",", " ").split())[:TAG_MAX].strip()
        if not t or t.lower() in seen:
            continue
        cost = tag_cost(t) + (1 if out else 0)
        if total + cost > limit:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def tags_length(tags: list[str]) -> int:
    return sum(tag_cost(t) for t in tags) + max(0, len(tags) - 1)


# ═══════════════════════════════════════════════
# 업로드
# ═══════════════════════════════════════════════
def upload_video(file_path, title, description, tags=None, privacy: str = 'private',
                 thumbnail_path=None, progress_cb: Optional[Callable[[float], None]] = None,
                 category_id: str = '22', fallback_title: str = "DocuMotion") -> dict:
    """영상 업로드 (resumable, 8MB 청크). 반환: {video_id, url, thumbnail_ok, warning}.
    썸네일 실패는 치명적이지 않다 (채널 미인증 등 → warning)."""
    if privacy not in PRIVACY_VALUES:
        raise ValueError(f"invalid privacy: {privacy}")
    creds = _youtube_credentials()
    youtube = _build_service(creds)

    body = {
        'snippet': {
            'title': clean_title(title, fallback_title),
            'description': clean_description(description),
            'tags': clean_tags(tags),
            'categoryId': str(category_id),
            'defaultLanguage': 'ko',
            'defaultAudioLanguage': 'ko',
        },
        'status': {
            'privacyStatus': privacy,
            'selfDeclaredMadeForKids': False,
        },
    }
    logger.info(f"YouTube upload started: {body['snippet']['title']} ({privacy})")

    media = MediaFileUpload(str(file_path), chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_cb:
            progress_cb(round(status.progress() * 100, 1))
    if progress_cb:
        progress_cb(100.0)

    video_id = response['id']
    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info(f"YouTube upload complete: {url}")

    thumbnail_ok, warning = False, None
    if thumbnail_path:
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            thumbnail_ok = True
        except Exception as e:
            logger.warning(f"YouTube thumbnail failed: {e}")
            warning = "썸네일은 적용하지 못했어요 (채널 인증이 필요할 수 있어요)"
    return {"video_id": video_id, "url": url, "thumbnail_ok": thumbnail_ok, "warning": warning}


def upload_short(file_path, title, description, tags=None, category_id='22', privacy='private'):
    """레거시 호환: 업로드 후 URL 만 반환."""
    return upload_video(file_path, title, description, tags=tags, privacy=privacy,
                        category_id=category_id)["url"]
