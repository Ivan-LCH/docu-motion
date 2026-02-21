"""
DocuMotion - 슬라이드 관리 API (업로드, 편집, 순서 변경)
"""
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from typing import List

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import SlideRead, SlideUpdate
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger

router = APIRouter(prefix="/projects", tags=["slides"])
logger = get_logger(__name__)


def _get_project_or_404(project_id: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _assets_dir(project_id: str) -> Path:
    d = OUTPUTS_DIR / project_id / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:80]


def _process_pdf(pdf_path: Path, assets_dir: Path) -> list[dict]:
    """PDF 페이지를 이미지로 변환"""
    import fitz  # PyMuPDF
    doc = fitz.open(str(pdf_path))
    results = []
    base_name = pdf_path.stem
    for i in range(len(doc)):
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=150)
        img_name = f"{base_name}_p{i+1:02d}.png"
        pix.save(str(assets_dir / img_name))
        results.append({"filename": img_name, "label": f"{pdf_path.name} - P{i+1}"})
    return results


@router.get("/{project_id}/slides", response_model=List[SlideRead])
def get_slides(project_id: str, db: Session = Depends(get_db)):
    """슬라이드 목록 조회"""
    _get_project_or_404(project_id, db)
    slides = db.query(Slide).filter(Slide.project_id == project_id)\
               .order_by(Slide.order_index).all()
    return slides


@router.post("/{project_id}/slides/upload", response_model=List[SlideRead])
async def upload_slides(
    project_id: str,
    files: List[UploadFile] = File(...),
    insert_at: int = Form(None),
    db: Session = Depends(get_db)
):
    """이미지/PDF 파일 업로드 → 슬라이드 생성"""
    project = _get_project_or_404(project_id, db)
    assets_dir = _assets_dir(project_id)

    # 기존 슬라이드 목록
    existing_slides = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    
    new_slides = []

    for f in files:
        timestamp = datetime.now().strftime('%H%M%S%f')
        safe_name = _sanitize_filename(Path(f.filename).stem)
        ext = Path(f.filename).suffix.lower()
        saved_filename = f"{safe_name}_{timestamp}{ext}"
        saved_path = assets_dir / saved_filename

        content = await f.read()
        with open(saved_path, "wb") as out:
            out.write(content)

        if ext == ".pdf":
            pdf_assets = _process_pdf(saved_path, assets_dir)
            for asset in pdf_assets:
                slide = Slide(
                    project_id=project_id,
                    order_index=0,  # 임시 지정
                    image_filename=asset["filename"],
                    label=asset["label"],
                    text=""
                )
                db.add(slide)
                new_slides.append(slide)
            os.unlink(saved_path)  # 원본 PDF 제거
        else:
            slide = Slide(
                project_id=project_id,
                order_index=0, # 임시 지정
                image_filename=saved_filename,
                label=f.filename,
                text=""
            )
            db.add(slide)
            new_slides.append(slide)

    # 순서 재정렬
    if insert_at is not None and insert_at >= 0:
        insert_idx = min(insert_at, len(existing_slides))
    else:
        insert_idx = len(existing_slides)
        
    combined_slides = existing_slides[:insert_idx] + new_slides + existing_slides[insert_idx:]
    for idx, slide in enumerate(combined_slides):
        slide.order_index = idx

    # 프로젝트 stage 업데이트
    project.stage = "uploaded"
    project.updated_at = datetime.utcnow()
    db.commit()
    for s in new_slides:
        db.refresh(s)
    logger.info(f"[{project_id}] Uploaded {len(new_slides)} slides at index {insert_idx}")
    return new_slides


@router.put("/{project_id}/slides")
def save_slides(
    project_id: str,
    slides: List[SlideUpdate],
    db: Session = Depends(get_db)
):
    """슬라이드 전체 저장 (순서/텍스트 업데이트)"""
    project = _get_project_or_404(project_id, db)

    for s_data in slides:
        slide = db.query(Slide).filter(Slide.id == s_data.id, Slide.project_id == project_id).first()
        if slide:
            slide.order_index    = s_data.order_index
            slide.text           = s_data.text
            slide.image_filename = s_data.image_filename
            slide.label          = s_data.label

    # stage 계산
    all_slides = db.query(Slide).filter(Slide.project_id == project_id).all()
    has_text = any(s.text.strip() for s in all_slides)
    project.stage = "scripted" if has_text else ("uploaded" if all_slides else "initialized")
    project.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/{project_id}/slides/{slide_id}", status_code=204)
def delete_slide(project_id: str, slide_id: str, db: Session = Depends(get_db)):
    """슬라이드 삭제"""
    slide = db.query(Slide).filter(Slide.id == slide_id, Slide.project_id == project_id).first()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    # 이미지 파일 삭제
    assets_dir = _assets_dir(project_id)
    img = assets_dir / slide.image_filename
    if img.exists():
        os.unlink(img)
    db.delete(slide)
    db.commit()
