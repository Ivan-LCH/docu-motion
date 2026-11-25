"""
DocuMotion - YouTube 업로드 & OAuth 인증 API

  GET  /youtube/auth-status                {authenticated, reason, scopes_ok, channel}
  GET  /youtube/auth-url · POST /youtube/exchange-code   (코드 붙여넣기 OAuth)
  GET  /projects/{id}/youtube/meta         저장된 메타 또는 자동 생성 (?regen=1&ai=0|1)
  PUT  /projects/{id}/youtube/meta         수정본 저장
  POST /projects/{id}/youtube/thumbnail    썸네일 이미지 업로드 → yt_thumb.jpg
  GET  /projects/{id}/youtube/thumbnail    업로드한 썸네일
  POST /projects/{id}/youtube/upload       202 queued (백그라운드 업로드)
  GET  /projects/{id}/youtube/status       업로드 진행 상태
"""
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core import config
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db
from backend.schema.project import YouTubeMetaUpdate, YouTubeUploadRequest
from backend.services import youtube_job, youtube_manager, youtube_meta
from backend.services.music_credit import credit_description
from backend.services.presets import project_meta

router = APIRouter(tags=["youtube"])
logger = get_logger(__name__)

OAUTH_REDIRECT = "http://localhost"
MAX_THUMB_CANDIDATES = 6
MAX_THUMB_UPLOAD = 20 * 1024 * 1024


