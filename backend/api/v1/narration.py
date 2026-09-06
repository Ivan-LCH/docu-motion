"""
DocuMotion - AI 나레이션 API

- POST /projects/{pid}/slides/{sid}/narration        : 슬라이드 1장 나레이션 생성
- POST /projects/{pid}/narration/generate-all        : 전체 이미지 슬라이드 일괄 생성 (비동기, F2)
- GET  /projects/{pid}/narration/generate-all/status : 일괄 생성 진행률 (F2)
- POST /projects/{pid}/narration/split               : 긴 스크립트를 슬라이드 수로 자동 분할
"""
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db, SessionLocal
from backend.schema.project import SlideRead
from backend.services import narration
from backend.services import task_status
from backend.core.config import OUTPUTS_DIR, GOOGLE_API_KEY

logger = get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["narration"])

TASK_KEY = "narration:{project_id}"


def _assets_dir(project_id: str):
    return OUTPUTS_DIR / project_id / "assets"


class SplitRequest(BaseModel):
    script: str


class GenerateAllRequest(BaseModel):
    overwrite: bool = False  # True면 기존 대사도 덮어씀
    tone: str = "documentary"  # "documentary" | "vlog" (F2)


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


@router.post("/{project_id}/narration/generate-all", status_code=202)
def generate_all_narrations(project_id: str, req: GenerateAllRequest,
                            background: BackgroundTasks, db: Session = Depends(get_db)):
    """이미지 슬라이드 전체에 나레이션 일괄 생성 (비동기). 기본은 대사가 비어있는 슬라이드만."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_id = TASK_KEY.format(project_id=project_id)
    st = task_status.get_status(task_id)
    if st and st.get("status") == "running":
        raise HTTPException(status_code=409, detail="이미 나레이션 생성이 진행 중입니다")

    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id, Slide.image_filename != "")
              .order_by(Slide.order_index).all())
    targets = [s for s in slides if req.overwrite or not (s.text or "").strip()]
    if not targets:
        return {"status": "nothing_to_do", "generated": 0, "failed": 0, "skipped": len(slides)}

    task_status.set_status(task_id, status="running", progress=0.0,
                           done=0, total=len(targets), failed=0, skipped=len(slides) - len(targets))
    background.add_task(_run_generate_all, project_id, project.name,
                        [s.id for s in targets], req.tone)
    return {"status": "started", "total": len(targets), "skipped": len(slides) - len(targets)}


def _run_generate_all(project_id: str, project_title: str, slide_ids: list[str], tone: str):
    """백그라운드 본문 — 슬라이드별 커밋(부분 진행 생존) + 0.3s 간격(RPM 절약)."""
    task_id = TASK_KEY.format(project_id=project_id)
    if not GOOGLE_API_KEY:
        task_status.set_status(task_id, status="error", message="GOOGLE_API_KEY 가 설정되지 않았습니다")
        return
    done = failed = 0
    assets = _assets_dir(project_id)
    for sid in slide_ids:
        db = SessionLocal()
        try:
            slide = db.query(Slide).filter(Slide.id == sid).first()
            text = ""
            if slide and slide.image_filename:
                text = narration.generate_narration_for_image(
                    assets / slide.image_filename, project_title=project_title, tone=tone)
            if text:
                slide.text = text
                db.commit()
                done += 1
            else:
                failed += 1
            task_status.set_status(task_id, status="running",
                                   done=done, failed=failed,
                                   progress=round((done + failed) / len(slide_ids) * 100, 1))
        except Exception as e:
            failed += 1
            logger.warning(f"나레이션 생성 실패({sid}): {e}")
            task_status.set_status(task_id, status="running",
                                   done=done, failed=failed,
                                   progress=round((done + failed) / len(slide_ids) * 100, 1))
        finally:
            db.close()
        time.sleep(0.3)
    task_status.set_status(task_id, status="done", done=done, failed=failed, progress=100.0)


@router.get("/{project_id}/narration/generate-all/status")
def generate_all_status(project_id: str, db: Session = Depends(get_db)):
    """일괄 생성 진행률. 완료 시 최종 결과 포함."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    st = task_status.get_status(TASK_KEY.format(project_id=project_id))
    if not st:
        raise HTTPException(status_code=404, detail="진행 중인 나레이션 생성이 없습니다 (재시도 해주세요)")
    return st


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


# ── 오타 검증 ────────────────────────────────────────────────────────────────
class SpellcheckResult(BaseModel):
    slide_id: str
    original: str
    corrected: str
    has_error: bool


class SpellcheckResponse(BaseModel):
    results: list[SpellcheckResult]
    checked: int
    errors: int


class SpellcheckApplyRequest(BaseModel):
    fixes: list[dict]  # [{slide_id, corrected}]


@router.post("/{project_id}/spellcheck", response_model=SpellcheckResponse)
def spellcheck_project(project_id: str, db: Session = Depends(get_db)):
    """전체 슬라이드 대사 맞춤법/오탈자 검사 (동기 — 제안만 반환, DB 변경 없음)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    slides = (db.query(Slide)
              .filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())
    items = [{"id": i, "text": s.text}
             for i, s in enumerate(slides) if (s.text or "").strip()]
    if not items:
        raise HTTPException(status_code=400, detail="검사할 대사가 없습니다")

    results = narration.spellcheck_texts(items)
    if results is None:
        raise HTTPException(status_code=502, detail="오타 검증 실패 (API 키/네트워크 확인)")

    id_to_slide = {i: s for i, s in enumerate(slides)}
    out = [SpellcheckResult(slide_id=id_to_slide[r["id"]].id,
                            original=r["original"], corrected=r["corrected"],
                            has_error=r["has_error"])
           for r in results]
    return SpellcheckResponse(results=out, checked=len(out),
                              errors=sum(1 for r in out if r.has_error))


@router.post("/{project_id}/spellcheck/apply")
def spellcheck_apply(project_id: str, req: SpellcheckApplyRequest,
                     db: Session = Depends(get_db)):
    """검증 결과 중 사용자가 선택한 수정안만 일괄 적용"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not req.fixes:
        raise HTTPException(status_code=400, detail="적용할 수정안이 없습니다")

    applied = 0
    for fix in req.fixes:
        slide = db.query(Slide).filter(Slide.id == fix.get("slide_id"),
                                       Slide.project_id == project_id).first()
        if slide and (fix.get("corrected") or "").strip():
            slide.text = fix["corrected"].strip()
            applied += 1
    db.commit()
    return {"ok": True, "applied": applied}
