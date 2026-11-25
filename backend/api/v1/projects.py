# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Import
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
import os
import shutil
import httpx
import json
from datetime import datetime

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.session import get_db
from backend.db.models import Project, Slide
from backend.schema.project import ProjectCreate, ProjectRead, ProjectDetail
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.services.color_grade import VALID_STYLE_PRESETS
from backend.services.presets import VALID_TRANSITIONS
from backend.services import music_search


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Router
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
router = APIRouter(prefix="/projects", tags=["projects"])
logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Helper Functions
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
def _enrich_project(project: Project) -> dict:
    """Project ORM 객체를 응답형 dict로 변환"""
    video_path = OUTPUTS_DIR / project.id / "result.mp4"
    return {
        "id"          : project.id,
        "name"        : project.name,
        "status"      : project.status,
        "stage"       : project.stage,
        "progress"    : project.progress,
        "message"     : project.message,
        "has_video"   : video_path.exists(),
        "slide_count" : len(project.slides),
        "created_at"  : project.created_at,
        "updated_at"  : project.updated_at,
        "bgm_filename": getattr(project, "bgm_filename", "") or "",
        "bgm_volume"  : getattr(project, "bgm_volume", 0.3) or 0.3,
        "aspect_ratio": getattr(project, "aspect_ratio", "16:9") or "16:9",
        "tts_master_volume": getattr(project, "tts_master_volume", 1.0) or 1.0,
        "tts_voice": getattr(project, "tts_voice", "") or "",
        "default_transition": getattr(project, "default_transition", "none") or "none",
        "default_slide_duration": getattr(project, "default_slide_duration", 3.0) or 3.0,
        "subtitle_font_size": getattr(project, "subtitle_font_size", 30) or 30,
        "subtitle_font_color": getattr(project, "subtitle_font_color", "white") or "white",
        "watermark_text": getattr(project, "watermark_text", "") or "",
        "watermark_opacity": getattr(project, "watermark_opacity", 0.3) or 0.3,
        "title_text": getattr(project, "title_text", "") or "",
        "resolution": getattr(project, "resolution", "720p") or "720p",
        "transition_duration": getattr(project, "transition_duration", 0.7) or 0.7,
        "style_preset": getattr(project, "style_preset", "none") or "none",
    }


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# API Endpoints
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    """프로젝트 목록 (최신순)"""
    projects = db.query(Project).order_by(Project.updated_at.desc()).all()
    return [_enrich_project(p) for p in projects]



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Create Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("", response_model=ProjectRead, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    """새 프로젝트 생성"""
    project = Project(
        name         = payload.name,
        aspect_ratio = getattr(payload, 'aspect_ratio', '16:9')
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    # outputs 디렉토리 생성
    (OUTPUTS_DIR / project.id / "assets").mkdir(parents=True, exist_ok=True)
    logger.info(f"Project created: {project.id} ({project.name})")
    return _enrich_project(project)



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Get Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, db: Session = Depends(get_db)):
    """프로젝트 상세 조회 (슬라이드 포함)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    result = _enrich_project(project)
    result["slides"] = [
        {
            "id"            : s.id,
            "order_index"   : s.order_index,
            "image_filename": s.image_filename,
            "label"         : s.label,
            "text"          : s.text,
            "slide_type"    : getattr(s, "slide_type", "image"),
            "video_filename": getattr(s, "video_filename", ""),
            "volume"        : getattr(s, "volume", 1.0),
            "subtitles"     : getattr(s, "subtitles", "[]"),
            "use_tts"       : getattr(s, "use_tts", 1),
            "trim_start"    : getattr(s, "trim_start", 0.0),
            "trim_end"      : getattr(s, "trim_end", 0.0),
            "transition"    : getattr(s, "transition", "none"),
            "tts_volume"    : getattr(s, "tts_volume", 1.0),
            "rotation"      : getattr(s, "rotation", 0),
            "overlays"      : getattr(s, "overlays", "[]"),
            "image_fit"     : getattr(s, "image_fit", "cover"),
            "ken_burns"     : getattr(s, "ken_burns", 0),
            "photo_effect"  : getattr(s, "photo_effect", None) or "none",
            "effect_reveal" : getattr(s, "effect_reveal", None) if getattr(s, "effect_reveal", None) is not None else 0.45,
            "meta"          : getattr(s, "meta", "{}"),
            "text_style"    : getattr(s, "text_style", None) or "caption",
        }
        for s in project.slides
    ]
    return result



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Delete Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
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


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Rename Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
from fastapi import UploadFile, File
from pydantic import BaseModel as PydanticBase

class ProjectRename(PydanticBase):
    name: str

@router.patch("/{project_id}/rename", response_model=ProjectRead)
def rename_project(project_id: str, payload: ProjectRename, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    project.name = name
    project.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"Project renamed: {project_id} → {name}")
    return _enrich_project(project)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM / 프로젝트 설정 엔드포인트
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class ProjectSettingsUpdate(PydanticBase):
    bgm_volume: float = 0.3
    aspect_ratio: str = "16:9"
    tts_master_volume: float = 1.0
    tts_voice: Optional[str] = None
    default_transition: Optional[str] = None
    default_slide_duration: Optional[float] = None
    subtitle_font_size: Optional[int] = None
    subtitle_font_color: Optional[str] = None
    watermark_text: Optional[str] = None
    watermark_opacity: Optional[float] = None
    title_text: Optional[str] = None
    resolution: Optional[str] = None
    transition_duration: Optional[float] = None
    style_preset: Optional[str] = None



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Upload BGM
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("/{project_id}/bgm", response_model=ProjectRead)
async def upload_bgm(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """BGM 파일 업로드"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    assets_dir = OUTPUTS_DIR / project_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    
    bgm_path   = assets_dir / f"bgm_{file.filename}"
    content    = await file.read()
    
    with open(bgm_path, "wb") as f:
        f.write(content)
    
    project.bgm_filename = f"bgm_{file.filename}"
    project.updated_at   = datetime.utcnow()
    db.commit()
    
    logger.info(f"BGM uploaded: {project_id} → {project.bgm_filename}")
    return _enrich_project(project)



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Delete BGM
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.delete("/{project_id}/bgm", response_model=ProjectRead)
def delete_bgm(project_id: str, db: Session = Depends(get_db)):
    """BGM 파일 제거"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    if project.bgm_filename:
        bgm_path = OUTPUTS_DIR / project_id / "assets" / project.bgm_filename
        if bgm_path.exists():
            bgm_path.unlink()
    
    project.bgm_filename = ""
    project.updated_at   = datetime.utcnow()
    db.commit()
    return _enrich_project(project)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM Search (Jamendo)
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
def _jamendo_http_error(e: music_search.JamendoError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail())


@router.get("/{project_id}/bgm/search")
async def search_bgm(project_id: str, q: str = "", page: int = 1, per_page: int = 15,
                     instrumental: bool = False,
                     min_duration: Optional[int] = None, max_duration: Optional[int] = None):
    """Jamendo 무료 음악 검색. 한글 검색어는 영어 키워드로 바꿔 검색 (query_used/translated).
    키 없음 → 503 {code: jamendo_key_missing}, 서버 오류 → 502 {code: jamendo_error}, 0건 → empty=true"""
    tr = await music_search.translate_query(q)
    rng = None
    if min_duration is not None or max_duration is not None:
        rng = (min_duration or 0, max_duration or 3600)
    try:
        hits, total = await music_search.jamendo_search(
            tr["query_used"], limit=per_page, offset=(max(1, page) - 1) * per_page,
            instrumental=instrumental, duration_range=rng)
    except music_search.JamendoError as e:
        raise _jamendo_http_error(e)
    return {"total": total, "hits": hits, "empty": not hits, **tr}


def bgm_context_text(project: Project, slides: list) -> str:
    """AI 음악 키워드용 맥락: 장소·느낌(간편 자동) + 나레이션 + 프로젝트 제목."""
    lines = []
    title = (project.title_text or "").strip() or (project.name or "").strip()
    if title:
        lines.append(f"제목: {title}")
    for s in slides:
        try:
            m = json.loads(s.meta or "{}")
            m = m if isinstance(m, dict) else {}
        except Exception:
            m = {}
        cap = " / ".join(str(m.get(k) or "").strip() for k in ("easy_place", "easy_feel")
                         if str(m.get(k) or "").strip())
        if cap:
            lines.append(cap)
        if (s.text or "").strip():
            lines.append(s.text.strip()[:200])
        if len(lines) > 30:
            break
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM AI Suggest (Gemini → keywords → Jamendo)
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("/{project_id}/bgm/suggest")
async def suggest_bgm(project_id: str, db: Session = Depends(get_db)):
    """장소·느낌·나레이션·제목 → 영어 음악 키워드(Gemini → 사전) → Jamendo 검색"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    slides = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    keywords, source = await music_search.ai_keywords(bgm_context_text(project, slides))
    keyword = " ".join(keywords)
    try:
        hits, _ = await music_search.jamendo_search(keyword, limit=15)
        if not hits and len(keywords) > 1:
            hits, _ = await music_search.jamendo_search(keywords[0], limit=15)
    except music_search.JamendoError as e:
        raise _jamendo_http_error(e)
    return {"keyword": keyword, "keywords": keywords, "keywords_source": source,
            "hits": hits, "empty": not hits}


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM Download from URL → set as project BGM
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
class BgmDownloadRequest(BaseModel):
    url: str
    filename: str

@router.post("/{project_id}/bgm/download", response_model=ProjectRead)
async def download_bgm(project_id: str, body: BgmDownloadRequest, db: Session = Depends(get_db)):
    """URL에서 음악 파일 다운로드 후 BGM으로 설정"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    safe_name = "".join(c for c in body.filename if c.isalnum() or c in "-_.")[:80] or "bgm_track"
    if not safe_name.endswith(".mp3"):
        safe_name += ".mp3"

    assets_dir = OUTPUTS_DIR / project_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # 기존 BGM 삭제
    if project.bgm_filename:
        old_path = assets_dir / project.bgm_filename
        if old_path.exists():
            old_path.unlink()

    bgm_filename = f"bgm_{safe_name}"
    bgm_path = assets_dir / bgm_filename

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(body.url)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to download music file")

    bgm_path.write_bytes(resp.content)
    project.bgm_filename = bgm_filename
    project.updated_at   = datetime.utcnow()
    db.commit()
    logger.info(f"BGM downloaded: {project_id} → {bgm_filename}")
    return _enrich_project(project)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Update Project Settings
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.patch("/{project_id}/settings", response_model=ProjectRead)
def update_project_settings(project_id: str, payload: ProjectSettingsUpdate, db: Session = Depends(get_db)):
    """프로젝트 설정 업데이트 (BGM 볼륨, 화면 비율)"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.bgm_volume        = payload.bgm_volume
    project.aspect_ratio      = payload.aspect_ratio
    project.tts_master_volume = payload.tts_master_volume
    if payload.tts_voice is not None:
        project.tts_voice = payload.tts_voice
    if payload.default_transition is not None:
        if payload.default_transition not in VALID_TRANSITIONS:
            raise HTTPException(status_code=400, detail=f"Invalid default_transition. Allowed: {list(VALID_TRANSITIONS)}")
        project.default_transition = payload.default_transition
    if payload.default_slide_duration is not None:
        project.default_slide_duration = payload.default_slide_duration
    if payload.subtitle_font_size is not None:
        project.subtitle_font_size = payload.subtitle_font_size
    if payload.subtitle_font_color is not None:
        project.subtitle_font_color = payload.subtitle_font_color
    if payload.watermark_text is not None:
        project.watermark_text = payload.watermark_text
    if payload.watermark_opacity is not None:
        project.watermark_opacity = payload.watermark_opacity
    if payload.title_text is not None:
        project.title_text = payload.title_text
    if payload.resolution is not None:
        project.resolution = payload.resolution
    if payload.transition_duration is not None:
        project.transition_duration = payload.transition_duration
    if payload.style_preset is not None:
        if payload.style_preset not in VALID_STYLE_PRESETS:
            raise HTTPException(status_code=400, detail=f"Invalid style_preset. Allowed: {VALID_STYLE_PRESETS}")
        project.style_preset = payload.style_preset
    project.updated_at   = datetime.utcnow()
    db.commit()
    return _enrich_project(project)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# End of file
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
