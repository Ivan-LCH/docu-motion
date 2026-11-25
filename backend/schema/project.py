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
from pydantic import BaseModel, field_validator

from backend.services.presets import (
    VALID_TRANSITIONS, DEFAULT_TRANSITION, VALID_TEXT_STYLES, DEFAULT_TEXT_STYLE,
    VALID_PHOTO_EFFECTS, DEFAULT_PHOTO_EFFECT, DEFAULT_EFFECT_REVEAL,
)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# 프리셋 검증 (전환 / 텍스트 스타일) — 입력 스키마(Create/Update) 전용
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
def validate_transition(v) -> str:
    """None/빈 값 → 기본값, 그 외 presets.VALID_TRANSITIONS 이외 값은 거부."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return DEFAULT_TRANSITION
    v = str(v).strip().lower()
    if v not in VALID_TRANSITIONS:
        raise ValueError(f"Invalid transition '{v}'. Allowed: {list(VALID_TRANSITIONS)}")
    return v


def validate_text_style(v) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return DEFAULT_TEXT_STYLE
    v = str(v).strip().lower()
    if v not in VALID_TEXT_STYLES:
        raise ValueError(f"Invalid text_style '{v}'. Allowed: {list(VALID_TEXT_STYLES)}")
    return v


def validate_photo_effect(v) -> str:
    """None/빈 값 → 기본값, 그 외 presets.VALID_PHOTO_EFFECTS 이외 값은 거부."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return DEFAULT_PHOTO_EFFECT
    v = str(v).strip().lower()
    if v not in VALID_PHOTO_EFFECTS:
        raise ValueError(f"Invalid photo_effect '{v}'. Allowed: {list(VALID_PHOTO_EFFECTS)}")
    return v


def validate_effect_reveal(v) -> float:
    """슬라이드 길이 대비 원본→효과 비율. 범위 밖은 0~1 로 클램프, 숫자가 아니면 거부."""
    if v is None or v == "":
        return DEFAULT_EFFECT_REVEAL
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("effect_reveal must be a number between 0 and 1")
    if f != f or f == float("inf") or f == float("-inf"):
        raise ValueError("effect_reveal must be a number between 0 and 1")
    return max(0.0, min(1.0, f))


class _SlidePresetValidators(BaseModel):
    @field_validator("transition", mode="before", check_fields=False)
    @classmethod
    def _check_transition(cls, v):
        return validate_transition(v)

    @field_validator("text_style", mode="before", check_fields=False)
    @classmethod
    def _check_text_style(cls, v):
        return validate_text_style(v)

    @field_validator("photo_effect", mode="before", check_fields=False)
    @classmethod
    def _check_photo_effect(cls, v):
        return validate_photo_effect(v)

    @field_validator("effect_reveal", mode="before", check_fields=False)
    @classmethod
    def _check_effect_reveal(cls, v):
        return validate_effect_reveal(v)


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
    transition: str = "crossfade"  # presets.VALID_TRANSITIONS
    tts_volume: float = 1.0  # TTS 볼륨 (0.0 ~ 2.0)
    rotation: int = 0        # 회전 각도: 0, 90, 180, 270
    overlays: str = "[]"     # JSON array of overlay objects
    image_fit: str = "cover" # 'cover' | 'fit'
    ken_burns: int = 0       # Ken Burns 줌 강도 0~100
    photo_effect: str = "none"   # presets.VALID_PHOTO_EFFECTS ('none'|'sketch'|'cartoon'|'sketch_soft'|'watercolor'|'sketch_color')
    effect_reveal: float = 0.45  # 0~1, 원본→효과 크로스페이드가 차지하는 슬라이드 길이 비율
    meta: str = "{}"         # 타입별 추가 데이터 JSON (route/place, hero_text/punch_text)
    text_style: str = "caption"  # presets.VALID_TEXT_STYLES


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide Create
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class SlideCreate(_SlidePresetValidators, SlideBase):
    pass


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Slide Update
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

class SlideUpdate(_SlidePresetValidators):
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
    photo_effect: str = "none"
    effect_reveal: float = 0.45
    meta: str = "{}"         # 메타 JSON — 저장 시 hero_text/punch_text 키만 병합 (route 프레임 등 보존)
    text_style: str = "caption"


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
    photo_effect: str = "none"
    effect_reveal: float = 0.45
    meta: str = "{}"
    text_style: str = "caption"

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
    tts_voice: str = ""
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
