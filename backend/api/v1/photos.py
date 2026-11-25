"""
DocuMotion - Google Photos Picker API 연동
세션 생성 → Picker UI 팝업 → 폴링 → 선택 미디어 다운로드 → 슬라이드 생성
"""
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
from backend.services import media_import, photos_manager

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


# 공용 미디어 처리 (Google 드라이브 가져오기와 공유) — backend/services/media_import.py
_sanitize = media_import.sanitize
_is_hevc = media_import.is_hevc
_transcode_to_h264 = media_import.transcode_to_h264


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

            # 슬라이드 생성 (HEVC→H.264, HEIC→JPEG, EXIF — 드라이브 가져오기와 공용)
            order_idx = existing_count + len(imported)
            slide, saved_filename, kind = media_import.make_media_slide(
                project_id, Path(dest_path), original_name,
                "video/mp4" if is_video else mime_type, order_idx)

            db.add(slide)
            db.flush()                      # slide.id 기본값 채우기 (응답·성공 개수용)
            imported.append({"id": slide.id, "filename": saved_filename, "type": kind})
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
