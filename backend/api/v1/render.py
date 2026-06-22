"""
DocuMotion - 렌더링 API (시작/상태/다운로드)
"""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import RenderStatus
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services import worker as render_worker

router = APIRouter(prefix="/projects", tags=["render"])
logger = get_logger(__name__)


@router.post("/{project_id}/render", status_code=202)
def start_render(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """렌더링 시작 - 백그라운드 태스크 큐에 추가"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status in ("QUEUED", "PROCESSING"):
        raise HTTPException(status_code=409, detail="Already rendering")

    # 슬라이드 존재 확인
    slides = db.query(Slide).filter(Slide.project_id == project_id).all()
    if not slides:
        raise HTTPException(status_code=400, detail="No slides to render")

    # 상태 업데이트
    project.status     = "QUEUED"
    project.progress   = 0
    project.message    = "대기 중..."
    project.updated_at = datetime.utcnow()
    db.commit()

    # 백그라운드 렌더링 시작
    background_tasks.add_task(render_worker.run_render, project_id)
    logger.info(f"Render queued: {project_id}")
    return {"ok": True, "project_id": project_id}


@router.get("/{project_id}/render/status", response_model=RenderStatus)
def get_render_status(project_id: str, db: Session = Depends(get_db)):
    """렌더링 진행 상태 조회"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return RenderStatus(
        status=project.status,
        progress=project.progress,
        message=project.message
    )


@router.get("/{project_id}/download")
def download_video(project_id: str, db: Session = Depends(get_db)):
    """결과 영상 다운로드"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    video_path = OUTPUTS_DIR / project_id / "result.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found. Render first.")

    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in project.name)[:50]
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"{safe_name}.mp4",
        headers={"Cache-Control": "no-cache, must-revalidate"}
    )
