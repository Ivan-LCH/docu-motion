"""
DocuMotion - EXIF 자동 구성 API (Photo-Vlog F3)

사진 촬영 메타데이터 기반:
  POST /projects/{id}/slides/exif/scan    — 기존 슬라이드 EXIF 백필 (동기)
  GET  /projects/{id}/organize/suggestions — 시간순 정렬/경로 삽입 제안 조회
  POST /projects/{id}/organize/sort        — 촬영시각순 정렬 적용
  POST /projects/{id}/slides/sort-by-capture-time — 모든 슬라이드(사진·영상 섞어서) 촬영 시각 오래된 순
  POST /projects/{id}/organize/insert-route — 제안 수락 → 경로 슬라이드 삽입

모든 변경은 사용자가 명시적으로 수락하는 요청에 의해서만 일어난다.
"""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.logger import get_logger
from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.services.exif_service import extract_exif, analyze_timeline, DEFAULT_GAP_KM
from backend.services import capture_time, media_import, route_slide_service

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["organize"])


def _get_project_or_404(project_id: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _assets_dir(project_id: str):
    from backend.core.config import OUTPUTS_DIR
    d = OUTPUTS_DIR / project_id / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slide_photo(s: Slide) -> dict:
    """Slide ORM → analyze_timeline 입력 포맷."""
    try:
        exif = json.loads(s.exif) if s.exif else {}
    except Exception:
        exif = {}
    return {
        "slide_id": s.id,
        "order_index": s.order_index,
        "captured_at": exif.get("captured_at"),
        "gps": exif.get("gps"),
    }


# ── EXIF 백필 스캔 ────────────────────────────────────────────────────────
@router.post("/{project_id}/slides/exif/scan")
def scan_exif(project_id: str, db: Session = Depends(get_db)):
    """기존 업로드 사진의 EXIF를 읽어 Slide.exif 컬럼에 백필. PIL 헤더 읽기라 동기로 충분히 빠름."""
    _get_project_or_404(project_id, db)
    assets_dir = _assets_dir(project_id)
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id, Slide.slide_type == "image")
              .order_by(Slide.order_index).all())
    scanned = with_time = with_gps = 0
    for s in slides:
        if not s.image_filename:
            continue
        path = assets_dir / s.image_filename
        if not path.exists():
            continue
        data = extract_exif(path)
        s.exif = json.dumps(data, ensure_ascii=False) if (data.get("captured_at") or data.get("gps")) else "{}"
        scanned += 1
        with_time += 1 if data.get("captured_at") else 0
        with_gps += 1 if data.get("gps") else 0
    db.commit()
    return {"scanned": scanned, "with_time": with_time, "with_gps": with_gps}


# ── 제안 조회 ────────────────────────────────────────────────────────────
@router.get("/{project_id}/organize/suggestions")
def get_suggestions(project_id: str, gap_km: float = DEFAULT_GAP_KM, db: Session = Depends(get_db)):
    """시간순 정렬 필요 여부 + GPS 이동 구간 경로 삽입 제안."""
    _get_project_or_404(project_id, db)
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id, Slide.slide_type == "image")
              .order_by(Slide.order_index).all())
    photos = [_slide_photo(s) for s in slides]
    result = analyze_timeline(photos, gap_km=gap_km)
    # 프론트 라벨용: 제안 지점의 사진 파일명
    by_id = {s.id: s for s in slides}
    for r in result["routes"]:
        s = by_id.get(r["after_slide_id"])
        r["after_image_filename"] = s.image_filename if s else ""
    return result


# ── 시간순 정렬 적용 ──────────────────────────────────────────────────────
@router.post("/{project_id}/organize/sort")
def apply_sort(project_id: str, db: Session = Depends(get_db)):
    """전체 슬라이드를 이미지의 촬영시각 기준으로 재정렬.
    시각 없는 이미지/비이미지(route·place·video) 슬라이드는 기존 상대 순서를 유지한 채 원래 위치에 둔다."""
    project = _get_project_or_404(project_id, db)
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())

    # 원래 위치를 키로 삼아 시각 있는 사진만 시간순 재배치 (stable)
    with_time = [(i, s) for i, s in enumerate(slides)
                 if s.slide_type == "image" and (_slide_photo(s).get("captured_at"))]
    if len(with_time) < 2:
        raise HTTPException(status_code=400, detail="촬영시각이 있는 사진이 2장 이상 필요합니다 (EXIF 스캔 먼저)")
    positions = [i for i, _ in with_time]
    sorted_slides = sorted((s for _, s in with_time), key=lambda s: _slide_photo(s)["captured_at"])
    for pos, s in zip(positions, sorted_slides):
        slides[pos] = s
    for idx, s in enumerate(slides):
        s.order_index = idx
    project.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "reordered": len(sorted_slides)}


