"""
DocuMotion - 슬라이드 미리보기 API (6-19)
  POST /projects/{pid}/slides/{sid}/preview   → 캐시 hit: 200 / miss: 백그라운드 렌더 + 202
  GET  /projects/{pid}/slides/{sid}/preview    → 캐시 mp4 서빙(Range) / 없으면 202 pending
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.core.logger import get_logger
from backend.services import preview as preview_service

router = APIRouter(prefix="/projects", tags=["preview"])
logger = get_logger(__name__)

MIN_MP4 = preview_service.MIN_VALID_MP4_BYTES


class PreviewRequest(BaseModel):
    include_neighbors: bool = False
    force_tts: bool = False


def _resolve(db: Session, project_id: str, slide_id: str,
             include_neighbors: bool, force_tts: bool):
    ctx = preview_service.compute_context(
        db, project_id, slide_id, include_neighbors, force_tts)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Project or slide not found")
    return ctx


def _is_ready(path) -> bool:
    return path.exists() and path.stat().st_size >= MIN_MP4


@router.post("/{project_id}/slides/{slide_id}/preview")
def request_preview(
    project_id: str,
    slide_id: str,
    body: PreviewRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """슬라이드 미리보기 요청 — 캐시 hit 시 즉시 200, miss 시 백그라운드 렌더 후 202."""
    ctx = _resolve(db, project_id, slide_id, body.include_neighbors, body.force_tts)
    cache = ctx["cache_path"]

    if _is_ready(cache):
        return {"status": "ready", "cached": True, "hash": ctx["hash"]}

    key = preview_service.inflight_key(project_id, slide_id, ctx["hash"])
    if not preview_service.is_inflight(key):
        preview_service.mark_inflight(key)
        background_tasks.add_task(
            preview_service.render_and_clear,
            project_id, slide_id, body.include_neighbors, body.force_tts, key,
        )
        logger.info(f"Preview queued: {project_id}/{slide_id} hash={ctx['hash']}"
                    f" neighbors={body.include_neighbors} force_tts={body.force_tts}")

    return JSONResponse(
        {"status": "rendering", "cached": False, "hash": ctx["hash"]},
        status_code=202,
    )


@router.get("/{project_id}/slides/{slide_id}/preview")
def serve_preview(
    project_id: str,
    slide_id: str,
    include_neighbors: bool = False,
    force_tts: bool = False,
    db: Session = Depends(get_db),
):
    """캐시된 미리보기 mp4 서빙(Range 요청 지원). 아직 없으면 202 pending."""
    ctx = _resolve(db, project_id, slide_id, include_neighbors, force_tts)
    cache = ctx["cache_path"]

    if _is_ready(cache):
        return FileResponse(
            str(cache),
            media_type="video/mp4",
            headers={"Cache-Control": "public, max-age=60",
                     "Accept-Ranges": "bytes"},
        )
    return JSONResponse({"status": "pending", "hash": ctx["hash"]}, status_code=202)
