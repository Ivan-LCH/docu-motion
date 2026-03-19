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
    transition     = Column(String(50), default="none")     # Scene transition: 'none'|'crossfade'|'fade_black'|'slide_left'|'slide_right'
    tts_volume     = Column(Float, default=1.0)              # TTS audio volume (0.0 ~ 2.0), independent of video volume
    rotation       = Column(Integer, default=0)               # Image/Video rotation: 0, 90, 180, 270

    project = relationship("Project", back_populates="slides")