# ─── 헬퍼 ───────────────────────────────────────
def _project(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _slides(db: Session, project_id: str) -> list:
    return db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()


def _thumb_candidates(project_id: str, slides: list) -> list[dict]:
    """썸네일 후보: 파일이 있는 이미지 슬라이드 중 고르게 최대 6장. index 는 전체 슬라이드 순번."""
    assets = Path(config.OUTPUTS_DIR) / project_id / "assets"
    pool = [(i, s) for i, s in enumerate(slides)
            if (s.slide_type or "image") == "image" and s.image_filename
            and (assets / s.image_filename).is_file()]
    if len(pool) > MAX_THUMB_CANDIDATES:
        step = (len(pool) - 1) / (MAX_THUMB_CANDIDATES - 1)
        pool = [pool[round(k * step)] for k in range(MAX_THUMB_CANDIDATES)]
    return [{"index": i, "slide_id": s.id,
             "url": f"/api/v1/projects/{project_id}/assets/{s.image_filename}"} for i, s in pool]


def _uploaded_thumb_url(project_id: str):
    p = Path(config.OUTPUTS_DIR) / project_id / youtube_job.THUMB_FILE
    return f"/api/v1/projects/{project_id}/youtube/thumbnail?v={int(p.stat().st_mtime)}" if p.is_file() else None


def _auth_http_error(reason: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": reason, "message": youtube_job.auth_message(reason)})


# ─── 인증 ───────────────────────────────────────
@router.get("/youtube/auth-status")
def youtube_auth_status():
    """현재 YouTube 인증 상태 (스코프 확인 포함, 파일 쓰기 없음)"""
    return youtube_manager.get_auth_status()


@router.get("/youtube/auth-url")
def youtube_auth_url():
    """OAuth2 인증 URL 생성 후 반환"""
    try:
        return {"auth_url": youtube_manager.generate_auth_url(OAUTH_REDIRECT)}
    except Exception as e:
        logger.error(f"Auth URL generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class _CodeExchangeRequest(BaseModel):
    code: str


@router.post("/youtube/exchange-code")
def youtube_exchange_code(req: _CodeExchangeRequest):
    """수동으로 입력받은 authorization code → token 교환"""
    try:
        youtube_manager.exchange_code(req.code, OAUTH_REDIRECT)
        logger.info("YouTube OAuth code exchange success")
        return {"ok": True}
    except Exception as e:
        logger.error(f"YouTube code exchange failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ─── 메타데이터 ─────────────────────────────────
def _meta_response(project_id: str, meta: dict, slides: list) -> dict:
    credit = project_meta(slides).get("bgm_credit")
    return {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "tags": list(meta.get("tags") or []),
        "privacy": meta.get("privacy", "private"),
        "thumbnail": meta.get("thumbnail", "none"),
        "source": meta.get("source", "saved"),
        "credit": credit_description(credit if isinstance(credit, dict) else None),
        "thumbnails": _thumb_candidates(project_id, slides),
        "uploaded_thumbnail": _uploaded_thumb_url(project_id),
        "tags_limit": youtube_manager.TAGS_TOTAL_MAX,
    }


@router.get("/projects/{project_id}/youtube/meta")
def get_youtube_meta(project_id: str, regen: int = 0, ai: int = 1, db: Session = Depends(get_db)):
    """저장된 메타가 있으면 그대로, 없거나 regen=1 이면 새로 만들어 저장"""
    project = _project(db, project_id)
    slides = _slides(db, project_id)
    meta = None if regen else youtube_job.load_meta(project_id)
    if not meta:
        meta = youtube_meta.build_metadata(project, slides, use_ai=bool(ai))
        meta.pop("credit", None)
        old = youtube_job.load_meta(project_id) or {}
        meta["thumbnail"] = old.get("thumbnail", "none")
        if old.get("privacy") in youtube_manager.PRIVACY_VALUES:
            meta["privacy"] = old["privacy"]
        youtube_job.save_meta(project_id, meta)
    return _meta_response(project_id, meta, slides)


@router.put("/projects/{project_id}/youtube/meta")
def put_youtube_meta(project_id: str, req: YouTubeMetaUpdate, db: Session = Depends(get_db)):
    """사용자가 고친 제목·설명·태그·공개 범위·썸네일 선택 저장"""
    project = _project(db, project_id)
    meta = {
        "title": youtube_manager.clean_title(req.title, project.name),
        "description": youtube_manager.clean_description(req.description),
        "tags": youtube_manager.clean_tags(req.tags),
        "privacy": req.privacy,
        "thumbnail": req.thumbnail or "none",
        "source": "edited",
    }
    youtube_job.save_meta(project_id, meta)
    return _meta_response(project_id, meta, _slides(db, project_id))


# ─── 썸네일 ─────────────────────────────────────
@router.post("/projects/{project_id}/youtube/thumbnail")
async def upload_youtube_thumbnail(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """이미지 → JPEG 1280x720 (cover-fit, 2MB 미만) → outputs/{id}/yt_thumb.jpg"""
    _project(db, project_id)
    data = await file.read(MAX_THUMB_UPLOAD + 1)
    if len(data) > MAX_THUMB_UPLOAD:
        raise HTTPException(status_code=413, detail="이미지가 너무 커요 (20MB 이하)")
    import io
    dest = Path(config.OUTPUTS_DIR) / project_id / youtube_job.THUMB_FILE
    try:
        youtube_job.make_thumbnail(io.BytesIO(data), dest)
    except Exception:
        raise HTTPException(status_code=400, detail="이미지를 읽지 못했어요")
    return {"ok": True, "url": _uploaded_thumb_url(project_id)}


@router.get("/projects/{project_id}/youtube/thumbnail")
def get_youtube_thumbnail(project_id: str):
    p = Path(config.OUTPUTS_DIR) / project_id / youtube_job.THUMB_FILE
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(str(p), media_type="image/jpeg")


# ─── 업로드 ─────────────────────────────────────
@router.post("/projects/{project_id}/youtube/upload", status_code=202)
def upload_to_youtube(project_id: str, payload: YouTubeUploadRequest, background_tasks: BackgroundTasks,
                      db: Session = Depends(get_db)):
    """YouTube 업로드 예약 (202). 진행은 GET .../youtube/status"""
    project = _project(db, project_id)
    video_path = Path(config.OUTPUTS_DIR) / project_id / "result.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found. Render first.")
    if youtube_job.is_active(project_id):
        raise HTTPException(status_code=409, detail="이미 업로드 중이에요")
    try:
        youtube_manager.check_ready()
    except PermissionError as e:
        raise _auth_http_error(str(e))
    except Exception as e:
        logger.error(f"YouTube auth check failed: {e}")
        raise HTTPException(status_code=503, detail="YouTube 인증 서버에 연결하지 못했어요")

    slides = _slides(db, project_id)
    saved = youtube_job.load_meta(project_id) or {}
    title = payload.title if payload.title is not None else saved.get("title")
    description = payload.description if payload.description is not None else saved.get("description", "")
    tags = payload.tags if payload.tags is not None else saved.get("tags")
    meta = {
        "title": youtube_manager.clean_title(title, project.name),
        "description": youtube_manager.clean_description(description),
        "tags": youtube_manager.clean_tags(tags),
        "privacy": payload.privacy,
        "thumbnail": payload.thumbnail or "none",
        "source": saved.get("source", "edited"),
    }
    youtube_job.save_meta(project_id, meta)
    thumb = youtube_job.resolve_thumbnail(project_id, meta["thumbnail"], slides)

    if not youtube_job.try_begin(project_id, payload.privacy):
        raise HTTPException(status_code=409, detail="이미 업로드 중이에요")
    background_tasks.add_task(youtube_job.run_upload, project_id, {
        **meta, "thumbnail_path": str(thumb) if thumb else None, "fallback_title": project.name})
    logger.info(f"YouTube upload queued: {project_id} ({payload.privacy})")
    return JSONResponse(status_code=202, content={"ok": True, **youtube_job.get_state(project_id)})


@router.get("/projects/{project_id}/youtube/status")
def youtube_upload_status(project_id: str):
    return youtube_job.get_state(project_id)
