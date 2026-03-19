"""
DocuMotion - YouTube 업로드 & OAuth 인증 API
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project
from backend.schema.project import YouTubeUploadRequest
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services import youtube_manager

router = APIRouter(tags=["youtube"])
logger = get_logger(__name__)

OAUTH_REDIRECT = "http://localhost"


# ─── YouTube 인증 상태 확인 ─────────────────────
@router.get("/youtube/auth-status")
def youtube_auth_status():
    """현재 YouTube 인증 상태 반환"""
    return youtube_manager.get_auth_status()


# ─── OAuth 인증 URL 생성 (4-1-2) ─────────────────
@router.get("/youtube/auth-url")
def youtube_auth_url(request: Request):
    """OAuth2 인증 URL 생성 후 반환"""
    try:
        url = youtube_manager.generate_auth_url(OAUTH_REDIRECT)
        return {"auth_url": url}
    except Exception as e:
        logger.error(f"Auth URL generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 수동 코드 교환 ──────────────────────────────
from pydantic import BaseModel as _BaseModel

class _CodeExchangeRequest(_BaseModel):
    code: str

@router.post("/youtube/exchange-code")
def youtube_exchange_code(req: _CodeExchangeRequest):
    """수동으로 입력받은 authorization code → token 교환"""
    try:
        youtube_manager.exchange_code(req.code, OAUTH_REDIRECT)
        logger.info("YouTube OAuth code exchange success")
        return {"ok": True}
    except Exception as e:
        logger.error(f"YouTube code exchange failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ─── YouTube 업로드 ──────────────────────────────
@router.post("/projects/{project_id}/youtube/upload")
def upload_to_youtube(
    project_id: str,
    payload: YouTubeUploadRequest,
    db: Session = Depends(get_db)
):
    """YouTube에 결과 영상 업로드"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    video_path = OUTPUTS_DIR / project_id / "result.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found. Render first.")

    try:
        url = youtube_manager.upload_short(
            file_path=str(video_path),
            title=payload.title,
            description=payload.description,
            tags=payload.tags
        )
        if url:
            logger.info(f"YouTube upload success: {url}")
            return {"ok": True, "url": url}
        else:
            raise HTTPException(status_code=500, detail="YouTube upload failed")
    except PermissionError as e:
        # 인증 필요 — 401로 반환하여 프론트에서 재인증 트리거
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"YouTube upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
