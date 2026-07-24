"""
DocuMotion - AI 나레이션 API

- POST /projects/{pid}/slides/{sid}/narration   : 슬라이드 1장 나레이션 생성
- POST /projects/{pid}/narration/generate-all   : 전체 이미지 슬라이드 일괄 생성
- POST /projects/{pid}/narration/split          : 긴 스크립트를 슬라이드 수로 자동 분할
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db
from backend.schema.project import SlideRead
from backend.services import narration
from backend.core.config import OUTPUTS_DIR

logger = get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["narration"])


def _assets_dir(project_id: str):
    return OUTPUTS_DIR / project_id / "assets"


class SplitRequest(BaseModel):
    script: str


class GenerateAllRequest(BaseModel):
    overwrite: bool = False  # True면 기존 대사도 덮어씀


@router.post("/{project_id}/slides/{slide_id}/narration", response_model=SlideRead)
def generate_slide_narration(project_id: str, slide_id: str, db: Session = Depends(get_db)):
    """슬라이드 이미지 1장 → Gemini Vision 나레이션 초안 생성 후 slide.text에 저장"""
    slide = db.query(Slide).filter(Slide.id == slide_id, Slide.project_id == project_id).first()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    if not slide.image_filename:
        raise HTTPException(status_code=400, detail="이미지가 없는 슬라이드입니다")

    project = db.query(Project).filter(Project.id == project_id).first()
    image_path = _assets_dir(project_id) / slide.image_filename
    text = narration.generate_narration_for_image(image_path, project_title=project.name if project else "")
    if not text:
        raise HTTPException(status_code=502, detail="AI 나레이션 생성 실패 (API 키/네트워크 확인)")

    slide.text = text
    db.commit()
    db.refresh(slide)
    return slide


@router.post("/{project_id}/narration/generate-all")
def generate_all_narrations(project_id: str, req: GenerateAllRequest, db: Session = Depends(get_db)):
    """이미지 슬라이드 전체에 나레이션 일괄 생성. 기본은 대사가 비어있는 슬라이드만."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    slides = (
        db.query(Slide)
        .filter(Slide.project_id == project_id, Slide.image_filename != "")
        .order_by(Slide.order_index)
        .all()
    )
    results = []
    for slide in slides:
        if not req.overwrite and (slide.text or "").strip():
            results.append({"slide_id": slide.id, "ok": True, "skipped": True})
            continue
        text = narration.generate_narration_for_image(
            _assets_dir(project_id) / slide.image_filename, project_title=project.name
        )
        if text:
            slide.text = text
            results.append({"slide_id": slide.id, "ok": True, "skipped": False})
        else:
            results.append({"slide_id": slide.id, "ok": False, "skipped": False})
    db.commit()

    ok_count = sum(1 for r in results if r["ok"] and not r["skipped"])
    fail_count = sum(1 for r in results if not r["ok"])
    return {"generated": ok_count, "failed": fail_count, "skipped": len(results) - ok_count - fail_count}


@router.post("/{project_id}/narration/split")
def split_script(project_id: str, req: SplitRequest, db: Session = Depends(get_db)):
    """긴 스크립트를 슬라이드 수에 맞춰 AI 자동 분할 → 각 슬라이드 text에 저장"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    slides = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    if not slides:
        raise HTTPException(status_code=400, detail="슬라이드가 없습니다")
    if not req.script.strip():
        raise HTTPException(status_code=400, detail="스크립트가 비어있습니다")

    parts = narration.split_script(req.script, len(slides))
    if not parts:
        raise HTTPException(status_code=502, detail="AI 스크립트 분할 실패 (API 키/네트워크 확인)")

    for slide, text in zip(slides, parts):
        slide.text = text
    db.commit()
    return {"ok": True, "assigned": len(slides)}
