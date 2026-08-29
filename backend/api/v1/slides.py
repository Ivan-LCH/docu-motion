"""
DocuMotion - 슬라이드 관리 API (업로드, 편집, 순서 변경)
"""
import os
import re
import struct
import subprocess
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from typing import List, Optional

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import SlideRead, SlideUpdate
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services import map_service
from backend.services import place_info
from backend.services.exif_service import extract_exif
from backend.services import route_slide_service
from backend.services.renderer import ASPECT_RATIO_MAP

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


def _is_hevc(file_path: Path) -> bool:
    """MP4 파일이 HEVC(H.265) 코덱인지 확인 — moov 박스에서 hvc1/hev1 검색"""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            # 파일 끝 1MB에서 코덱 시그니처 검색 (moov가 끝에 있는 경우 대비)
            read_size = min(fsize, 1024 * 1024)
            f.seek(fsize - read_size)
            tail = f.read(read_size)
            return b'hvc1' in tail or b'hev1' in tail
    except Exception:
        return False


def _transcode_to_h264(src: Path, dst: Path) -> bool:
    """HEVC → H.264 GPU(NVENC) 트랜스코딩, 실패 시 CPU(libx264) 폴백"""
    for codec in ["h264_nvenc", "libx264"]:
        try:
            cmd = [
                "ffmpeg", "-y", "-i", str(src),
                "-c:v", codec, "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",  # moov를 앞으로 이동 (스트리밍 최적화)
                str(dst)
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode == 0 and dst.exists() and dst.stat().st_size > 1000:
                logger.info(f"Transcoded HEVC→H.264 ({codec}): {src.name} → {dst.name}")
                return True
            logger.warning(f"Transcode with {codec} failed: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"Transcode with {codec} error: {e}")
    return False


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

        # 청크 단위 스트리밍 저장 (대용량 영상 OOM 방지)
        with open(saved_path, "wb") as out:
            while chunk := await f.read(64 * 1024):
                out.write(chunk)

        VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".ts", ".3gp"}

        is_video = ext in VIDEO_EXTS or (f.content_type or '').startswith('video/')
        logger.info(f"Upload: filename={f.filename!r} ext={ext!r} content_type={f.content_type!r} is_video={is_video}")

        if is_video:
            # HEVC(H.265) → H.264 자동 트랜스코딩 (Chrome 호환)
            if _is_hevc(saved_path):
                logger.info(f"HEVC detected: {saved_filename}, transcoding to H.264...")
                h264_name = f"{safe_name}_{timestamp}_h264.mp4"
                h264_path = assets_dir / h264_name
                if _transcode_to_h264(saved_path, h264_path):
                    os.unlink(saved_path)  # 원본 HEVC 제거
                    saved_filename = h264_name
                    saved_path = h264_path
                else:
                    logger.warning(f"HEVC transcode failed, keeping original: {saved_filename}")

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
            # EXIF 촬원시각/GPS 추출 (F3 자동 구성용 — 실패해도 업로드에 영향 없음)
            try:
                exif_data = extract_exif(saved_path)
            except Exception:
                exif_data = {"captured_at": None, "gps": None}
            slide = Slide(
                project_id=project_id,
                order_index=0, # 임시 지정
                image_filename=saved_filename,
                label=f.filename,
                text="",
                exif=json.dumps(exif_data, ensure_ascii=False) if (exif_data.get("captured_at") or exif_data.get("gps")) else "{}"
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
            slide.transition     = getattr(s_data, 'transition', 'none')
            slide.tts_volume     = getattr(s_data, 'tts_volume', 1.0)
            slide.rotation       = getattr(s_data, 'rotation', 0)
            slide.overlays       = getattr(s_data, 'overlays', '[]')
            slide.image_fit      = getattr(s_data, 'image_fit', 'cover')
            slide.ken_burns      = getattr(s_data, 'ken_burns', 0)

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
    # Route 슬라이드 프레임 파일들 일괄 삭제
    if slide.slide_type == "route":
        _delete_route_frames(assets_dir, slide.meta)
    db.delete(slide)
    db.commit()


# ─────────────────────────────────────────────
# Route / Place 슬라이드 자동 생성 (OSM)
# ─────────────────────────────────────────────
from pydantic import BaseModel as _BM


class RouteSlideRequest(_BM):
    origin: str
    destination: str
    profile: str = "driving"      # 'driving' | 'foot' | 'bicycle'
    insert_at: Optional[int] = None
    duration: float = 5.0         # 애니메이션 지속 시간(초)
    n_frames: int = 30            # 생성할 프레임 수


class PlaceSlideRequest(_BM):
    query: str
    insert_at: Optional[int] = None


class RouteRegenerateRequest(_BM):
    """기존 route 슬라이드 재생성 — origin/destination(좌표)은 유지하고
    profile/n_frames/duration 만 갱신. None 필드는 기존 meta값 유지."""
    profile: Optional[str] = None
    n_frames: Optional[int] = None
    duration: Optional[float] = None


def _canvas_for(project: Project) -> tuple:
    return ASPECT_RATIO_MAP.get(getattr(project, "aspect_ratio", "16:9") or "16:9", (1280, 720))


def _delete_route_frames(assets_dir: Path, meta) -> None:
    """route 슬라이드 meta 내 frames 파일들을 일괄 삭제. meta 가 손상된 경우 무시."""
    if not meta:
        return
    try:
        m = json.loads(meta) if isinstance(meta, str) else meta
        for fn in m.get("frames", []):
            fp = assets_dir / fn
            if fp.exists():
                os.unlink(fp)
    except Exception:
        pass


def _insert_order_index(db: Session, project_id: str, insert_at: Optional[int]) -> tuple:
    """새 슬라이드 삽입 위치의 order_index 와 기존 슬라이드 재정렬을 준비.
    반환: (insert_idx, existing_slides)  — 호출자가 new_slide.order_index = insert_idx 후
    combined = existing[:insert_idx] + [new] + existing[insert_idx:] 로 재정렬."""
    existing = db.query(Slide).filter(Slide.project_id == project_id)\
        .order_by(Slide.order_index).all()
    if insert_at is not None and insert_at >= 0:
        insert_idx = min(insert_at, len(existing))
    else:
        insert_idx = len(existing)
    return insert_idx, existing


@router.post("/{project_id}/slides/route", response_model=SlideRead)
def create_route_slide(
    project_id: str,
    request: RouteSlideRequest,
    db: Session = Depends(get_db),
):
    """출발지→도착지 경로 애니메이션 슬라이드 생성 (OSRM + staticmap)."""
    project = _get_project_or_404(project_id, db)
    assets_dir = _assets_dir(project_id)

    try:
        return route_slide_service.create_route_slide(
            db, project, assets_dir,
            origin=request.origin, destination=request.destination,
            profile=request.profile, duration=request.duration,
            n_frames=request.n_frames, insert_at=request.insert_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Route slide 생성 실패: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"경로 정보 조회 실패: {e}")


@router.post("/{project_id}/slides/{slide_id}/route/regenerate", response_model=SlideRead)
def regenerate_route_slide(
    project_id: str,
    slide_id: str,
    request: RouteRegenerateRequest,
    db: Session = Depends(get_db),
):
    """기존 route 슬라이드를 저장된 출발/도착 좌표로 재생성.
    profile·n_frames·duration 만 갱신(미지정 시 기존값 유지). 현재 aspect_ratio 로 캔버스 재계산."""
    project = _get_project_or_404(project_id, db)
    slide = db.query(Slide).filter(Slide.id == slide_id, Slide.project_id == project_id).first()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    if slide.slide_type != "route":
        raise HTTPException(status_code=400, detail="route 슬라이드만 재생성할 수 있습니다")

    try:
        old = json.loads(slide.meta) if slide.meta else {}
    except Exception:
        old = {}
    origin = old.get("origin")
    destination = old.get("destination")
    if not (origin and destination and "lat" in origin and "lat" in destination):
        raise HTTPException(status_code=400, detail="경로 좌표 메타가 손상되어 재생성할 수 없습니다")

    profile = request.profile if request.profile in ("driving", "foot", "walking", "bike", "bicycle") \
        else old.get("profile", "driving")
    n_frames = max(6, min(60, int(request.n_frames if request.n_frames is not None else old.get("n_frames", 30))))
    duration = float(request.duration if request.duration is not None else old.get("duration", 5.0))

    assets_dir = _assets_dir(project_id)
    # 기존 프레임 삭제 (slide_id prefix 유지→같은 이름으로 덮어쓰기, 삭제로 누락 방지)
    _delete_route_frames(assets_dir, slide.meta)

    try:
        route = map_service.get_route(origin, destination, profile=profile)
        canvas = _canvas_for(project)
        frames = map_service.render_route_frames(
            route["geometry_lnglat"], n_frames, canvas, assets_dir, slide.id,
            distance_m=route["distance_m"], duration_s=route["duration_s"], profile=route["profile"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Route slide 재생성 실패: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"경로 재생성 실패: {e}")

    fps = (n_frames - 1) / duration if duration > 0 else 6.0
    meta = {
        "type": "route",
        "origin": origin,
        "destination": destination,
        "profile": route["profile"],
        "geometry_lnglat": route["geometry_lnglat"],
        "distance_m": route["distance_m"],
        "duration_s": route["duration_s"],
        "frames": frames,
        "n_frames": n_frames,
        "duration": duration,
        "fps": fps,
        "canvas": [canvas[0], canvas[1]],
    }
    slide.meta = json.dumps(meta, ensure_ascii=False)
    slide.image_filename = frames[0] if frames else ""
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(slide)
    logger.info(f"[{project_id}] Route slide regenerated: {slide_id} ({profile}, {n_frames} frames)")
    return slide


@router.post("/{project_id}/slides/place", response_model=SlideRead)
def create_place_slide(
    project_id: str,
    request: PlaceSlideRequest,
    db: Session = Depends(get_db),
):
    """장소 카드 슬라이드 생성 (네이버 실데이터 + Gemini 설명 + 지도/패널 합성)."""
    project = _get_project_or_404(project_id, db)
    assets_dir = _assets_dir(project_id)

    try:
        details = map_service.get_place_details(request.query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Place slide 생성 실패: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"장소 정보 조회 실패: {e}")

    # 네이버 실데이터 + Gemini 설명 + 장소 사진 (모두 키 없으면 graceful degrade)
    geo = {"name": details["name"], "lat": details["lat"], "lng": details["lng"],
           "display_name": details.get("address", "")}
    naver = place_info.fetch_naver_local(request.query)
    desc = place_info.generate_description(request.query, naver, geo)
    photo_path = place_info.fetch_place_photo(request.query, details["name"])

    import uuid as _uuid
    slide_id = str(_uuid.uuid4())
    canvas = _canvas_for(project)
    map_filename = f"{slide_id}_place.png"
    try:
        map_service.render_place_card(details, naver, desc, canvas, assets_dir / map_filename,
                                      photo_path=photo_path)
    except Exception as e:
        logger.error(f"Place card 렌더링 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"장소 카드 생성 실패: {e}")
    finally:
        # 임시 사진 파일 정리
        if photo_path:
            try:
                import os as _os
                if _os.path.exists(photo_path):
                    _os.unlink(photo_path)
            except Exception:
                pass

    # 내레이션: Gemini 개요 첫 문장 + 장소명 (없으면 기본 문장)
    display_name = (naver or {}).get("title") or details["name"]
    overview = (desc.get("overview") or "").strip()
    first_sent = re.split(r"[.!?]\s*", overview)[0].strip() if overview else ""
    text = f"{display_name}입니다. {first_sent}" if first_sent else f"{display_name}을(를) 방문했습니다."
    text = text[:300]

    meta = {
        "type": "place",
        "name": details["name"],
        "display_name": display_name,
        "address": (naver or {}).get("road_address") or (naver or {}).get("address") or details.get("address", ""),
        "lat": details["lat"],
        "lng": details["lng"],
        "category": (naver or {}).get("category") or details.get("category", ""),
        "opening_hours": details.get("opening_hours", ""),
        "canvas": [canvas[0], canvas[1]],
        "naver": naver,
        "description": desc,
    }

    insert_idx, existing = _insert_order_index(db, project_id, request.insert_at)
    slide = Slide(
        id=slide_id,
        project_id=project_id,
        order_index=insert_idx,
        image_filename=map_filename,
        label=display_name,
        text=text,
        slide_type="place",
        use_tts=1,
        meta=json.dumps(meta, ensure_ascii=False),
    )
    db.add(slide)
    combined = existing[:insert_idx] + [slide] + existing[insert_idx:]
    for idx, s in enumerate(combined):
        s.order_index = idx
    project.stage = "uploaded"
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(slide)
    logger.info(f"[{project_id}] Place slide created: {details['name']}")
    return slide


# ─────────────────────────────────────────────
# 갤러리 이미지 콜라주 생성
# ─────────────────────────────────────────────
from PIL import Image
from pydantic import BaseModel


class CollageRequest(BaseModel):
    slide_ids: List[str]
    layout: str = "auto"  # 'auto' | 'horizontal' | '2x2' | '3x1' | '1x3'

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

    # 콜라주 병합 — 레이아웃별 그리드 배치
    layout = request.layout or "auto"
    n = len(images)

    # auto: 2장=가로, 3장=3x1, 4장=2x2
    if layout == "auto":
        if n == 4:
            layout = "2x2"
        elif n == 3:
            layout = "3x1"
        else:
            layout = "horizontal"

    if layout == "2x2":
        cols, rows = 2, 2
    elif layout == "3x1":
        cols, rows = 3, 1
    elif layout == "1x3":
        cols, rows = 1, 3
    else:  # horizontal
        cols, rows = n, 1

    # 셀 크기 적응형: 선택 이미지들의 평균 가로세로 비율로 셀 방향 결정
    #   가로 위주 → 640×480 / 세로 위주 → 480×640 / 혼합·정방형 → 560×560
    mean_ratio = sum(im.width / im.height for im in images) / len(images)
    if mean_ratio >= 1.2:
        CELL_W, CELL_H = 640, 480
    elif mean_ratio <= 0.8:
        CELL_W, CELL_H = 480, 640
    else:
        CELL_W, CELL_H = 560, 560
    GAP = 4

    canvas_w = cols * CELL_W + (cols - 1) * GAP
    canvas_h = rows * CELL_H + (rows - 1) * GAP
    collage_im = Image.new('RGBA', (canvas_w, canvas_h), (0, 0, 0, 255))

    for idx, im in enumerate(images):
        if idx >= cols * rows:
            break
        col = idx % cols
        row = idx // cols
        # 셀을 가득 채우기(cover): 비율 맞춰 확대 후 중앙 크롭 — 셀 내 여백 없음
        im_ratio = im.width / im.height
        cell_ratio = CELL_W / CELL_H
        if im_ratio > cell_ratio:
            new_h = CELL_H
            new_w = int(CELL_H * im_ratio)
        else:
            new_w = CELL_W
            new_h = int(CELL_W / im_ratio)
        resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = max(0, (new_w - CELL_W) // 2)
        top = max(0, (new_h - CELL_H) // 2)
        cropped = resized.crop((left, top, left + CELL_W, top + CELL_H))
        collage_im.paste(cropped, (col * (CELL_W + GAP), row * (CELL_H + GAP)))

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

