"""
DocuMotion - Google Photos Picker API 연동
세션 생성 → Picker UI 팝업 → 폴링 → 선택 미디어 다운로드 → 슬라이드 생성
"""
import json
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
from backend.services import capture_time, media_import, photos_manager

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
    sort_order: str = "oldest"  # oldest(기본 · 촬영 시각순, 사진·영상 섞어서) | newest | selected | api


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
        is_video = bool(item.get("isVideo"))
        kind = "video" if is_video else "image"
        original_name = item.get("filename") or f"{kind}_{item['id'][:8]}"
        dest_path = None
        try:
            mime_type = item.get("mimeType", "")
            base_url = item.get("baseUrl", "")
            if not base_url:
                raise ValueError("다운로드 주소가 없어요")
            status = item.get("processingStatus") or ""
            if is_video and status and status != "READY":
                raise ValueError("아직 처리 중인 영상" if status == "PROCESSING" else "처리 실패한 영상")

            # 파일명 생성 — '=dv' 는 .MOV 원본이어도 mp4 를 준다
            stem = _sanitize(Path(original_name).stem)
            ext = ".mp4" if is_video else (Path(original_name).suffix.lower() or ".jpg")
            timestamp = datetime.now().strftime('%H%M%S%f')
            saved_filename = f"{stem}_{timestamp}{ext}"
            dest_path = assets_dir / saved_filename

            # 다운로드 (Authorization 헤더, 임시 파일 → rename, 용량 상한)
            photos_manager.download_item(base_url, mime_type, str(dest_path), is_video=is_video)

            if is_video:
                info = media_import.probe_video(dest_path)
                if not info["has_video"] or not info["duration"]:
                    raise ValueError("영상 파일이 아니에요")
                media_import.pretrim_long_video(dest_path)     # 60초 넘으면 가운데 15초만 (스트림 복사)

            # 슬라이드 생성 (HEVC→H.264, HEIC→JPEG, EXIF, 영상 포스터 — 드라이브 가져오기와 공용)
            order_idx = existing_count + sum(1 for i in imported if i.get("id"))
            slide, saved_filename, kind = media_import.make_media_slide(
                project_id, dest_path, original_name,
                (mime_type if mime_type.startswith("video/") else "video/mp4") if is_video else mime_type,
                order_idx)

            # 촬영 시각(UTC) 보관 → 나중에 '시간순 정렬' (organize.sort_by_capture_time)
            captured = capture_time.picked_item_key(item)
            if captured is not None:
                exif = json.loads(slide.exif or "{}") or {}
                exif["captured_utc"] = capture_time.to_utc_iso(captured)
                slide.exif = json.dumps(exif, ensure_ascii=False)

            db.add(slide)
            db.flush()                      # slide.id 기본값 채우기 (응답·성공 개수용)
            imported.append({"id": slide.id, "filename": saved_filename, "type": kind})
            logger.info(f"Photos import: {original_name} -> {saved_filename}")

        except Exception as e:
            logger.error(f"Photos import failed for {item.get('id', '?')}: {e}")
            if dest_path is not None:
                for f in (dest_path, dest_path.with_name(media_import.poster_name(dest_path.name))):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            imported.append({"id": None, "filename": original_name, "type": kind, "error": str(e)})

    if any(i.get("id") for i in imported):
        project.stage = "uploaded"
        project.updated_at = datetime.utcnow()
        db.commit()

    # 세션 정리
    photos_manager.delete_session(req.session_id)

    ok = [i for i in imported if i.get("id")]
    success_count = len(ok)
    errors = [{"filename": i["filename"], "type": i["type"], "error": i["error"]}
              for i in imported if not i.get("id")]
    logger.info(f"[{project_id}] Photos Picker import: {success_count}/{len(items)} succeeded")

    return {"ok": True, "imported": imported, "count": success_count,
            "photos": sum(1 for i in ok if i["type"] == "image"),
            "videos": sum(1 for i in ok if i["type"] == "video"),
            "failed": len(errors), "errors": errors}
