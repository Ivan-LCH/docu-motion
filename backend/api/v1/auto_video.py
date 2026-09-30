"""
DocuMotion - 원클릭 자동 영상 API

POST /{project_id}/auto-video        — 파이프라인 시작 (202)
GET  /{project_id}/auto-video/status — 진행 상태
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.core.logger import get_logger
from backend.services import task_status
from backend.services.auto_video import TASK_KEY, run_auto_video
from backend.services.color_grade import VALID_STYLE_PRESETS

router = APIRouter(prefix="/projects", tags=["auto-video"])
logger = get_logger(__name__)


class AutoVideoRequest(BaseModel):
    narration_ratio: float = Field(
        default=0.4, ge=0.0, le=1.0,
        description="나레이션(자막+TTS)을 달 슬라이드 비율. 나머지는 무자막 통과")
    tone: str = Field(default="documentary",
                      description="documentary | vlog")
    style_preset: str = Field(default="cinematic",
                              description=f"자동 연출 스타일: {VALID_STYLE_PRESETS}")
    auto_curate: bool = Field(default=True,
                              description="흐림/어두움/중복 컷 자동 제거")


@router.post("/{project_id}/auto-video", status_code=202)
def start_auto_video(project_id: str, req: AutoVideoRequest,
                     background_tasks: BackgroundTasks,
                     db: Session = Depends(get_db)):
    """원클릭 자동 영상: 큐레이션 → 스마트 나레이션 → 자동 연출 → 렌더."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id)
    if st and st.get("status") == "running":
        raise HTTPException(status_code=409, detail="자동 영상이 이미 진행 중입니다")
    if project.status in ("QUEUED", "PROCESSING"):
        raise HTTPException(status_code=409, detail="렌더링이 이미 진행 중입니다")

    slides = db.query(Slide).filter(Slide.project_id == project_id).count()
    if not slides:
        raise HTTPException(status_code=400, detail="슬라이드가 없습니다. 먼저 사진을 가져오세요")

    if req.tone not in ("documentary", "vlog"):
        raise HTTPException(status_code=400, detail="tone은 documentary | vlog")
    if req.style_preset not in VALID_STYLE_PRESETS:
        raise HTTPException(status_code=400,
                            detail=f"Invalid style_preset. Allowed: {VALID_STYLE_PRESETS}")

    task_status.set_status(task_id, status="running", phase="queued",
                           progress=0.0, message="대기 중...")
    background_tasks.add_task(run_auto_video, project_id, req.model_dump())
    logger.info(f"Auto-video queued: {project_id} {req.model_dump()}")
    return {"ok": True, "project_id": project_id,
            "options": req.model_dump()}


@router.get("/{project_id}/auto-video/status")
def auto_video_status(project_id: str, db: Session = Depends(get_db)):
    """자동 영상 진행 상태 + 렌더 상태 병합."""
    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id) or {}
    project = db.query(Project).filter(Project.id == project_id).first()
    render = {}
    if project:
        render = {"status": project.status, "progress": project.progress,
                  "message": project.message}
    return {"task": st, "render": render}


@router.delete("/{project_id}/auto-video")
def clear_auto_video_status(project_id: str):
    """완료/실패 후 상태 초기화 (다음 실행을 위해)."""
    task_status.clear(TASK_KEY.format(project_id=project_id))
    return {"ok": True}
