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

        VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".ts", ".3gp"}

        is_video = ext in VIDEO_EXTS or (f.content_type or '').startswith('video/')
        logger.info(f"Upload: filename={f.filename!r} ext={ext!r} content_type={f.content_type!r} is_video={is_video}")

        if is_video:
            slide = Slide(
                project_id=project_id,
                order_index=0,
                image_filename="",
                label=f.filename,
                text="",
                slide_type="video",
                video_filename=saved_filename,
                volume=1.0,
                subtitles="[]",
                use_tts=1,
                trim_start=0.0,
                trim_end=0.0
            )
            db.add(slide)
            new_slides.append(slide)
        elif ext == ".pdf":
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
            # 디버그: 프론트에서 보낸 데이터 vs DB 기존 값 비교
            logger.info(f"SaveSlide [{s_data.id[:8]}] frontend: type={s_data.slide_type!r} vid={s_data.video_filename!r} | db: type={slide.slide_type!r} vid={slide.video_filename!r}")
            slide.order_index    = s_data.order_index
            slide.text           = s_data.text
            slide.image_filename = s_data.image_filename
            slide.label          = s_data.label
            # Video Slide Fields — 프론트에서 기본값으로 보내면 기존 DB 값 유지
            if s_data.slide_type and s_data.slide_type != 'image':
                slide.slide_type = s_data.slide_type
            elif s_data.slide_type == 'image' and slide.slide_type == 'video':
                # 프론트가 기본값 "image"를 보내도, DB에 video로 저장된 것은 유지
                pass  # slide.slide_type 유지
            else:
                slide.slide_type = s_data.slide_type

            if s_data.video_filename:
                slide.video_filename = s_data.video_filename
            # else: 기존 video_filename 유지

            slide.volume         = s_data.volume
            slide.subtitles      = s_data.subtitles
            slide.use_tts        = s_data.use_tts
            slide.trim_start     = getattr(s_data, 'trim_start', 0.0)
            slide.trim_end       = getattr(s_data, 'trim_end', 0.0)
            logger.info(f"SaveSlide [{s_data.id[:8]}] RESULT: type={slide.slide_type!r} vid={slide.video_filename!r}")

    # stage 계산
    all_slides = db.query(Slide).filter(Slide.project_id == project_id).all()
    has_text = any((s.text or '').strip() for s in all_slides)
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
    assets_dir = _assets_dir(project_id)
    # 이미지 파일 삭제
    if slide.image_filename:
        img = assets_dir / slide.image_filename
        if img.exists():
            os.unlink(img)
    # 비디오 파일 삭제
    if slide.video_filename:
        vid = assets_dir / slide.video_filename
        if vid.exists():
            os.unlink(vid)
    db.delete(slide)
    db.commit()


# ─────────────────────────────────────────────
# 갤러리 이미지 콜라주 생성
# ─────────────────────────────────────────────
from PIL import Image
from pydantic import BaseModel

class CollageRequest(BaseModel):
    slide_ids: List[str]

@router.post("/{project_id}/slides/collage", response_model=SlideRead)
def create_collage(
    project_id: str,
    request: CollageRequest,
    db: Session = Depends(get_db)
):
    """여러 이미지 슬라이드를 합쳐 하나의 콜라주 슬라이드로 변환"""
    project = _get_project_or_404(project_id, db)
    if not request.slide_ids or len(request.slide_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 slides required for collage")

    # DB에서 슬라이드 검색
    slides = db.query(Slide).filter(
        Slide.project_id == project_id,
        Slide.id.in_(request.slide_ids),
        Slide.slide_type == "image"
    ).order_by(Slide.order_index).all()

    if len(slides) != len(request.slide_ids):
        raise HTTPException(status_code=400, detail="Some selected slides are invalid or not images")

    assets_dir = _assets_dir(project_id)
    images = []
    for s in slides:
        if s.image_filename:
            path = assets_dir / s.image_filename
            if path.exists():
                images.append(Image.open(path).convert('RGBA'))

    if not images:
        raise HTTPException(status_code=400, detail="No valid images found for the selected slides")

    # 콜라주 병합 (간단한 가로 나열)
    widths, heights = zip(*(i.size for i in images))
    total_width = sum(widths)
    max_height = max(heights)

    collage_im = Image.new('RGBA', (total_width, max_height), (255, 255, 255, 0))
    x_offset = 0
    for im in images:
        # 이미지를 높이에 맞춰 리사이즈
        if im.size[1] != max_height:
            new_w = int(im.size[0] * (max_height / im.size[1]))
            resized = im.resize((new_w, max_height), Image.Resampling.LANCZOS)
        else:
            resized = im
        collage_im.paste(resized, (x_offset, 0))
        x_offset += resized.size[0]

    # 저장
    timestamp = datetime.now().strftime('%H%M%S%f')
    collage_filename = f"collage_{timestamp}.png"
    collage_path = assets_dir / collage_filename
    
    # RGBA -> RGB 변환 후 저장 (JPEG 등 지원을 위해 필요하지만 여기선 PNG 권장)
    if collage_im.mode in ('RGBA', 'LA') or (collage_im.mode == 'P' and 'transparency' in collage_im.info):
        bg = Image.new("RGB", collage_im.size, (255, 255, 255))
        bg.paste(collage_im, mask=collage_im.split()[3] if collage_im.mode == 'RGBA' else None)
        bg.save(collage_path, "PNG")
    else:
        collage_im.save(collage_path, "PNG")

    # DB 업데이트 (가장 앞의 order_index 기준)
    base_order = slides[0].order_index
    new_slide = Slide(
        project_id=project_id,
        order_index=base_order,
        image_filename=collage_filename,
        label=f"Collage ({len(images)} imgs)",
        text=""
    )
    db.add(new_slide)

    # 이전 슬라이드 삭제
    for s in slides:
        if s.image_filename:
            old_path = assets_dir / s.image_filename
            if old_path.exists():
                try: os.unlink(old_path)
                except: pass
        db.delete(s)
        
    db.commit()
    db.refresh(new_slide)

    # 순서 재정렬
    remaining = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    for idx, s in enumerate(remaining):
        s.order_index = idx
    project.updated_at = datetime.utcnow()
    db.commit()

    return new_slide


# ─────────────────────────────────────────────
# 에셋 파일 서빙 (이미지/동영상, Range 요청 지원)
# ─────────────────────────────────────────────
import mimetypes
from fastapi import Request
from fastapi.responses import StreamingResponse, FileResponse

@router.get("/{project_id}/assets/{filename}")
def serve_asset(project_id: str, filename: str, request: Request):
    """에셋 파일 서빙 (동영상 미리보기 Range 요청 지원 → seek 슬라이더 작동)"""
    assets_dir = _assets_dir(project_id)
    file_path = assets_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Asset not found")

    file_size = file_path.stat().st_size
    mime_type, _ = mimetypes.guess_type(str(file_path))
    mime_type = mime_type or "application/octet-stream"

    # Range 헤더가 있으면 Partial Content(206) 응답
    range_header = request.headers.get("range")
    if range_header:
        try:
            range_val = range_header.strip().lower().replace("bytes=", "")
            start_str, end_str = range_val.split("-")
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range")

        if start >= file_size or end >= file_size or start > end:
            raise HTTPException(
                status_code=416, detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"}
            )

        chunk_size = end - start + 1

        def iter_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            }
        )

    # Range 없으면 전체 파일 반환 (Accept-Ranges 헤더 포함)
    return FileResponse(
        str(file_path),
        media_type=mime_type,
        headers={"Accept-Ranges": "bytes"},
    )

