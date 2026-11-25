"""
DocuMotion - 간편 자동 모드 API (가져오기 → 장소·느낌 → 음악 → 자동 렌더)

GET  /{id}/easy/estimate          — 예상 영상 길이 (계획 dry-run, 음악 맞춤 전)
POST /{id}/easy/music-candidates  — AI 키워드/검색어 → Jamendo 연주곡 후보 3~4개 + 길이 맞춤 라벨
POST /{id}/easy/music             — 곡 선택: 다운로드(허용 곡만) → BGM + 크레딧
GET  /{id}/easy/credit            — YouTube 설명용 음악 크레딧 문구
POST /{id}/easy/render            — (선별) → 계획 → 음악 맞춤 → 렌더 (202, 백그라운드)
GET  /{id}/easy/status            — 진행 상태
POST /{id}/easy/reshuffle         — 전환·그림 효과·Ken Burns 다시 섞기 (잠긴 슬라이드 제외, 렌더 없음)
"""
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db
from backend.services import easy_auto, task_status
from backend.services.auto_edit import AutoEditError, reshuffle_effects
from backend.services.easy_auto import EasyAutoError, TASK_KEY, run_easy_render
from backend.services.music_search import JamendoError

router = APIRouter(prefix="/projects", tags=["easy-auto"])
logger = get_logger(__name__)


def _project_or_404(db, project_id: str) -> Project:
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _http(e: Exception) -> HTTPException:
    if isinstance(e, JamendoError):
        return HTTPException(status_code=e.status, detail=e.detail())
    if isinstance(e, EasyAutoError):
        return HTTPException(status_code=e.status, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


@router.get("/{project_id}/easy/estimate")
def easy_estimate(project_id: str, slide_duration: Optional[float] = None, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    if slide_duration is not None:
        slide_duration = max(1.5, min(8.0, slide_duration))
    try:
        return easy_auto.estimate_length(db, project_id, slide_duration)
    except (EasyAutoError, AutoEditError) as e:
        raise _http(e)


class CandidatesRequest(BaseModel):
    keywords: Optional[list[str]] = Field(default=None, max_length=6)
    query: Optional[str] = Field(default=None, max_length=100)
    slide_duration: Optional[float] = Field(default=None, ge=1.5, le=8.0)


@router.post("/{project_id}/easy/music-candidates")
async def easy_music_candidates(project_id: str, req: CandidatesRequest, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return await easy_auto.music_candidates(db, project_id, req.keywords, req.query, req.slide_duration)
    except (JamendoError, EasyAutoError, AutoEditError) as e:
        raise _http(e)


class ChooseMusicRequest(BaseModel):
    track: dict


@router.post("/{project_id}/easy/music")
async def easy_choose_music(project_id: str, req: ChooseMusicRequest, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return await easy_auto.choose_track(db, project_id, req.track)
    except (JamendoError, EasyAutoError) as e:
        raise _http(e)


@router.get("/{project_id}/easy/credit")
def easy_credit(project_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    return easy_auto.credit_text(db, project_id)


class EasyRenderRequest(BaseModel):
    resolution: Optional[Literal["720p", "1080p"]] = None
    stylize_ratio: Optional[float] = Field(default=None, ge=0.0, le=0.5)
    slide_duration: Optional[float] = Field(default=None, ge=1.5, le=8.0)
    auto_curate: Optional[bool] = None
    seed: Optional[int] = Field(default=None, ge=0, le=1_000_000)


@router.post("/{project_id}/easy/render", status_code=202)
def easy_render(project_id: str, req: EasyRenderRequest, background_tasks: BackgroundTasks,
                db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id)
    if (st and st.get("status") == "running") or project.status in ("QUEUED", "PROCESSING"):
        raise HTTPException(status_code=409, detail="렌더링이 이미 진행 중입니다")
    if not db.query(Slide).filter(Slide.project_id == project_id).count():
        raise HTTPException(status_code=400, detail="사진이나 영상을 먼저 가져오세요")
    if not easy_auto.has_bgm(project):
        raise HTTPException(status_code=400, detail="음악을 먼저 골라주세요 (BGM 필요)")
    opts = req.model_dump()
    if opts.get("auto_curate") is None:
        opts["auto_curate"] = easy_auto.easy_settings(easy_auto._slides(db, project_id)).get("auto_curate", True)
    task_status.set_status(task_id, status="running", phase="queued", progress=0.0,
                           message="대기 중...", summary=None)
    background_tasks.add_task(run_easy_render, project_id, opts)
    logger.info(f"Easy render queued: {project_id} {opts}")
    return {"ok": True, "project_id": project_id, "options": opts}


@router.get("/{project_id}/easy/status")
def easy_status(project_id: str, db: Session = Depends(get_db)):
    st = task_status.get_status(TASK_KEY.format(project_id=project_id)) or {}
    project = db.query(Project).filter(Project.id == project_id).first()
    render = {}
    if project:
        render = {"status": project.status, "progress": project.progress, "message": project.message}
    return {"task": st, "render": render}


class ReshuffleRequest(BaseModel):
    seed: Optional[int] = Field(default=None, ge=1, le=1_000_000)


@router.post("/{project_id}/easy/reshuffle")
def easy_reshuffle(project_id: str, req: ReshuffleRequest, db: Session = Depends(get_db)):
    """전환·그림 효과·Ken Burns 만 새 seed 로 다시 고른다 (잠긴 슬라이드 제외). 간편 자동이고 BGM 이 있으면
    길이 맞춤도 다시 한다 (그림 슬라이드가 바뀌면 실사 홀드 길이가 달라지므로)."""
    project = _project_or_404(db, project_id)
    if project.status in ("QUEUED", "PROCESSING"):
        raise HTTPException(status_code=409, detail="렌더링 중에는 다시 섞을 수 없습니다")
    try:
        out = reshuffle_effects(db, project_id, req.seed)
        if out.get("easy") and easy_auto.has_bgm(project):
            out["fit"] = easy_auto.fit_music(db, project_id)
        return out
    except (AutoEditError, EasyAutoError) as e:
        raise _http(e)
