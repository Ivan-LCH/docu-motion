# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Import
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
import os
import shutil
from datetime import datetime

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import ProjectCreate, ProjectRead, ProjectDetail
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Router
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
router = APIRouter(prefix="/projects", tags=["projects"])
logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Helper Functions
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
def _enrich_project(project: Project) -> dict:
    """Project ORM 객체를 응답형 dict로 변환"""
    video_path = OUTPUTS_DIR / project.id / "result.mp4"
    return {
        "id"          : project.id,
        "name"        : project.name,
        "status"      : project.status,
        "stage"       : project.stage,
        "progress"    : project.progress,
        "message"     : project.message,
        "has_video"   : video_path.exists(),
        "slide_count" : len(project.slides),
        "created_at"  : project.created_at,
        "updated_at"  : project.updated_at,
        "bgm_filename": getattr(project, "bgm_filename", "") or "",
        "bgm_volume"  : getattr(project, "bgm_volume", 0.3) or 0.3,
        "aspect_ratio": getattr(project, "aspect_ratio", "16:9") or "16:9",
        "tts_master_volume": getattr(project, "tts_master_volume", 1.0) or 1.0,
        "default_transition": getattr(project, "default_transition", "none") or "none",
        "default_slide_duration": getattr(project, "default_slide_duration", 3.0) or 3.0,
        "subtitle_font_size": getattr(project, "subtitle_font_size", 28) or 28,
        "subtitle_font_color": getattr(project, "subtitle_font_color", "white") or "white",
        "watermark_text": getattr(project, "watermark_text", "") or "",
        "watermark_opacity": getattr(project, "watermark_opacity", 0.3) or 0.3,
    }


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# API Endpoints
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    """프로젝트 목록 (최신순)"""
    projects = db.query(Project).order_by(Project.updated_at.desc()).all()
    return [_enrich_project(p) for p in projects]



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Create Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("", response_model=ProjectRead, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    """새 프로젝트 생성"""
    project = Project(
        name         = payload.name,
        aspect_ratio = getattr(payload, 'aspect_ratio', '16:9')
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    # outputs 디렉토리 생성
    (OUTPUTS_DIR / project.id / "assets").mkdir(parents=True, exist_ok=True)
    logger.info(f"Project created: {project.id} ({project.name})")
    return _enrich_project(project)



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Get Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, db: Session = Depends(get_db)):
    """프로젝트 상세 조회 (슬라이드 포함)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    result = _enrich_project(project)
    result["slides"] = [
        {
            "id"            : s.id,
            "order_index"   : s.order_index,
            "image_filename": s.image_filename,
            "label"         : s.label,
            "text"          : s.text,
            "slide_type"    : getattr(s, "slide_type", "image"),
            "video_filename": getattr(s, "video_filename", ""),
            "volume"        : getattr(s, "volume", 1.0),
            "subtitles"     : getattr(s, "subtitles", "[]"),
            "use_tts"       : getattr(s, "use_tts", 1),
            "trim_start"    : getattr(s, "trim_start", 0.0),
            "trim_end"      : getattr(s, "trim_end", 0.0),
            "transition"    : getattr(s, "transition", "none"),
            "tts_volume"    : getattr(s, "tts_volume", 1.0),
        }
        for s in project.slides
    ]
    return result



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Delete Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, db: Session = Depends(get_db)):
    """프로젝트 삭제"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(project)
    db.commit()
    # 파일 삭제
    project_dir = OUTPUTS_DIR / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir)
    logger.info(f"Project deleted: {project_id}")


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM / 프로젝트 설정 엔드포인트
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
from fastapi import UploadFile, File
from pydantic import BaseModel as PydanticBase

class ProjectSettingsUpdate(PydanticBase):
    bgm_volume: float = 0.3
    aspect_ratio: str = "16:9"
    tts_master_volume: float = 1.0
    default_transition: Optional[str] = None
    default_slide_duration: Optional[float] = None
    subtitle_font_size: Optional[int] = None
    subtitle_font_color: Optional[str] = None
    watermark_text: Optional[str] = None
    watermark_opacity: Optional[float] = None



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Upload BGM
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("/{project_id}/bgm", response_model=ProjectRead)
async def upload_bgm(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """BGM 파일 업로드"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    assets_dir = OUTPUTS_DIR / project_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    
    bgm_path   = assets_dir / f"bgm_{file.filename}"
    content    = await file.read()
    
    with open(bgm_path, "wb") as f:
        f.write(content)
    
    project.bgm_filename = f"bgm_{file.filename}"
    project.updated_at   = datetime.utcnow()
    db.commit()
    
    logger.info(f"BGM uploaded: {project_id} → {project.bgm_filename}")
    return _enrich_project(project)



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Delete BGM
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.delete("/{project_id}/bgm", response_model=ProjectRead)
def delete_bgm(project_id: str, db: Session = Depends(get_db)):
    """BGM 파일 제거"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    if project.bgm_filename:
        bgm_path = OUTPUTS_DIR / project_id / "assets" / project.bgm_filename
        if bgm_path.exists():
            bgm_path.unlink()
    
    project.bgm_filename = ""
    project.updated_at   = datetime.utcnow()
    db.commit()
    return _enrich_project(project)



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Update Project Settings
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.patch("/{project_id}/settings", response_model=ProjectRead)
def update_project_settings(project_id: str, payload: ProjectSettingsUpdate, db: Session = Depends(get_db)):
    """프로젝트 설정 업데이트 (BGM 볼륨, 화면 비율)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.bgm_volume        = payload.bgm_volume
    project.aspect_ratio      = payload.aspect_ratio
    project.tts_master_volume = payload.tts_master_volume
    if payload.default_transition is not None:
        project.default_transition = payload.default_transition
    if payload.default_slide_duration is not None:
        project.default_slide_duration = payload.default_slide_duration
    if payload.subtitle_font_size is not None:
        project.subtitle_font_size = payload.subtitle_font_size
    if payload.subtitle_font_color is not None:
        project.subtitle_font_color = payload.subtitle_font_color
    if payload.watermark_text is not None:
        project.watermark_text = payload.watermark_text
    if payload.watermark_opacity is not None:
        project.watermark_opacity = payload.watermark_opacity
    project.updated_at   = datetime.utcnow()
    db.commit()
    return _enrich_project(project)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# End of file
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
