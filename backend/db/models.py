"""
DocuMotion - SQLAlchemy ORM Models
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship

from backend.db.session import Base


class Project(Base):
    __tablename__ = "projects"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String(200), nullable=False)
    status     = Column(String(20), default="DRAFT")   # DRAFT / QUEUED / PROCESSING / COMPLETED / ERROR
    stage      = Column(String(20), default="initialized")  # initialized / uploaded / scripted
    progress   = Column(Integer, default=0)
    message    = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # BGM
    bgm_filename = Column(String(300), default="")   # uploaded BGM file name
    bgm_volume   = Column(Float, default=0.3)         # 0.0 ~ 1.0
    # Aspect ratio
    aspect_ratio = Column(String(10), default="16:9") # '16:9' | '9:16' | '1:1'
    # Master TTS Volume
    tts_master_volume = Column(Float, default=1.0)     # 0.0 ~ 2.0 — 전역 TTS 볼륨
    # Global defaults (6-10)
    default_transition      = Column(String(50), default="crossfade")    # 전역 전환 효과
    default_slide_duration  = Column(Float, default=3.0)            # 텍스트 없는 슬라이드 기본 시간(초)
    subtitle_font_size      = Column(Integer, default=30)           # 자막 폰트 크기
    subtitle_font_color     = Column(String(20), default="white")   # 자막 폰트 색상
    watermark_text          = Column(String(300), default="")       # 워터마크 텍스트
    watermark_opacity       = Column(Float, default=0.3)            # 워터마크 불투명도
    title_text              = Column(String(300), default="")       # 인트로 타이틀 (비우면 프로젝트명 사용)
    resolution              = Column(String(10), default="720p")    # 출력 해상도 '720p' | '1080p'
    transition_duration     = Column(Float, default=0.7)            # 전환 효과 길이(초)
    style_preset            = Column(String(30), default="none")    # 자동 연출 스타일 'none'|'cinematic'|'vlog'|'documentary'|'trending' (Phase A)

    slides = relationship("Slide", back_populates="project",
                          cascade="all, delete-orphan",
                          order_by="Slide.order_index")


class Slide(Base):
    __tablename__ = "slides"

    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id     = Column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    order_index    = Column(Integer, nullable=False, default=0)
    image_filename = Column(String(300), default="")
    label          = Column(String(300), default="")
    text           = Column(Text, default="")

    # Video Slide Fields
    slide_type     = Column(String(20), default="image")    # 'image' or 'video'
    video_filename = Column(String(300), default="")
    volume         = Column(Float, default=1.0)
    subtitles      = Column(Text, default="[]")             # JSON array of subtitle entries
    use_tts        = Column(Integer, default=1)             # 0 or 1 (SQLite has no BOOLEAN)
    trim_start     = Column(Float, default=0.0)             # Video trim start time (seconds)
    trim_end       = Column(Float, default=0.0)             # Video trim end time (seconds)
    transition     = Column(String(50), default="crossfade")     # Scene transition: 'none'|'crossfade'|'fade_black'|'slide_left'|'slide_right'
    tts_volume     = Column(Float, default=1.0)              # TTS audio volume (0.0 ~ 2.0), independent of video volume
    rotation       = Column(Integer, default=0)               # Image/Video rotation: 0, 90, 180, 270
    overlays       = Column(Text, default="[]")              # JSON array of overlay objects
    image_fit      = Column(String(20), default="cover")     # 'cover' | 'fit' (fit = image top-aligned, subtitle area at bottom)
    ken_burns      = Column(Integer, default=0)               # Ken Burns intensity 0~100 (0 = no zoom, 100 = max ±15%)
    meta           = Column(Text, default="{}")              # 타입별 추가 데이터 JSON (route/place 슬라이드)
    exif           = Column(Text, default="{}")              # 촬영 메타데이터 JSON {"captured_at": ISO|None, "gps": {lat,lng}|None}

    project = relationship("Project", back_populates="slides")


class SavedLocation(Base):
    """자주 쓰는 장소(집/회사 등) — 전역, 프로젝트 무관.
    경로/장소 슬라이드 생성 시 빠른 선택용. query 저장 시 geocode 로 lat/lng 캐싱."""
    __tablename__ = "saved_locations"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String(100), nullable=False)   # 표시명: 집/회사/...
    query      = Column(String(300), nullable=False)   # 주소 또는 장소명
    lat        = Column(Float, default=0.0)            # geocode 캐시(표시/향후 최적화용)
    lng        = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

