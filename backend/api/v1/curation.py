"""
DocuMotion - 스마트 사진 선별 API (Photo-Vlog F1)

  POST /projects/{id}/curation/run    → 202, 백그라운드 분석 시작 (중복 실행 409)
  GET  /projects/{id}/curation/status → 진행률 + 결과 제안
  POST /projects/{id}/curation/apply  → 수락한 slide_ids 일괄 삭제 (원자적)

모든 제안은 사용자가 체크한 slide_ids 로만 반영된다. 상태는 in-memory
(task_status) — 재시작 시 404 → 프론트 "다시 시도" 안내.
"""
import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.services import curation, task_status

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["curation"])

# project_id 단위 단일 실행 가드 (task_status에 status로 판별)
TASK_KEY = "curation:{project_id}"


def _get_project_or_404(project_id: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _run_curation(project_id: str, photo_specs: list[dict]):
    task_id = TASK_KEY.format(project_id=project_id)
    try:
        def cb(frac):
            task_status.set_status(task_id, status="running", progress=round(frac * 100, 1))
        result = curation.curate_photos(photo_specs, progress_cb=cb)
        task_status.set_status(task_id, status="done", progress=100.0, result=result)
    except Exception as e:
        logger.error(f"스마트 선별 실패({project_id}): {e}", exc_info=True)
        task_status.set_status(task_id, status="error", message=str(e))


@router.post("/{project_id}/curation/run")
def run_curation(project_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    """이미지 슬라이드 전체 분석 — 202 즉시 반환, 결과는 status 폴링."""
    _get_project_or_404(project_id, db)
    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id)
    if st and st.get("status") == "running":
        raise HTTPException(status_code=409, detail="이미 선별 작업이 진행 중입니다")

    assets_dir = OUTPUTS_DIR / project_id / "assets"
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id, Slide.slide_type == "image")
              .order_by(Slide.order_index).all())
    specs = [{"slide_id": s.id, "path": str(assets_dir / s.image_filename), "exif": s.exif}
             for s in slides if s.image_filename and (assets_dir / s.image_filename).exists()]
    if not specs:
        raise HTTPException(status_code=400, detail="분석할 이미지 슬라이드가 없습니다")

    task_status.set_status(task_id, status="running", progress=0.0, done=0, total=len(specs))
    background.add_task(_run_curation, project_id, specs)
    return {"status": "started", "total": len(specs)}


@router.get("/{project_id}/curation/status")
def curation_status(project_id: str, db: Session = Depends(get_db)):
    _get_project_or_404(project_id, db)
    st = task_status.get_status(TASK_KEY.format(project_id=project_id))
    if not st:
        raise HTTPException(status_code=404, detail="진행 중인 선별 작업이 없습니다 (재시도 해주세요)")
    # 제안에 썸네일 파일명/라벨 부여
    result = st.get("result")
    if result and result.get("suggestions"):
        ids = [s["slide_id"] for s in result["suggestions"]]
        by = {s.id: s for s in db.query(Slide).filter(Slide.id.in_(ids)).all()}
        for sug in result["suggestions"]:
            s = by.get(sug["slide_id"])
            sug["image_filename"] = s.image_filename if s else ""
            sug["label"] = s.label if s else ""
    return st


class ApplyRequest(BaseModel):
    slide_ids: list[str]


@router.post("/{project_id}/curation/apply")
def apply_curation(project_id: str, request: ApplyRequest, db: Session = Depends(get_db)):
    """사용자가 수락한 슬라이드만 원자적으로 삭제 (자산 파일 포함)."""
    project = _get_project_or_404(project_id, db)
    if not request.slide_ids:
        raise HTTPException(status_code=400, detail="삭제할 슬라이드가 선택되지 않았습니다")
    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id, Slide.id.in_(request.slide_ids)).all())
    if not slides:
        raise HTTPException(status_code=404, detail="해당 슬라이드를 찾을 수 없습니다")
    assets_dir = OUTPUTS_DIR / project_id / "assets"
    for s in slides:
        if s.image_filename:
            p = assets_dir / s.image_filename
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    logger.warning(f"자산 삭제 실패: {p}")
        db.delete(s)
    project.updated_at = datetime.utcnow()
    db.commit()
    # 남은 슬라이드 order_index 정리 + 상태 초기화
    remaining = (db.query(Slide).filter(Slide.project_id == project_id)
                 .order_by(Slide.order_index).all())
    for idx, s in enumerate(remaining):
        s.order_index = idx
    db.commit()
    task_status.clear(TASK_KEY.format(project_id=project_id))
    return {"ok": True, "deleted": len(slides), "remaining": len(remaining)}