# ── 촬영 시각순 재정렬 (사진·영상 공통) ────────────────────────────────────
def _slide_capture_key(s: Slide, assets_dir) -> Optional[datetime]:
    """저장된 시각(exif.captured_utc → captured_at) → 파일명(원본 label, 저장 이름) → 파일(EXIF / ffprobe)."""
    try:
        exif = json.loads(s.exif) if s.exif else {}
    except Exception:
        exif = {}
    is_video = s.slide_type == "video"
    fname = (s.video_filename if is_video else s.image_filename) or ""
    if s.slide_type not in ("image", "video") or not fname:
        return None                              # 장소·경로 슬라이드 등 → 앞 슬라이드에 붙어 간다

    def read_file() -> Optional[datetime]:
        path = assets_dir / fname
        if not path.is_file():
            return None
        if is_video:                             # ffprobe creation_time (UTC 기준 naive)
            return capture_time.parse_iso_utc(media_import.ffprobe(path).get("creation_time"))
        return capture_time.parse_iso_utc(extract_exif(path).get("captured_at"), capture_time.LOCAL_TZ)

    return capture_time.slide_capture_key(
        exif, is_video, [s.label or "", capture_time.strip_import_stamp(fname)], read_file)


@router.post("/{project_id}/slides/sort-by-capture-time")
def sort_by_capture_time(project_id: str, db: Session = Depends(get_db)):
    """전체 슬라이드를 촬영 시각 오래된 순으로 (사진·영상 섞어서). 시각을 모르는 슬라이드는
    원래 순서에서 바로 앞 슬라이드에 붙어 상대 순서를 유지한다 (맨 앞이면 맨 앞) — capture_time.sort_by_capture."""
    project = _get_project_or_404(project_id, db)
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())
    assets_dir = _assets_dir(project_id)
    keys = {s.id: _slide_capture_key(s, assets_dir) for s in slides}
    ordered = capture_time.sort_by_capture(slides, lambda s: keys[s.id])
    changed = [s.id for s in ordered] != [s.id for s in slides]
    for idx, s in enumerate(ordered):
        s.order_index = idx
    if changed:
        project.updated_at = datetime.utcnow()
    db.commit()
    timed = sum(1 for k in keys.values() if k is not None)
    return {"ok": True, "changed": changed, "with_time": timed, "without_time": len(slides) - timed,
            "order": [s.id for s in ordered]}


# ── 경로 슬라이드 삽입 ────────────────────────────────────────────────────
class InsertRouteRequest(BaseModel):
    after_slide_id: str
    origin: dict          # {lat, lng, name?} — suggestions 의 from
    destination: dict     # {lat, lng, name?} — suggestions 의 to
    profile: str = "driving"
    duration: float = 5.0
    n_frames: int = 30


@router.post("/{project_id}/organize/insert-route")
def insert_route(project_id: str, request: InsertRouteRequest, db: Session = Depends(get_db)):
    """제안 수락 → 해당 위치 뒤에 경로 슬라이드 생성 (route_slide_service 재사용)."""
    project = _get_project_or_404(project_id, db)
    anchor = db.query(Slide).filter(Slide.id == request.after_slide_id,
                                    Slide.project_id == project_id).first()
    if not anchor:
        raise HTTPException(status_code=404, detail="기준 슬라이드를 찾을 수 없습니다")
    try:
        slide = route_slide_service.create_route_slide(
            db, project, _assets_dir(project_id),
            origin=request.origin, destination=request.destination,
            profile=request.profile, duration=request.duration,
            n_frames=request.n_frames, insert_at=anchor.order_index + 1,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Auto route slide 생성 실패: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"경로 슬라이드 생성 실패: {e}")
    return {"ok": True, "slide_id": slide.id, "label": slide.label}
