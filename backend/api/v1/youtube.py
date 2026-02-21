"""
DocuMotion - YouTube 업로드 API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project
from backend.schema.project import YouTubeUploadRequest
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services import youtube_manager

router = APIRouter(prefix="/projects", tags=["youtube"])
logger = get_logger(__name__)


@router.post("/{project_id}/youtube/upload")
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
    except Exception as e:
        logger.error(f"YouTube upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
