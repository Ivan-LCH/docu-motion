"""
DocuMotion - 프로젝트 CRUD API
"""
import os
import shutil
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import ProjectCreate, ProjectRead, ProjectDetail
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger

router = APIRouter(prefix="/projects", tags=["projects"])
logger = get_logger(__name__)


def _enrich_project(project: Project) -> dict:
    """Project ORM 객체를 응답형 dict로 변환"""
    video_path = OUTPUTS_DIR / project.id / "result.mp4"
    return {
        "id":          project.id,
        "name":        project.name,
        "status":      project.status,
        "stage":       project.stage,
        "progress":    project.progress,
        "message":     project.message,
        "has_video":   video_path.exists(),
        "slide_count": len(project.slides),
        "created_at":  project.created_at,
        "updated_at":  project.updated_at,
    }


@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    """프로젝트 목록 (최신순)"""
    projects = db.query(Project).order_by(Project.updated_at.desc()).all()
    return [_enrich_project(p) for p in projects]


@router.post("", response_model=ProjectRead, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    """새 프로젝트 생성"""
    project = Project(name=payload.name)
    db.add(project)
    db.commit()
    db.refresh(project)
    # outputs 디렉토리 생성
    (OUTPUTS_DIR / project.id / "assets").mkdir(parents=True, exist_ok=True)
    logger.info(f"Project created: {project.id} ({project.name})")
    return _enrich_project(project)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, db: Session = Depends(get_db)):
    """프로젝트 상세 조회 (슬라이드 포함)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    result = _enrich_project(project)
    result["slides"] = [
        {
            "id": s.id, "order_index": s.order_index, 
            "image_filename": s.image_filename, "label": s.label, "text": s.text,
            "slide_type": getattr(s, "slide_type", "image"),
            "video_filename": getattr(s, "video_filename", ""),
            "volume": getattr(s, "volume", 1.0),
            "subtitles": getattr(s, "subtitles", "[]"),
            "use_tts": getattr(s, "use_tts", 1),
            "trim_start": getattr(s, "trim_start", 0.0),
            "trim_end": getattr(s, "trim_end", 0.0)
        }
        for s in project.slides
    ]
    return result


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
