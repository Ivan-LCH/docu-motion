"""
DocuMotion - 자동 편집 1단계 API (위치 카드 · 키네틱 타이포 · 음악 인트로)

POST   /{project_id}/auto-edit        — 계획(+렌더) 시작 (202)
GET    /{project_id}/auto-edit/status — 진행 상태 + 계획 요약 + 렌더 상태
DELETE /{project_id}/auto-edit        — 상태 초기화
"""
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db
from backend.services import task_status
from backend.services.auto_edit import TASK_KEY, run_auto_edit
from backend.services.color_grade import VALID_STYLE_PRESETS

router = APIRouter(prefix="/projects", tags=["auto-edit"])
logger = get_logger(__name__)


class AutoEditRequest(BaseModel):
    render: bool = Field(default=True, description="계획 후 바로 렌더")
    resolution: Optional[Literal["720p", "1080p"]] = Field(
        default=None, description="출력 해상도 (비우면 프로젝트 값)")
    intro_seconds: float = Field(default=5.0, ge=0.0, le=20.0,
                                 description="음악 인트로 길이(초). 0 = 인트로 없음")
    music_intro: bool = Field(default=True, description="음악 인트로 + 키네틱 제목")
    location_cards: bool = Field(default=True, description="📍 위치 카드")
    kinetic: bool = Field(default=True, description="키네틱 타이포 (제목 + 중간 문구)")
    narration: bool = Field(default=False, description="일부 슬라이드 나레이션 (기본 음악 전용)")
    use_ai: bool = Field(default=False, description="키네틱 문구를 Gemini 로 생성")
    style_preset: str = Field(default="cinematic",
                              description=f"자동 연출 스타일: {VALID_STYLE_PRESETS}")
    slide_duration: float = Field(default=3.0, ge=1.5, le=8.0)
    stylize_ratio: float = Field(default=0.0, ge=0.0, le=0.5,
                                 description="2단계: 그림→실사 전환 사진 비율 (0 = 끔)")
    stylize_effect: Literal["auto", "sketch", "cartoon"] = Field(
        default="auto", description="그림 스타일 (auto = 만화/스케치 교대)")


@router.post("/{project_id}/auto-edit", status_code=202)
def start_auto_edit(project_id: str, req: AutoEditRequest,
                    background_tasks: BackgroundTasks,
                    db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id)
    if st and st.get("status") == "running":
        raise HTTPException(status_code=409, detail="자동 편집이 이미 진행 중입니다")
    if project.status in ("QUEUED", "PROCESSING"):
        raise HTTPException(status_code=409, detail="렌더링이 이미 진행 중입니다")
    if not db.query(Slide).filter(Slide.project_id == project_id).count():
        raise HTTPException(status_code=400, detail="슬라이드가 없습니다. 먼저 사진을 가져오세요")
    if req.style_preset not in VALID_STYLE_PRESETS:
        raise HTTPException(status_code=400,
                            detail=f"Invalid style_preset. Allowed: {VALID_STYLE_PRESETS}")
    bgm = (project.bgm_filename or "").strip()
    if not bgm or not (OUTPUTS_DIR / project_id / "assets" / bgm).is_file():
        raise HTTPException(status_code=400,
                            detail="BGM 필요: 자동 편집은 배경음악이 있어야 합니다. 먼저 BGM 을 추가하세요")

    opts = req.model_dump()
    task_status.set_status(task_id, status="running", phase="queued",
                           progress=0.0, message="대기 중...", summary=None)
    background_tasks.add_task(run_auto_edit, project_id, opts)
    logger.info(f"Auto-edit queued: {project_id} {opts}")
    return {"ok": True, "project_id": project_id, "options": opts}


@router.get("/{project_id}/auto-edit/status")
def auto_edit_status(project_id: str, db: Session = Depends(get_db)):
    st = task_status.get_status(TASK_KEY.format(project_id=project_id)) or {}
    project = db.query(Project).filter(Project.id == project_id).first()
    render = {}
    if project:
        render = {"status": project.status, "progress": project.progress,
                  "message": project.message}
    return {"task": st, "render": render}


@router.delete("/{project_id}/auto-edit")
def clear_auto_edit_status(project_id: str):
    task_status.clear(TASK_KEY.format(project_id=project_id))
    return {"ok": True}
