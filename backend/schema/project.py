# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
"""
DocuMotion - Pydantic 스키마 (요청/응답 모델)
"""
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Import
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide 스키마
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
class SlideBase(BaseModel):
    order_index: int = 0
    image_filename: str = ""
    label: str = ""
    text: str = ""
    # Video Slide
    slide_type: str = "image"
    video_filename: str = ""
    volume: float = 1.0
    subtitles: str = "[]"
    use_tts: int = 1  # 1=TTS 생성, 0=자막만
    trim_start: float = 0.0
    trim_end: float = 0.0
    transition: str = "crossfade"  # 'none'|'crossfade'|'fade_black'|'slide_left'|'slide_right'
    tts_volume: float = 1.0  # TTS 볼륨 (0.0 ~ 2.0)
    rotation: int = 0        # 회전 각도: 0, 90, 180, 270
    overlays: str = "[]"     # JSON array of overlay objects
    image_fit: str = "cover" # 'cover' | 'fit'
    ken_burns: int = 0       # Ken Burns 줌 강도 0~100
    meta: str = "{}"         # 타입별 추가 데이터 JSON (route/place)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide Create
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class SlideCreate(SlideBase):
    pass


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide Update
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class SlideUpdate(BaseModel):
    id: str
    order_index: int
    image_filename: str = ""
    label: str = ""
    text: str = ""
    # Video Slide
    slide_type: str = "image"
    video_filename: str = ""
    volume: float = 1.0
    subtitles: str = "[]"
    use_tts: int = 1  # 1=TTS 생성, 0=자막만
    trim_start: float = 0.0
    trim_end: float = 0.0
    transition: str = "crossfade"
    tts_volume: float = 1.0
    rotation: int = 0
    overlays: str = "[]"
    image_fit: str = "cover" # 'cover' | 'fit'
    ken_burns: int = 0       # Ken Burns 줌 강도 0~100
    meta: str = "{}"         # 타입별 추가 데이터 JSON (route/place)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide Read
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class SlideRead(SlideBase):
    id: str

    # Video Slide Fields Explicitly Added for Serialization
    slide_type: str = "image"
    video_filename: str = ""
    volume: float = 1.0
    subtitles: str = "[]"
    use_tts: int = 1
    trim_start: float = 0.0
    trim_end: float = 0.0
    transition: str = "crossfade"
    tts_volume: float = 1.0
    rotation: int = 0
    overlays: str = "[]"
    image_fit: str = "cover"
    ken_burns: int = 0
    meta: str = "{}"

    model_config = {"from_attributes": True}


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Project 스키마
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class ProjectCreate(BaseModel):
    name: str
    aspect_ratio: str = "16:9"  # '16:9' | '9:16' | '1:1'


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Project Read
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class ProjectRead(BaseModel):
    id: str
    name: str
    status: str
    stage: str
    progress: int
    message: str
    has_video: bool = False
    slide_count: int = 0
    created_at: datetime
    updated_at: datetime
    bgm_filename: str = ""
    bgm_volume: float = 0.3
    aspect_ratio: str = "16:9"
    tts_master_volume: float = 1.0
    default_transition: str = "crossfade"
    default_slide_duration: float = 3.0
    subtitle_font_size: int = 30
    subtitle_font_color: str = "white"
    watermark_text: str = ""
    watermark_opacity: float = 0.3
    title_text: str = ""
    resolution: str = "720p"
    transition_duration: float = 0.7
    style_preset: str = "none"

    model_config = {"from_attributes": True}


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Project Detail
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class ProjectDetail(ProjectRead):
    slides: List[SlideRead] = []


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# 렌더링 / 유튜브 스키마
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class RenderStatus(BaseModel):
    status: str
    progress: int
    message: str


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# YouTube Upload
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class YouTubeUploadRequest(BaseModel):
    title: str
    description: str = "Generated by DocuMotion"
    tags: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# End of file
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
