"""
DocuMotion - YouTube 업로드 백그라운드 작업

상태: outputs/{id}/youtube_upload.json
  {status: idle|queued|uploading|processing|done|error, progress, url, video_id, privacy,
   error, warning, started_at, finished_at}
메타(생성/수정본): outputs/{id}/youtube_meta.json
썸네일: outputs/{id}/yt_thumb.jpg (사용자 파일), yt_thumb_slide.jpg (슬라이드에서 만든 것)
"""
from __future__ import annotations

import io
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from backend.core import config
from backend.core.logger import get_logger
from backend.services import youtube_manager

logger = get_logger(__name__)

STATE_FILE = "youtube_upload.json"
META_FILE = "youtube_meta.json"
THUMB_FILE = "yt_thumb.jpg"
SLIDE_THUMB_FILE = "yt_thumb_slide.jpg"
THUMB_SIZE = (1280, 720)
THUMB_MAX_BYTES = 2 * 1024 * 1024
ACTIVE = ("queued", "uploading", "processing")

_lock = threading.Lock()
_active: set[str] = set()        # 이 프로세스에서 진행 중인 프로젝트


def _dir(project_id: str) -> Path:
    return Path(config.OUTPUTS_DIR) / project_id


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> Optional[dict]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ────────────────────────────────────────────────────────────────────────── #
# 상태 / 메타
# ────────────────────────────────────────────────────────────────────────── #
def idle_state() -> dict:
    return {"status": "idle", "progress": 0.0, "url": None, "video_id": None, "privacy": None,
            "error": None, "warning": None, "started_at": None, "finished_at": None}


def get_state(project_id: str) -> dict:
    st = {**idle_state(), **(_read_json(_dir(project_id) / STATE_FILE) or {})}
    with _lock:
        running = project_id in _active
    if st["status"] in ACTIVE and not running:
        # 서버 재시작 등으로 끊긴 작업
        st.update(status="error", error="업로드가 중단됐어요. 다시 시도해 주세요.")
    return st


def update_state(project_id: str, **fields) -> dict:
    with _lock:
        path = _dir(project_id) / STATE_FILE
        st = {**idle_state(), **(_read_json(path) or {}), **fields}
        _write_json(path, st)
        return st


def load_meta(project_id: str) -> Optional[dict]:
    return _read_json(_dir(project_id) / META_FILE)


def save_meta(project_id: str, meta: dict) -> dict:
    _write_json(_dir(project_id) / META_FILE, meta)
    return meta


def is_active(project_id: str) -> bool:
    with _lock:
        return project_id in _active


def try_begin(project_id: str, privacy: str) -> bool:
    """진행 중이 아니면 queued 로 표시하고 True."""
    with _lock:
        if project_id in _active:
            return False
        _active.add(project_id)
    update_state(project_id, status="queued", progress=0.0, url=None, video_id=None, privacy=privacy,
                 error=None, warning=None, started_at=_now(), finished_at=None)
    return True


# ────────────────────────────────────────────────────────────────────────── #
# 썸네일
# ────────────────────────────────────────────────────────────────────────── #
def make_thumbnail(src, dest: Path) -> Path:
    """이미지 → JPEG 1280x720 (cover-fit), 2MB 미만."""
    img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, THUMB_SIZE, method=Image.LANCZOS)
    data = b""
    for q in (90, 82, 74, 65, 55, 45):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) < THUMB_MAX_BYTES:
            break
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def resolve_thumbnail(project_id: str, choice: Optional[str], slides: list) -> Optional[Path]:
    """'none' | 'upload' | 'slide:<n>' → 업로드할 JPEG 경로 (없으면 None)."""
    choice = (choice or "none").strip()
    d = _dir(project_id)
    if choice == "upload":
        p = d / THUMB_FILE
        return p if p.is_file() else None
    if choice.startswith("slide:"):
        try:
            n = int(choice.split(":", 1)[1])
            s = slides[n]
        except (ValueError, IndexError):
            return None
        name = getattr(s, "image_filename", "") or ""
        src = d / "assets" / name
        if not name or not src.is_file():
            return None
        try:
            return make_thumbnail(src, d / SLIDE_THUMB_FILE)
        except Exception as e:
            logger.warning(f"YouTube: 슬라이드 썸네일 생성 실패: {e}")
            return None
    return None


# ────────────────────────────────────────────────────────────────────────── #
# 실행 (BackgroundTasks)
# ────────────────────────────────────────────────────────────────────────── #
def run_upload(project_id: str, params: dict) -> None:
    """params: {title, description, tags, privacy, thumbnail_path, fallback_title}."""
    last = {"pct": -10.0}

    def progress(pct: float) -> None:
        if pct - last["pct"] >= 1.0 or pct >= 100:
            last["pct"] = pct
            update_state(project_id, status="uploading", progress=min(99.0, float(pct)))

    try:
        update_state(project_id, status="uploading", progress=0.0)
        res = youtube_manager.upload_video(
            _dir(project_id) / "result.mp4", params.get("title"), params.get("description"),
            tags=params.get("tags"), privacy=params.get("privacy", "private"),
            thumbnail_path=params.get("thumbnail_path"), progress_cb=progress,
            fallback_title=params.get("fallback_title") or "DocuMotion")
        update_state(project_id, status="done", progress=100.0, url=res["url"], video_id=res["video_id"],
                     warning=res.get("warning"), error=None, finished_at=_now())
    except PermissionError as e:
        update_state(project_id, status="error", error=auth_message(str(e)), finished_at=_now())
    except Exception as e:
        logger.error(f"YouTube upload failed: {e}", exc_info=True)
        update_state(project_id, status="error", error=_short_error(e), finished_at=_now())
    finally:
        with _lock:
            _active.discard(project_id)


AUTH_MESSAGES = {
    "no_token": "YouTube 인증이 필요해요",
    "reauth_required": "YouTube 인증이 만료됐어요",
    "scope_missing": "YouTube 업로드 권한이 없어요",
}


def auth_message(reason: str) -> str:
    return AUTH_MESSAGES.get(reason, "YouTube 인증을 확인해 주세요")


def _short_error(e: Exception) -> str:
    msg = str(e)
    if "quotaExceeded" in msg:
        return "오늘 업로드 한도를 넘었어요. 내일 다시 시도해 주세요."
    if "uploadLimitExceeded" in msg:
        return "채널 업로드 한도를 넘었어요."
    return ("업로드 실패: " + msg)[:200]
