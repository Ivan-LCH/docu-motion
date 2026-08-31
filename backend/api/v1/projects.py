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
from backend.core.config import OUTPUTS_DIR, GOOGLE_API_KEY, JAMENDO_CLIENT_ID, GEMINI_MODEL
from backend.core.logger import get_logger
from backend.services.color_grade import VALID_STYLE_PRESETS


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
            "meta"          : getattr(s, "meta", "{}"),
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
def _jamendo_hits(results: list) -> list:
    return [
        {
            "id": t["id"],
            "title": t.get("name", f"Track {t['id']}"),
            "tags": ", ".join(t.get("musicinfo", {}).get("tags", {}).get("genres", []) +
                              t.get("musicinfo", {}).get("tags", {}).get("instruments", []) +
                              t.get("musicinfo", {}).get("tags", {}).get("vartags", [])),
            "duration": t.get("duration", 0),
            "preview_url": t.get("audio", ""),
            "page_url": t.get("shareurl", ""),
            "artist": t.get("artist_name", ""),
        }
        for t in results
        if t.get("audio")
    ]

@router.get("/{project_id}/bgm/search")
async def search_bgm(project_id: str, q: str = "", page: int = 1, per_page: int = 15):
    """Jamendo 무료 음악 검색"""
    if not JAMENDO_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Jamendo client ID not configured")
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": per_page,
        "offset": (page - 1) * per_page,
        "search": q,
        "include": "musicinfo",
        "audioformat": "mp32",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("https://api.jamendo.com/v3.0/tracks/", params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Jamendo API error")
    data = resp.json()
    results = data.get("results", [])
    total = data.get("headers", {}).get("results_fullcount", len(results))
    return {"total": total, "hits": _jamendo_hits(results)}


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# BGM AI Suggest (Gemini → keywords → Jamendo)
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
@router.post("/{project_id}/bgm/suggest")
async def suggest_bgm(project_id: str, db: Session = Depends(get_db)):
    """슬라이드 나레이션 텍스트 분석 → Gemini로 음악 키워드 추천 → Jamendo 검색"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    slides = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    narrations = [s.text for s in slides if s.text and s.text.strip()]
    combined_text = "\n".join(narrations[:20])

    # Gemini로 키워드 추출
    keyword = "background"
    if GOOGLE_API_KEY and (combined_text or project.title):
        prompt_text = combined_text or project.title
        gemini_prompt = (
            f"다음은 영상의 나레이션 텍스트입니다:\n\n{prompt_text}\n\n"
            "이 영상의 분위기와 주제에 맞는 배경음악을 검색할 영어 키워드 1~2개를 추천해주세요. "
            "키워드만 반환하세요 (예: 'upbeat travel', 'calm nature', 'cinematic inspiring'). "
            "다른 설명 없이 키워드만 한 줄로 답하세요."
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                gemini_resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}",
                    json={"contents": [{"parts": [{"text": gemini_prompt}]}]},
                )
            if gemini_resp.status_code == 200:
                gdata = gemini_resp.json()
                keyword = gdata["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"').strip("'")
                keyword = keyword.split("\n")[0][:60]
        except Exception as e:
            logger.warning(f"Gemini suggest failed: {e}")

    # Jamendo 검색
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 15,
        "search": keyword,
        "include": "musicinfo",
        "audioformat": "mp32",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        jresp = await client.get("https://api.jamendo.com/v3.0/tracks/", params=params)
    hits = []
    if jresp.status_code == 200:
        hits = _jamendo_hits(jresp.json().get("results", []))
    return {"keyword": keyword, "hits": hits}


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
