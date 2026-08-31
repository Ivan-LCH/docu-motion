"""
DocuMotion - Google Photos Picker API 연동
세션 생성 → Picker UI 팝업 → 폴링 → 선택 미디어 다운로드 → 슬라이드 생성
"""
import os
import re
import json
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services import photos_manager
from backend.services.exif_service import extract_exif

router = APIRouter(prefix="/photos", tags=["photos"])
logger = get_logger(__name__)


# ─── 인증 상태 ─────────────────────────────────
@router.get("/auth-status")
def photos_auth_status():
    """Google Photos Picker API 인증 상태 확인"""
    return photos_manager.check_auth()


OAUTH_REDIRECT = "http://localhost"

# ─── OAuth 인증 URL (Picker 스코프 포함) ─────────
@router.get("/auth-url")
def photos_auth_url(request: Request):
    """Picker 스코프 포함 OAuth URL 생성"""
    redirect_uri = OAUTH_REDIRECT
    try:
        url = photos_manager.generate_auth_url(redirect_uri)
        return {"auth_url": url}
    except Exception as e:
        logger.error(f"Photos auth URL failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 수동 코드 교환 ────────────────────────────
class CodeExchangeRequest(BaseModel):
    code: str

@router.post("/exchange-code")
def photos_exchange_code(req: CodeExchangeRequest):
    """수동으로 입력받은 authorization code → token 교환"""
    try:
        photos_manager.exchange_code(req.code, OAUTH_REDIRECT)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Photos code exchange failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ─── Picker 세션 생성 ──────────────────────────
@router.post("/session")
def create_picker_session():
    """Picker 세션 생성 → pickerUri 반환"""
    try:
        return photos_manager.create_session()
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Picker session create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Picker 세션 폴링 ─────────────────────────
@router.get("/session/{session_id}")
def poll_picker_session(session_id: str):
    """세션 폴링 — 사용자 선택 완료 여부 확인"""
    try:
        result = photos_manager.poll_session(session_id)
        logger.info(f"Picker poll {session_id[:12]}...: mediaItemsSet={result.get('mediaItemsSet')}")
        return result
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Picker session poll failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 선택 미디어 가져오기 (다운로드 + 슬라이드 생성) ──
class ImportSessionRequest(BaseModel):
    session_id: str
    sort_order: str = "selected"  # selected | oldest | newest | api


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:80]


def _is_hevc(file_path: str) -> bool:
    """MP4 파일이 HEVC(H.265) 코덱인지 확인"""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            read_size = min(fsize, 1024 * 1024)
            f.seek(fsize - read_size)
            tail = f.read(read_size)
            return b'hvc1' in tail or b'hev1' in tail
    except Exception:
        return False


def _transcode_to_h264(src: str, dst: str) -> bool:
    """HEVC → H.264 GPU(NVENC) 트랜스코딩, 실패 시 CPU(libx264) 폴백"""
    for codec in ["h264_nvenc", "libx264"]:
        try:
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-c:v", codec, "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                dst
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode == 0 and Path(dst).exists() and Path(dst).stat().st_size > 1000:
                logger.info(f"Transcoded HEVC→H.264 ({codec}): {Path(src).name} → {Path(dst).name}")
                return True
            logger.warning(f"Transcode with {codec} failed: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"Transcode with {codec} error: {e}")
    return False


@router.post("/import/{project_id}")
def import_from_session(
    project_id: str,
    req: ImportSessionRequest,
    db: Session = Depends(get_db),
):
    """Picker 세션에서 선택된 미디어를 다운로드하여 프로젝트 슬라이드로 추가"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 선택된 미디어 목록 조회
    try:
        items = photos_manager.list_picked_items(req.session_id, req.sort_order)
    except Exception as e:
        logger.error(f"Failed to list picked items: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not items:
        raise HTTPException(status_code=400, detail="선택된 미디어가 없습니다")

    assets_dir = OUTPUTS_DIR / project_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    existing_count = db.query(Slide).filter(Slide.project_id == project_id).count()
    imported = []

    VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}

    for item in items:
        try:
            original_name = item.get("filename", f"photo_{item['id'][:8]}")
            mime_type = item.get("mimeType", "")
            base_url = item.get("baseUrl", "")
            is_video = item.get("isVideo", False)

            if not base_url:
                logger.warning(f"Skipping item with no baseUrl: {item['id']}")
                continue

            # 파일명 생성
            stem = _sanitize(Path(original_name).stem)
            ext = Path(original_name).suffix.lower()
            if not ext:
                ext = ".mp4" if is_video else ".jpg"
            timestamp = datetime.now().strftime('%H%M%S%f')
            saved_filename = f"{stem}_{timestamp}{ext}"
            dest_path = str(assets_dir / saved_filename)

            # 다운로드 (Authorization 헤더 포함)
            photos_manager.download_item(base_url, mime_type, dest_path)

            # 슬라이드 DB 생성
            order_idx = existing_count + len(imported)
            is_video_file = is_video or ext in VIDEO_EXTS or mime_type.startswith("video/")

            # HEVC(H.265) → H.264 자동 트랜스코딩 (Chrome 호환)
            if is_video_file and _is_hevc(dest_path):
                logger.info(f"HEVC detected: {saved_filename}, transcoding to H.264...")
                h264_name = f"{stem}_{timestamp}_h264.mp4"
                h264_path = str(assets_dir / h264_name)
                if _transcode_to_h264(dest_path, h264_path):
                    os.unlink(dest_path)
                    saved_filename = h264_name
                    dest_path = h264_path
                else:
                    logger.warning(f"HEVC transcode failed, keeping original: {saved_filename}")

            if is_video_file:
                slide = Slide(
                    project_id=project_id,
                    order_index=order_idx,
                    image_filename="",
                    label=original_name,
                    text="",
                    slide_type="video",
                    video_filename=saved_filename,
                    volume=1.0,
                    subtitles="[]",
                    use_tts=1,
                    trim_start=0.0,
                    trim_end=0.0,
                )
            else:
                # EXIF 촬영시각/GPS 저장 (F3 — 일반 업로드와 동일)
                try:
                    exif_data = extract_exif(Path(dest_path))
                    exif_json = json.dumps(exif_data, ensure_ascii=False) if (exif_data.get("captured_at") or exif_data.get("gps")) else "{}"
                except Exception:
                    exif_json = "{}"
                slide = Slide(
                    project_id=project_id,
                    order_index=order_idx,
                    image_filename=saved_filename,
                    label=original_name,
                    text="",
                    exif=exif_json,
                )

            db.add(slide)
            imported.append({
                "id": slide.id, "filename": saved_filename,
                "type": "video" if is_video_file else "image",
            })
            logger.info(f"Photos import: {original_name} -> {saved_filename}")

        except Exception as e:
            logger.error(f"Photos import failed for {item.get('id', '?')}: {e}")
            imported.append({"id": None, "filename": None, "error": str(e)})

    if imported:
        project.stage = "uploaded"
        project.updated_at = datetime.utcnow()
        db.commit()

    # 세션 정리
    photos_manager.delete_session(req.session_id)

    success_count = sum(1 for i in imported if i.get("id"))
    logger.info(f"[{project_id}] Photos Picker import: {success_count}/{len(items)} succeeded")

    return {"ok": True, "imported": imported, "count": success_count}
