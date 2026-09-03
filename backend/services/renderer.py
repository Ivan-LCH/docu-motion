# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
"""
DocuMotion - Video Renderer (MoviePy 기반)
기존 renderer.py를 backend/services/ 으로 이식 (경로 설정만 변경)
"""
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Import 
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

import os
# imageio_ffmpeg 번들 바이너리는 NVENC 미지원 → 시스템 ffmpeg 사용 강제
os.environ.setdefault("IMAGEIO_FFMPEG_EXE", "/usr/bin/ffmpeg")
import logging
import re
import shutil
import time
import numpy as np
from pathlib import Path

from moviepy.editor import (
    ImageClip, AudioFileClip, concatenate_videoclips,
    TextClip, CompositeVideoClip, ColorClip
)
from PIL import Image
from proglog import ProgressBarLogger

try:
    from backend.services.tts_manager import TTSEngine
except ImportError:
    TTSEngine = None

from backend.services.color_grade import apply_cinematic, _resolve_motion_cfg, _resolve_rhythm_cfg, MOTION_VARIETY
from backend.services.face_detect import detect_face_center
from backend.services.beat_detect import detect_beats, nearest_beat
from backend.core.config import CANVAS_SIZE, FONT_PATH, FONT_BOLD_PATH, TTS_SERVER_URL, TTS_VOICE_NAME
from backend.core.logger import get_logger

# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Logger
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Constant
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

# PIL 호환성
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS

FONT_SIZE  = 28
TEXT_COLOR = 'white'
BG_COLOR   = (0, 0, 0)

# Aspect-ratio → (width, height) 매핑
ASPECT_RATIO_MAP = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1":  (720, 720),
}


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Split Sentences
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def split_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s for s in sentences if s]


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Cleanup Temp Files
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def cleanup_temp_files(temp_dir: Path):
    for f in temp_dir.glob("v_*.wav"):
        try: os.unlink(f)
        except: pass
    for f in Path(".").glob("*TEMP_MPY*.mp3"):
        try: os.unlink(f)
        except: pass


TRANSITION_DURATION = 0.7  # seconds for crossfade / fade transitions


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Apply Overlays (모자이크 / 블러 / 이모지 / 텍스트)
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

import json as _json_mod
import numpy as _np
from PIL import ImageFilter, ImageDraw, ImageFont

# RouteSlide 이동정보 표시용 프로필 한글화
_ROUTE_PROFILE_KO = {'driving': '자동차', 'foot': '도보', 'walking': '도보',
                     'bicycle': '자전거', 'bike': '자전거'}


def _format_route_info(distance_m: float, duration_s: float, profile: str = '') -> str:
    """거리·소요시간·이동수단 → 표시용 문자열 (예: '10.4km · 17분 · 자동차')."""
    if distance_m >= 1000:
        dist = f"{distance_m / 1000:.1f}km"
    elif distance_m > 0:
        dist = f"{int(distance_m)}m"
    else:
        dist = ""
    if duration_s >= 3600:
        h = int(duration_s // 3600)
        m = int(round((duration_s % 3600) / 60))
        dur = f"{h}시간 {m}분" if m else f"{h}시간"
    elif duration_s >= 60:
        dur = f"{int(round(duration_s / 60))}분"
    elif duration_s > 0:
        dur = f"{int(duration_s)}초"
    else:
        dur = ""
    prof = _ROUTE_PROFILE_KO.get(profile, profile)
    parts = [p for p in (dist, dur, prof) if p]
    return "  ·  ".join(parts)


def _apply_overlays_to_pil(img: Image.Image, overlays: list) -> Image.Image:
    """PIL Image에 오버레이 목록 적용 후 반환"""
    if not overlays:
        return img
    img = img.copy().convert("RGBA")
    iw, ih = img.size

    for ov in overlays:
        otype = ov.get("type", "")
        x = int(ov.get("x", 0) * iw)
        y = int(ov.get("y", 0) * ih)
        w = max(1, int(ov.get("w", 0.1) * iw))
        h = max(1, int(ov.get("h", 0.1) * ih))
        x2, y2 = min(x + w, iw), min(y + h, ih)

        if otype in ("mosaic", "blur"):
            region = img.crop((x, y, x2, y2)).convert("RGB")
            if otype == "mosaic":
                block = max(1, min(w, h) // 10)
                small = region.resize((max(1, w // block), max(1, h // block)), Image.NEAREST)
                processed = small.resize((w, h), Image.NEAREST)
            else:
                radius = max(5, min(w, h) // 8)
                processed = region.filter(ImageFilter.GaussianBlur(radius=radius))
            img.paste(processed.convert("RGBA"), (x, y))

        elif otype == "emoji":
            content = ov.get("content", "⭐")
            # fontSize가 있으면 우선 사용, 없으면 기존 방식(h 기반)으로 폴백
            font_frac = ov.get("fontSize")
            font_size = max(12, int((font_frac if font_frac else ov.get("h", 0.1)) * ih))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype(FONT_PATH, font_size)
            except Exception:
                font = ImageFont.load_default()
            # 박스 중앙 정렬
            try:
                bbox = draw.textbbox((0, 0), content, font=font, anchor="lt")
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = font_size, font_size
            cx = x + w // 2 - tw // 2 - bbox[0]
            cy = y + h // 2 - th // 2 - bbox[1]
            draw.text((cx, cy), content, font=font, fill=(255, 255, 255, 255))

        elif otype == "text":
            content = ov.get("content", "")
            color_str = ov.get("color", "white")
            font_frac = ov.get("fontSize")
            font_size = max(12, int((font_frac if font_frac else ov.get("h", 0.05)) * ih))
            color_map = {"white": (255,255,255,255), "black": (0,0,0,255),
                         "red": (255,0,0,255), "yellow": (255,220,0,255), "blue": (80,150,255,255)}
            color = color_map.get(color_str, (255,255,255,255))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype(FONT_PATH, font_size)
            except Exception:
                font = ImageFont.load_default()
            # 박스 중앙 정렬을 위한 텍스트 크기 측정
            try:
                bbox_t = draw.textbbox((0, 0), content, font=font, anchor="lt")
                tw, th = bbox_t[2] - bbox_t[0], bbox_t[3] - bbox_t[1]
                ox, oy = bbox_t[0], bbox_t[1]
            except Exception:
                tw, th, ox, oy = font_size * len(content) // 2, font_size, 0, 0
            cx = x + w // 2 - tw // 2 - ox
            cy = y + h // 2 - th // 2 - oy
            # 반투명 배경 (실제 텍스트 위치 기준)
            pad = 4
            bg = Image.new("RGBA", img.size, (0, 0, 0, 0))
            bg_draw = ImageDraw.Draw(bg)
            bg_draw.rectangle((cx + ox - pad, cy + oy - pad, cx + ox + tw + pad, cy + oy + th + pad), fill=(0,0,0,140))
            img = Image.alpha_composite(img, bg)
            draw = ImageDraw.Draw(img)
            draw.text((cx, cy), content, font=font, fill=color)

    return img.convert("RGB")


def make_overlay_frame_fn(overlays: list):
    """MoviePy fl_image용 함수 반환 (비디오 슬라이드)"""
    def apply(frame):
        img = Image.fromarray(frame)
        result = _apply_overlays_to_pil(img, overlays)
        return _np.array(result)
    return apply


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Apply Transition
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def _make_volume_duck_filter(t_start, t_end, duck_vol, fade):
    """
    [t_start, t_end] 구간에서 볼륨을 duck_vol로 낮추는 MoviePy fl() 필터.
    구간 전후 fade초에 걸쳐 부드럽게 전환 (BGM/원본 오디오 덕킹 공용).
    오디오 렌더링 시 t가 numpy 배열로 들어오므로 벡터 연산으로 처리한다.
    """
    def volume_filter(get_frame, t):
        frame = get_frame(t)
        t_arr = np.atleast_1d(np.asarray(t, dtype=float))
        vol = np.ones_like(t_arr)
        ramp_in  = (t_arr >= t_start - fade) & (t_arr < t_start)
        inside   = (t_arr >= t_start) & (t_arr <= t_end)
        ramp_out = (t_arr > t_end) & (t_arr <= t_end + fade)
        vol[ramp_in]  = 1.0 - ((t_arr[ramp_in] - (t_start - fade)) / fade) * (1.0 - duck_vol)
        vol[inside]   = duck_vol
        vol[ramp_out] = duck_vol + ((t_arr[ramp_out] - t_end) / fade) * (1.0 - duck_vol)
        if np.ndim(t) == 0:
            return frame * vol[0]
        # 배열 t: frame은 (n_samples, n_channels) — 채널 축에 브로드캐스트
        return frame * vol.reshape(-1, 1)
    return volume_filter


def apply_transition(clip_a, clip_b, transition: str, duration: float = None):
    """
    clip_a → clip_b 사이에 전환 효과를 적용하여 연결된 클립 리스트를 반환합니다.
    각 클립의 start 오프셋은 concatenate 방식 대신 직접 배치합니다.

    반환: (처리된 clip_a, 처리된 clip_b, offset_delta)
      offset_delta = clip_a가 끝나기 전에 clip_b가 시작되는 시간 (겹침)
    """
    t = duration if duration and duration > 0 else TRANSITION_DURATION
    transition = (transition or "none").lower()

    if transition == "crossfade":
        # clip_a의 마지막 t초에서 서서히 사라지고, clip_b의 첫 t초에서 서서히 나타남
        a_out = clip_a.crossfadeout(t)
        b_in  = clip_b.crossfadein(t)
        return a_out, b_in, t

    elif transition == "fade_black":
        # clip_a: fadeout(검정으로), clip_b: fadein(검정에서)
        a_out = clip_a.fadeout(t)
        b_in  = clip_b.fadein(t)
        return a_out, b_in, 0  # 겹침 없이 순차 배치

    elif transition in ("slide_left", "slide_right"):
        # clip_b가 clip_a 위로 밀려 들어오는 효과
        w = clip_a.size[0]
        direction = 1 if transition == "slide_left" else -1

        def pos_b(rel_t):
            progress = min(rel_t / t, 1.0)
            x = direction * w * (1.0 - progress)
            return (int(x), 0)

        b_moving = clip_b.set_position(pos_b).set_start(0).set_duration(min(t, clip_b.duration))
        # slide는 두 클립을 겹쳐서 CompositeVideoClip으로 합성
        # 편의상 fade_black과 같이 offset 없이 처리 (구현 단순화)
        a_out = clip_a.fadeout(t * 0.3)
        b_in  = clip_b.fadein(t * 0.3)
        return a_out, b_in, 0

    else:  # 'none' or unknown
        return clip_a, clip_b, 0



# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Worker Progress Logger
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
class WorkerProgressLogger(ProgressBarLogger):
    def __init__(self, callback, base_progress=50):
        super().__init__()
        self._progress_callback = callback    # 실제 DB 업데이트용 콜백
        self.callback = lambda **kw: None     # ProgressBarLogger의 __call__ 우회 (0% 덮어쓰기 방지)
        self.base_progress = base_progress
        self.last_percent = -1
        self.last_time = 0

    def bars_callback(self, bar, attr, value, old_value=None):
        if bar == 't' and attr == 'index':
            total = self.bars[bar]['total']
            if total > 0:
                percent = int(value / total * 100)
                now = time.time()
                # 1초 이상 지났거나 100% 도달 시에만 업데이트 (로그 스팸 방지)
                if percent == 100 or (now - self.last_time >= 5.0) or (percent - self.last_percent >= 10):
                    self.last_percent = percent
                    self.last_time = now
                    overall = self.base_progress + int(percent * 0.5)
                    self._progress_callback(overall, f"인코딩 중... ({percent}%)")


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Render Logic
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def load_tts_engine(voice_name: str = ""):
    if TTSEngine is None:
        return None
    try:
        return TTSEngine(server_url=TTS_SERVER_URL, voice_name=voice_name or TTS_VOICE_NAME)
    except Exception as e:
        logger.error(f"TTS Engine Load Error: {e}")
        return None


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Build Single Slide Clip (슬라이드 1개 → MoviePy 클립)
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
def build_slide_clip(item: dict, slide_index: int, assets_dir: Path, temp_dir: Path,
                     canvas_size: tuple, tts_engine=None,
                     font_size: int = FONT_SIZE, font_color: str = TEXT_COLOR,
                     default_slide_duration: float = 3.0,
                     tts_master_volume: float = 1.0,
                     narration_segments: list = None,
                     style_preset: str = "none",
                     free_slide_duration: float = None):
    """
    단일 슬라이드 → MoviePy 클립 생성 (비디오 / 이미지 공용).
    render_project()의 슬라이드 루프 본문을 추출한 것으로, 풀 렌더와
    구간 미리보기(6-19) 양쪽에서 동일 로직을 재사용한다.

    narration_segments: 리스트를 넘기면 TTS 음성 구간 (start, end)이
      클립 기준 상대 시간으로 append됨 (BGM 덕킹용, 8-7). None이면 수집 안 함.

    반환:
      - MoviePy VideoClip: 정상 생성된 슬라이드 클립
      - None: 비디오 파일 누락 등으로 스킵
      (TTS 생성 실패 시 Exception 전파 — 기존과 동일)
    """
    # 루프 본문과 변수명을 일치시키기 위한 별칭 (동작 보존)
    i = slide_index
    _font_size = font_size
    _font_color = font_color

    text = item.get('text', '').strip()
    slide_type = item.get('slide_type', 'image')

    a_clip = None
    subtitle_clips = []

    # ── Video Slide ──────────────────────────────────
    if slide_type == 'video':
        import json as _json
        video_filename = item.get('video_filename', '')
        video_path = assets_dir / video_filename
        if not video_path.exists():
            logger.error(f"Video file not found: {video_path}")
            return None

        from moviepy.editor import VideoFileClip, CompositeAudioClip
        video_clip = VideoFileClip(str(video_path))

        # 볼륨 조절
        volume = item.get('volume', 1.0)
        if volume != 1.0:
            video_clip = video_clip.volumex(volume)

        # 비디오 자르기 (Trim)
        trim_start = float(item.get('trim_start', 0.0))
        trim_end = float(item.get('trim_end', 0.0))
        if trim_end > trim_start:
            video_clip = video_clip.subclip(trim_start, min(trim_end, video_clip.duration))
        elif trim_start > 0:
            video_clip = video_clip.subclip(trim_start, video_clip.duration)

        # 회전 적용
        rotation = item.get('rotation', 0)
        if rotation and rotation % 360 != 0:
            video_clip = video_clip.rotate(-rotation)  # MoviePy는 반시계 방향이므로 부호 반전

        # 캔버스 크기 맞춤 (letterbox)
        video_clip = video_clip.resize(height=canvas_size[1])
        total_duration = video_clip.duration

        # Subtitles & optional TTS
        subtitle_entries = _json.loads(item.get('subtitles', '[]'))
        tts_vol = item.get('tts_volume', 1.0) * tts_master_volume
        original_audio = video_clip.audio
        tts_audio_clips = []  # TTS 오디오만 별도 수집 (duck 처리용)

        for sub_idx, entry in enumerate(subtitle_entries):
            try:
                start   = float(entry.get('start', 0))
                end     = float(entry.get('end', 0))
                sub_txt = entry.get('text', '').strip()
                use_tts = entry.get('use_tts', False)
                dur     = max(0, end - start)
                if dur <= 0 or start >= total_duration or not sub_txt:
                    continue

                # TextClip 자막
                tc = TextClip(
                    txt=sub_txt, font=FONT_BOLD_PATH, fontsize=_font_size,
                    color=_font_color, stroke_color='black', stroke_width=2,
                    size=(canvas_size[0] - 100, None),
                    method='caption', align='center', interline=8
                )
                tc_y = canvas_size[1] - 30 - tc.size[1]
                sub_dur = min(dur, total_duration - start)
                box = (
                    ColorClip(size=(tc.size[0] + 24, tc.size[1] + 14), color=(0, 0, 0))
                    .set_opacity(0.45)
                    .set_start(start)
                    .set_duration(sub_dur)
                    .set_position(('center', tc_y - 7))
                )
                tc = (
                    tc
                    .set_start(start)
                    .set_duration(sub_dur)
                    .set_position(('center', tc_y))
                )
                subtitle_clips.append(box)
                subtitle_clips.append(tc)

                # Optional TTS 오디오 생성
                if use_tts and tts_engine:
                    tts_path = temp_dir / f"v_{i}_{sub_idx}.wav"
                    if tts_path.exists() and tts_path.stat().st_size < 100:
                        try: tts_path.unlink()
                        except: pass

                    if not tts_path.exists():
                        tts_engine.generate_with_fallback(sub_txt, str(tts_path))

                    if tts_path.exists() and tts_path.stat().st_size >= 100:
                        tts_audio = AudioFileClip(str(tts_path)).set_start(start)
                        if tts_vol != 1.0:
                            tts_audio = tts_audio.volumex(tts_vol)
                        tts_audio_clips.append((start, end, tts_audio))
                        if narration_segments is not None:
                            narration_segments.append((start, end))
                    else:
                        logger.warning(f"TTS 오디오 생성 실패 (비디오 자막 [{i}][{sub_idx}]) - 자막만 표시")
            except Exception as e:
                logger.error(f"Error processing subtitle entry: {e}")

        # 오디오 믹싱: TTS 구간에서 원본 오디오 duck (볼륨 30%로 감소)
        audio_clips = []
        if original_audio and tts_audio_clips:
            DUCK_VOLUME = 0.3
            FADE_DUR = 0.3  # duck fade-in/out 시간
            # 원본 오디오를 TTS 구간에서 duck 처리
            ducked_audio = original_audio
            for tts_start, tts_end, _ in tts_audio_clips:
                ducked_audio = ducked_audio.fl(
                    _make_volume_duck_filter(tts_start, tts_end, DUCK_VOLUME, FADE_DUR))
            audio_clips.append(ducked_audio)
            audio_clips.extend(tts_clip for _, _, tts_clip in tts_audio_clips)
        elif original_audio:
            audio_clips.append(original_audio)
        else:
            audio_clips.extend(tts_clip for _, _, tts_clip in tts_audio_clips)

        bg_clip = ColorClip(size=canvas_size, color=BG_COLOR).set_duration(total_duration)
        video_clip = video_clip.set_fps(24)

        # 오버레이 적용 (모자이크 / 블러 / 이모지 / 텍스트)
        overlays = _json_mod.loads(item.get('overlays', '[]') or '[]')
        if overlays:
            video_clip = video_clip.fl_image(make_overlay_frame_fn(overlays))

        final_clip = CompositeVideoClip([bg_clip, video_clip] + subtitle_clips)
        if audio_clips:
            final_clip = final_clip.set_audio(CompositeAudioClip(audio_clips))

        return final_clip

    # ── Image Slide ──────────────────────────────────
    use_tts_flag = bool(item.get('use_tts', 1))

    if not text:
        # 텍스트가 없으면 기본 슬라이드 시간만큼 이미지 유지 (B2 비트 스냅 시 free_slide_duration 사용)
        total_duration = free_slide_duration if free_slide_duration is not None else default_slide_duration
    elif not use_tts_flag:
        # TTS 꺼짐 → 텍스트 길이에 비례해 시간 계산 (글자당 0.15초, 최소 3초)
        total_duration = max(3.0, len(text) * 0.15)
        sentences      = split_sentences(text)
        num_sent       = len(sentences)
        total_chars    = sum(len(s) for s in sentences)
        seg_dur        = total_duration / num_sent if num_sent > 0 else total_duration
        current_start  = 0.1
        for idx, s in enumerate(sentences):
            char_ratio = len(s) / total_chars if total_chars > 0 else 1.0 / num_sent
            dur        = char_ratio * (total_duration - 0.2)
            txt_clip = TextClip(
                txt       = s,
                font      = FONT_BOLD_PATH,
                fontsize  = _font_size,
                color     = _font_color,
                stroke_color = 'black',
                stroke_width = 2,
                size      = (canvas_size[0] - 100, None),
                method    = 'caption',
                align     = 'center',
                interline = 8
            )
            _ty = canvas_size[1] - 30 - txt_clip.size[1]
            box = (
                ColorClip(size=(txt_clip.size[0] + 24, txt_clip.size[1] + 14), color=(0, 0, 0))
                .set_opacity(0.45)
                .set_start(current_start)
                .set_duration(dur)
                .set_position(('center', _ty - 7))
            )
            txt_clip = (
                txt_clip
                .set_start(current_start)
                .set_duration(dur)
                .set_position(('center', _ty))
            )
            subtitle_clips.append(box)
            subtitle_clips.append(txt_clip)
            current_start += dur
    else:
        # TTS - 문장별로 따로 생성 후 순차 이어붙이기 (concatenate)
        # CompositeAudioClip+set_start의 클립 누락 버그를 회피
        from moviepy.editor import concatenate_audioclips
        from moviepy.audio.AudioClip import AudioArrayClip

        sentences = split_sentences(text)
        num_sent  = len(sentences)
        PAD_DELAY     = 1.0   # 이미지 안정 표시 후 음성/자막 시작
        SENTENCE_GAP  = 0.3   # 문장 간 쉬는 시간 (편안한 호흡)
        tts_vol = item.get('tts_volume', 1.0) * tts_master_volume

        sent_audio_clips_raw = []   # 원본 오디오 클립 목록 (set_start 미적용)
        sent_durations       = []   # 각 문장의 실제 음성 길이

        for sent_idx, s in enumerate(sentences):
            s_path = temp_dir / f"v_{i}_{sent_idx}.wav"

            # 기존 전체-텍스트 캐시 파일은 무시 (v_{i}.wav)
            if s_path.exists() and s_path.stat().st_size < 100:
                try: s_path.unlink()
                except: pass

            if not s_path.exists():
                if tts_engine:
                    try:
                        tts_engine.generate_with_fallback(s, str(s_path))
                    except Exception as tts_err:
                        logger.warning(f"TTS 생성 예외 (슬라이드 {i+1}, 문장 {sent_idx+1}): {tts_err}")

            if not s_path.exists() or s_path.stat().st_size < 100:
                # 8-11: 문장 하나의 TTS 실패가 전체 렌더를 중단시키지 않도록
                # 추정 길이의 무음으로 대체 (자막 타이밍은 유지)
                logger.warning(f"TTS 실패 → 무음 대체 (슬라이드 {i+1}, 문장 {sent_idx+1})")
                est_dur = max(1.5, len(s) * 0.15)
                _n = int(44100 * est_dur)
                s_clip = AudioArrayClip(np.zeros((_n, 2), dtype=np.float32), fps=44100)
                sent_audio_clips_raw.append(s_clip)
                sent_durations.append(est_dur)
                continue

            s_clip = AudioFileClip(str(s_path))
            if tts_vol != 1.0:
                s_clip = s_clip.volumex(tts_vol)

            sent_audio_clips_raw.append(s_clip)
            sent_durations.append(s_clip.duration)

        # 무음 패딩(앞/뒤 PAD_DELAY, 문장 사이 SENTENCE_GAP) 클립 헬퍼
        _ref = sent_audio_clips_raw[0]
        _sr  = int(getattr(_ref, 'fps', 22050) or 22050)
        _nch = 2  # AudioFileClip은 보통 stereo로 디코드. 안전하게 2채널 무음 사용
        def _make_silence(d):
            n = max(1, int(_sr * d))
            return AudioArrayClip(np.zeros((n, _nch), dtype=np.float32), fps=_sr)

        parts = [_make_silence(PAD_DELAY)]
        for idx, c in enumerate(sent_audio_clips_raw):
            parts.append(c)
            if idx < num_sent - 1:
                parts.append(_make_silence(SENTENCE_GAP))
        parts.append(_make_silence(PAD_DELAY))

        a_clip = concatenate_audioclips(parts)
        total_duration = a_clip.duration

        # 자막 타이밍 — 실제 음성 길이 기반
        sub_offset = PAD_DELAY
        BOTTOM_MARGIN = 30
        for sent_idx, (s, s_dur) in enumerate(zip(sentences, sent_durations)):
            txt_clip = TextClip(
                txt=s, font=FONT_BOLD_PATH, fontsize=_font_size,
                color=_font_color, stroke_color='black', stroke_width=2,
                size=(canvas_size[0] - 100, None),
                method='caption', align='center', interline=8
            )
            txt_y = canvas_size[1] - BOTTOM_MARGIN - txt_clip.size[1]
            # 자막 뒤 반투명 배경 박스 — 밝은 영상 위에서도 가독성 확보
            box_clip = (
                ColorClip(size=(txt_clip.size[0] + 24, txt_clip.size[1] + 14), color=(0, 0, 0))
                .set_opacity(0.45)
                .set_start(sub_offset)
                .set_duration(s_dur)
                .set_position(('center', txt_y - 7))
            )
            txt_clip = (
                txt_clip
                .set_start(sub_offset)
                .set_duration(s_dur)
                .set_position(('center', txt_y))
            )
            subtitle_clips.append(box_clip)
            subtitle_clips.append(txt_clip)
            if narration_segments is not None:
                narration_segments.append((sub_offset, sub_offset + s_dur))
            sub_offset += s_dur
            if sent_idx < num_sent - 1:
                sub_offset += SENTENCE_GAP

    # ── Route Slide: 프레임 시퀀스로 애니메이션 클립 생성 ──
    # place 슬라이드는 image_filename 이 있으므로 아래 일반 image 분기로 자연스럽게 처리.
    if slide_type == 'route':
        try:
            _meta = _json_mod.loads(item.get('meta', '{}') or '{}')
        except Exception:
            _meta = {}
        frame_names = _meta.get('frames', [])
        frame_paths = [str(assets_dir / fn) for fn in frame_names if fn]
        frame_paths = [p for p in frame_paths if Path(p).exists()]

        if frame_paths:
            from moviepy.editor import ImageSequenceClip  # ImageClip/concatenate_videoclips 은 모듈 top import 재사용 (local 재 import 시 image 분기에서 UnboundLocalError)
            # 여정이 슬라이드 길이의 85% 시점에 완주하도록 fps를 슬라이드 길이에 맞춤.
            # (기존: 고정 6fps라 나레이션이 짧으면 도착 전에 장면 전환되는 문제)
            _n = len(frame_paths)
            _travel_dur = max(1.0, total_duration * 0.85)
            _route_fps = _n / _travel_dur
            seq = ImageSequenceClip(frame_paths, fps=_route_fps)
            if seq.duration < total_duration:
                # 도착 후 남은 시간은 마지막(도착) 프레임 유지
                _hold = ImageClip(frame_paths[-1]).set_duration(total_duration - seq.duration)
                seq = concatenate_videoclips([seq, _hold], method="compose")
            else:
                seq = seq.subclip(0, total_duration)
            # 캔버스와 해상도가 다르면 cover 리사이즈
            cw, ch = canvas_size
            if seq.size != (cw, ch):
                seq = seq.resize((cw, ch))
            img_clip = seq
        else:
            img_clip = ColorClip(size=canvas_size, color=(0, 0, 0)).set_duration(total_duration)

        bg_clip = ColorClip(size=canvas_size, color=BG_COLOR).set_duration(total_duration)

        # 이동 정보 오버레이 (출발→도착) — 거리/시간/수단/진행률은 프레임 상단 알약에
        # 이미 구워져 있으므로 TextClip 중복 표시는 제거 (8-x 리디자인)
        route_overlays = list(subtitle_clips)
        try:
            _o = (_meta.get('origin') or {}).get('name', '')
            _d = (_meta.get('destination') or {}).get('name', '')
            if _o and _d:
                _title_txt = f"{_o}  →  {_d}"
                _title_clip = TextClip(
                    txt=_title_txt, font=FONT_PATH, fontsize=int(canvas_size[1] * 0.045),
                    color='white', stroke_color='black', stroke_width=2, method='label',
                ).set_duration(total_duration).set_position((24, 20))
                route_overlays.append(_title_clip)
        except Exception as _e:
            logger.warning(f"Route overlay 생성 실패(무시): {_e}")

        final_clip = CompositeVideoClip([bg_clip, img_clip] + route_overlays)
        if a_clip is not None:
            final_clip = final_clip.set_audio(a_clip)
        return final_clip

    # Image clip with Ken Burns effect
    img_filename = item.get('image_filename', '')
    img_path = Path(img_filename) if Path(img_filename).is_absolute() else assets_dir / img_filename

    if img_path.exists():
        # 회전 적용 (PIL로 이미지 전처리)
        rotation = item.get('rotation', 0)
        if rotation and rotation % 360 != 0:
            from PIL import Image as PILImage
            rotated_path = temp_dir / f"rot_{i}_{rotation}.png"
            if not rotated_path.exists():
                pil_img = PILImage.open(str(img_path))
                pil_img = pil_img.rotate(-rotation, expand=True)  # PIL은 반시계 방향
                pil_img.save(str(rotated_path))
            img_path = rotated_path

        # 오버레이 적용 (PIL로 이미지에 직접)
        overlays = _json_mod.loads(item.get('overlays', '[]') or '[]')
        if overlays:
            from PIL import Image as _PILImg
            ov_path = temp_dir / f"ov_{i}.png"
            pil_src = _PILImg.open(str(img_path))
            pil_src = _apply_overlays_to_pil(pil_src, overlays)
            pil_src.save(str(ov_path))
            img_path = ov_path

        # 이미지 표시 방식 + Ken Burns 강도
        image_fit = item.get('image_fit', 'cover')  # 'cover' | 'fit'
        ken_burns = max(0, min(100, int(item.get('ken_burns', 0) or 0)))  # 0~100
        raw_img_clip = ImageClip(str(img_path))
        iw, ih = raw_img_clip.size
        cw, ch = canvas_size

        # 강도 0이어도 cover 시 약간의 여유분 필요 → 최소 margin 1.02
        kb_intensity = ken_burns / 100.0  # 0.0 ~ 1.0
        margin = 1.02 + 0.13 * kb_intensity  # 1.02 (정적) ~ 1.15 (최대 줌)

        if image_fit == 'fit':
            fit_h = int(ch * 0.82)  # 하단 18% 자막 영역 확보
            # contain: 이미지 전체가 잘리지 않고 (cw x fit_h) 안에 들어오도록
            contain_scale = min(cw / iw, fit_h / ih)
            fit_scale = contain_scale * margin
            base_img = raw_img_clip.resize((int(iw * fit_scale), int(ih * fit_scale)))
            # kb_area = contain 기준 크기 → 이 크기로 크롭하면 이미지 전체가 보임
            kb_area = (int(iw * contain_scale), int(ih * contain_scale))
        else:
            cover_scale = max(cw / iw, ch / ih) * margin
            base_img = raw_img_clip.resize((int(iw * cover_scale), int(ih * cover_scale)))
            kb_area = (cw, ch)

        # ── 모션 설정 (Phase B) ──────────────────────────────────────────
        # motion_cfg: 스타일 프리셋의 모션 설정 (preset none → None)
        # face_center: (fx, fy) ∈ [0,1]², motion_cfg 있을 때만 감지 (얼굴 없으면 None)
        motion_cfg = _resolve_motion_cfg(style_preset)
        face_center = detect_face_center(str(img_path)) if motion_cfg else None
        motion_active = (ken_burns > 0) and (motion_cfg is not None)

        # 줌 강도: 슬라이드 ken_burns × 프리셋 intensity 곱. preset none → 1.0 (기존 동일)
        scale_mul = float(motion_cfg.get("intensity", 1.0)) if motion_cfg else 1.0
        max_zoom = 0.13 * kb_intensity * scale_mul   # ±최대 비율

        # 모션 타입 결정 (순환 순열 — 인접 중복 회피). 백호환 경로는 None.
        if motion_active:
            variety = motion_cfg.get("variety", "calm")
            allowed = MOTION_VARIETY.get(variety, MOTION_VARIETY["calm"])
            motion_type = allowed[i % len(allowed)]
        else:
            motion_type = None
            kb_start = 1.0
            kb_end = (1.0 + max_zoom) if (ken_burns > 0 and i % 2 == 0) else \
                     ((1.0 - max_zoom * 0.85) if ken_burns > 0 else 1.0)

        def _biased_center(w, h, view_w, view_h):
            """얼굴 바이어스 크롭 중심 (px). face_center 없으면 정중앙."""
            cx, cy = w * 0.5, h * 0.5
            if face_center is not None:
                fxn, fyn = face_center
                BIAS = 0.6
                cx = (1 - BIAS) * (w * 0.5) + BIAS * (fxn * w)
                cy = (1 - BIAS) * (h * 0.5) + BIAS * (fyn * h)
            left = min(max(cx - view_w * 0.5, 0.0), max(w - view_w, 0.0))
            top  = min(max(cy - view_h * 0.5, 0.0), max(h - view_h, 0.0))
            return left, top

        def _make_kb(clip, start_scale, end_scale, dur, area):
            # 백호환 선형 줌 (preset none + ken_burns>0). 정중앙 크롭, 이징 無.
            def make_frame(t):
                from PIL import Image as PILImage
                import numpy as np
                progress = t / dur if dur > 0 else 0.0
                scale = start_scale + (end_scale - start_scale) * progress
                frame = clip.get_frame(0)
                h, w = frame.shape[:2]
                target_w, target_h = area
                view_w = target_w / scale
                view_h = target_h / scale
                left = (w - view_w) / 2.0
                top  = (h - view_h) / 2.0
                cropped = PILImage.fromarray(frame).crop(
                    (left, top, left + view_w, top + view_h)
                )
                return np.array(cropped.resize((target_w, target_h), PILImage.LANCZOS))
            from moviepy.editor import VideoClip
            return VideoClip(make_frame, duration=dur).set_fps(24)

        def _make_motion_clip(clip, mt, dur, area, max_z):
            # Phase B 모션 프리셋: smoothstep 이징 + 얼굴 바이어스 + 다축 팬/줌.
            from PIL import Image as PILImage
            import numpy as np
            from moviepy.editor import VideoClip
            target_w, target_h = area
            pan_delta = 0.06   # 팬 이동폭 (프레임 대비 6%)

            def make_frame(t):
                p = t / dur if dur > 0 else 0.0
                e = p * p * (3 - 2 * p)   # smoothstep
                frame = clip.get_frame(0)
                h, w = frame.shape[:2]

                if mt == "zoom_in":
                    scale = 1.0 + max_z * e
                    view_w, view_h = target_w / scale, target_h / scale
                    left, top = _biased_center(w, h, view_w, view_h)
                elif mt == "zoom_out":
                    scale = (1.0 + max_z) - max_z * e
                    view_w, view_h = target_w / scale, target_h / scale
                    left, top = _biased_center(w, h, view_w, view_h)
                elif mt == "zoom_diag":
                    scale = 1.0 + max_z * e
                    view_w, view_h = target_w / scale, target_h / scale
                    left, top = _biased_center(w, h, view_w, view_h)
                    left = min(max(left + (pan_delta * w) * (0.5 - e), 0.0), max(w - view_w, 0.0))
                    top  = min(max(top + (pan_delta * h) * (0.5 - e), 0.0), max(h - view_h, 0.0))
                else:  # pan_left / pan_right / pan_up — 약간 줌인 고정 후 단축 이동
                    scale = 1.0 + max_z * 0.5
                    view_w, view_h = target_w / scale, target_h / scale
                    left, top = _biased_center(w, h, view_w, view_h)
                    if mt == "pan_left":
                        left = min(max(left + (pan_delta * w) * (0.5 - e), 0.0), max(w - view_w, 0.0))
                    elif mt == "pan_right":
                        left = min(max(left - (pan_delta * w) * (0.5 - e), 0.0), max(w - view_w, 0.0))
                    elif mt == "pan_up":
                        top = min(max(top + (pan_delta * h) * (0.5 - e), 0.0), max(h - view_h, 0.0))

                cropped = PILImage.fromarray(frame).crop(
                    (left, top, left + view_w, top + view_h)
                )
                return np.array(cropped.resize((target_w, target_h), PILImage.LANCZOS))
            return VideoClip(make_frame, duration=dur).set_fps(24)

        if ken_burns <= 0:
            # 정적 이미지: 한 번만 처리해서 ImageClip으로 (성능 최적화)
            # motion_cfg 있으면 얼굴 바이어스 중앙 크롭, 없으면 정중앙 (백호환).
            import numpy as _np2
            target_w, target_h = kb_area
            src_frame = base_img.get_frame(0)
            sh, sw = src_frame.shape[:2]
            crop_left, crop_top = _biased_center(sw, sh, target_w, target_h)
            static_pil = Image.fromarray(src_frame).crop(
                (crop_left, crop_top, crop_left + target_w, crop_top + target_h)
            )
            if static_pil.size != (target_w, target_h):
                static_pil = static_pil.resize((target_w, target_h), Image.LANCZOS)
            img_clip = ImageClip(_np2.array(static_pil)).set_duration(total_duration)
        elif motion_active:
            img_clip = _make_motion_clip(base_img, motion_type, total_duration, kb_area, max_zoom)
        else:
            img_clip = _make_kb(base_img, kb_start, kb_end, total_duration, kb_area)

        if image_fit == 'fit':
            img_clip = img_clip.set_position(('center', 'top'))
    else:
        img_clip = ColorClip(size=canvas_size, color=(0, 0, 0)).set_duration(total_duration)

    bg_clip = ColorClip(size=canvas_size, color=BG_COLOR).set_duration(total_duration)

    # Assemble
    final_clip = CompositeVideoClip([bg_clip, img_clip] + subtitle_clips)
    if a_clip is not None:
        final_clip = final_clip.set_audio(a_clip)
    return final_clip


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Render Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def render_project(project_id: str, slides: list, assets_dir: Path, output_file: Path,
                   progress_callback, on_tts_done=None,
                   bgm_path: str = "", bgm_volume: float = 0.3,
                   canvas_size: tuple = None, tts_master_volume: float = 1.0,
                   subtitle_font_size: int = 28, subtitle_font_color: str = "white",
                   watermark_text: str = "", watermark_opacity: float = 0.3,
                   default_slide_duration: float = 3.0, title_text: str = "",
                   transition_duration: float = 0.7, style_preset: str = "none",
                   tts_voice: str = ""):
    """
    영상 렌더링 메인 함수
    slides: [{"image_filename": ..., "text": ...}, ...]
    on_tts_done: TTS 슬라이드 생성이 모두 끝난 직후 호용되는 콜백. GPU 메모리 해제 등에 활용.
    bgm_path: BGM 파일 절대 경로 (없으면 비어있음)
    canvas_size: (width, height) 튜플, None이면 CANVAS_SIZE(1280x720) 사용
    tts_master_volume: 전역 TTS 볼륨 (개별 tts_volume에 곱해짐)
    subtitle_font_size: 자막 폰트 크기 (기본 28)
    subtitle_font_color: 자막 폰트 색상 (기본 white)
    watermark_text: 워터마크 텍스트 (빈 문자열이면 미적용)
    watermark_opacity: 워터마크 불투명도 (0.0~1.0)
    default_slide_duration: 텍스트 없는 슬라이드 기본 시간(초)
    title_text: 인트로 타이틀 — 영상 시작 3초간 화면 중앙에 페이드 인/아웃 (빈 문자열이면 미적용)
    """
    if canvas_size is None:
        canvas_size = CANVAS_SIZE
    # 자막 폰트 설정 (전역) — 캔버스 높이 비례 스케일링 (1080p에서도 동일한 상대 크기, 8-9)
    _font_size = int((subtitle_font_size or FONT_SIZE) * canvas_size[1] / 720)
    _font_color = subtitle_font_color or TEXT_COLOR
    temp_dir = assets_dir / "temp_render"
    temp_dir.mkdir(exist_ok=True)
    # 이전 렌더링의 wav 캐시 제거 (텍스트 변경 시 재사용 방지)
    for _old_wav in temp_dir.glob("v_*.wav"):
        try: os.unlink(_old_wav)
        except: pass

    total_slides = len(slides)
    final_clips  = []
    slide_narration_segments = []  # 슬라이드별 TTS 구간 (BGM 덕킹용, 8-7)
    tts_engine   = load_tts_engine(tts_voice)

    try:
        progress_callback(0, "렌더링 시작...")

        for i, item in enumerate(slides):
            current_progress = int((i / total_slides) * 50)
            progress_callback(current_progress, f"슬라이드 처리 중 {i+1}/{total_slides}")

            segs = []  # 이 슬라이드의 TTS 음성 구간 (클립 기준 상대 시간, BGM 덕킹용)
            clip = build_slide_clip(
                item, i, assets_dir, temp_dir, canvas_size,
                tts_engine=tts_engine,
                font_size=_font_size, font_color=_font_color,
                default_slide_duration=default_slide_duration,
                tts_master_volume=tts_master_volume,
                narration_segments=segs,
                style_preset=style_preset,
            )
            if clip is not None:
                final_clips.append(clip)
                slide_narration_segments.append(segs)


        # ── B2: 비트 스냅 컷 ────────────────────────────────────────────────
        # 자유 길이(텍스트 없는) 이미지 슬라이드의 종료 시점을 BGM 비트에 정렬.
        # TTS/텍스트/비디오 슬라이드는 길이 불변 (나레이션/자막 박자 보존).
        # transition overlap 은 transition 타입+duration 의 함수(crossfade=duration, else 0)이므로
        # apply_transition 전에 글로벌 clock 을 단일 패스로 계산 가능.
        try:
            rhythm_cfg = _resolve_rhythm_cfg(style_preset)
            if (rhythm_cfg and rhythm_cfg.get("beat_snap") and bgm_path
                    and len(final_clips) > 0):
                beats = detect_beats(bgm_path)
                if beats.size > 0:
                    tol  = float(rhythm_cfg.get("tolerance", 0.45))
                    dmin = float(rhythm_cfg.get("min", 2.0))
                    dmax = float(rhythm_cfg.get("max", 6.0))
                    transitions = [item.get('transition', 'none') for item in slides]
                    _ov = lambda t: transition_duration if t == 'crossfade' else 0.0

                    clock = 0.0
                    free_durs = {}
                    for idx in range(len(final_clips)):
                        dur = final_clips[idx].duration or 0.0
                        if idx > 0:
                            clock -= _ov(transitions[idx] if idx < len(transitions) else 'none')
                        s = slides[idx]
                        # 자유 길이 = 텍스트 없는 이미지 슬라이드만
                        is_free = (not s.get('text')) and bool(s.get('image_filename')) \
                                  and not s.get('video_filename')
                        if is_free:
                            target = nearest_beat(beats, clock + dur, tol)
                            if target is not None:
                                new_dur = max(dmin, min(dmax, target - clock))
                                if new_dur > 0 and abs(new_dur - dur) > 0.05:
                                    free_durs[idx] = new_dur
                                    dur = new_dur
                        clock += dur

                    # 스냅된 클립만 rebuild (TTS 無 → 비용 저렴)
                    for idx, nd in free_durs.items():
                        segs = slide_narration_segments[idx] if idx < len(slide_narration_segments) else []
                        nb = build_slide_clip(
                            slides[idx], idx, assets_dir, temp_dir, canvas_size,
                            tts_engine=tts_engine,
                            font_size=_font_size, font_color=_font_color,
                            default_slide_duration=default_slide_duration,
                            tts_master_volume=tts_master_volume,
                            narration_segments=segs,
                            style_preset=style_preset,
                            free_slide_duration=nd,
                        )
                        if nb is not None:
                            final_clips[idx] = nb
                    if free_durs:
                        logger.info(f"B2 beat-snap: {len(free_durs)}/{len(final_clips)} "
                                    f"free slides snapped to beats (clock end={clock:.2f}s)")
        except Exception as _b2_err:
            logger.warning(f"B2 beat-snap skipped (using unsnapped clips): {_b2_err}")


        # 클립 인코딩 - TTS 완료 직후 GPU 메모리 해제 후 NVENC 인코딩
        progress_callback(50, "클립 합치는 중...")
        if not final_clips:
            raise Exception("렌더링할 클립이 없습니다 (텍스트가 입력된 슬라이드가 필요합니다)")

        # ── 전환 효과 적용 ────────────────────────────────────────────────
        # slides 데이터에서 transition 정보를 가져와 인접 클립 간 적용
        # transition은 '해당 슬라이드가 시작될 때' 적용되는 효과
        transition_overlaps = [0.0] * len(final_clips)  # idx 슬라이드가 앞 슬라이드와 겹치는 시간
        if len(final_clips) > 1:
            transitions = [item.get('transition', 'none') for item in slides]
            composed_clips = [final_clips[0]]
            current_end = final_clips[0].duration  # 누적 시작 오프셋

            for idx in range(1, len(final_clips)):
                clip_prev = composed_clips[-1]
                clip_next = final_clips[idx]
                trans = transitions[idx] if idx < len(transitions) else 'none'

                clip_prev_out, clip_next_in, overlap = apply_transition(clip_prev, clip_next, trans, duration=transition_duration)
                composed_clips[-1] = clip_prev_out
                composed_clips.append(clip_next_in)
                transition_overlaps[idx] = overlap

                if overlap > 0:
                    logger.info(f"Slide {idx}: applying '{trans}' transition (overlap={overlap}s)")
                else:
                    logger.info(f"Slide {idx}: applying '{trans}' transition")

            final_clips = composed_clips
        # ─────────────────────────────────────────────────────────────────

        # ── 임시 연결 (BGM 문제 없음) ─────────────────────────────────
        # 클립 연결
        combined = concatenate_videoclips(final_clips, method="compose")

        # ── 워터마크 오버레이 ──────────────────────────────────────
        if watermark_text:
            try:
                wm_clip = (
                    TextClip(
                        txt=watermark_text, font=FONT_PATH, fontsize=max(16, _font_size // 2),
                        color='white', method='label'
                    )
                    .set_duration(combined.duration)
                    .set_position(('right', 'bottom'))
                    .margin(right=20, bottom=20, opacity=0)
                    .set_opacity(watermark_opacity)
                )
                combined = CompositeVideoClip([combined, wm_clip])
                logger.info(f"Watermark applied: '{watermark_text}' opacity={watermark_opacity}")
            except Exception as wm_err:
                logger.warning(f"Watermark failed (skipping): {wm_err}")

        # ── 인트로 타이틀 오버레이 (화면 중앙, 3초간 페이드 인/아웃) ──
        if title_text:
            try:
                TITLE_DURATION = 3.0
                t_dur = min(TITLE_DURATION, combined.duration)
                fade  = min(0.8, t_dur * 0.3)
                # 자막보다 큰 폰트로 제목 렌더
                title_txt_clip = TextClip(
                    txt=title_text, font=FONT_PATH,
                    fontsize=max(48, _font_size * 2),
                    color='white', method='caption', align='center',
                    size=(canvas_size[0] - 120, None),
                )
                # 텍스트 뒤에 반투명 검정 배경 박스 (가독성 향상)
                title_clip = (
                    title_txt_clip
                    .on_color(
                        size=(title_txt_clip.w + 60, title_txt_clip.h + 40),
                        color=(0, 0, 0), pos='center', col_opacity=0.5,
                    )
                    .set_start(0)
                    .set_duration(t_dur)
                    .set_position('center')
                    .crossfadein(fade)
                    .crossfadeout(fade)
                )
                combined = CompositeVideoClip([combined, title_clip])
                logger.info(f"Intro title applied: '{title_text}' ({t_dur}s)")
            except Exception as title_err:
                logger.warning(f"Intro title failed (skipping): {title_err}")

        # ── 시네마틱 후반 (자동 색보정/질감, Phase A) ──────────────
        if style_preset and style_preset != "none":
            try:
                combined = apply_cinematic(combined, style_preset, canvas_size)
                logger.info(f"Style preset applied: {style_preset}")
            except Exception as style_err:
                logger.warning(f"Style preset failed (skipping): {style_err}")

        # ── BGM 믹싱 ───────────────────────────────────────────────
        if bgm_path and Path(bgm_path).exists():
            try:
                from moviepy.editor import CompositeAudioClip
                bgm_clip = AudioFileClip(bgm_path)
                total_duration = combined.duration

                # BGM이 영상보다 짧으면 루프
                if bgm_clip.duration < total_duration:
                    loops = int(total_duration / bgm_clip.duration) + 1
                    from moviepy.editor import concatenate_audioclips
                    bgm_clip = concatenate_audioclips([bgm_clip] * loops)
                bgm_clip = (bgm_clip
                    .subclip(0, total_duration)
                    .volumex(bgm_volume)
                    .audio_fadeout(min(2.0, total_duration * 0.05)))

                # ── BGM 덕킹 (8-7): TTS 나레이션 구간에서 BGM 볼륨 자동 감소 ──
                # 슬라이드별 상대 구간 + 전환 overlap을 반영해 글로벌 타임라인으로 환산
                global_narr_segs = []
                _t = 0.0
                for _idx, _clip in enumerate(final_clips):
                    if _idx > 0:
                        _t += final_clips[_idx - 1].duration - transition_overlaps[_idx]
                    for (_s, _e) in slide_narration_segments[_idx]:
                        global_narr_segs.append((_t + _s, _t + _e))

                if global_narr_segs:
                    BGM_DUCK_VOLUME = 0.25  # 나레이션 중 BGM 볼륨 (25%)
                    BGM_DUCK_FADE   = 0.4   # 덕킹 페이드 시간
                    for (_s, _e) in global_narr_segs:
                        bgm_clip = bgm_clip.fl(
                            _make_volume_duck_filter(_s, _e, BGM_DUCK_VOLUME, BGM_DUCK_FADE))
                    logger.info(f"BGM ducking applied: {len(global_narr_segs)} narration segments")

                if combined.audio is not None:
                    mixed_audio = CompositeAudioClip([combined.audio, bgm_clip])
                else:
                    mixed_audio = bgm_clip
                combined = combined.set_audio(mixed_audio)
                logger.info(f"BGM mixed: {bgm_path} vol={bgm_volume}")
            except Exception as bgm_err:
                logger.warning(f"BGM mixing failed (skipping): {bgm_err}")
        # ────────────────────────────────────────────────────

        # TTS 완료 콜백 호출 (GPU 메모리 해제 등)
        if on_tts_done:
            try:
                on_tts_done()
            except Exception as cb_err:
                logger.warning(f"on_tts_done callback error: {cb_err}")

        mp_logger  = WorkerProgressLogger(progress_callback, base_progress=50)
        temp_audio = temp_dir / "temp_audio.mp3"

        # GPU NVENC 인코딩 시도, 실패 시 CPU 인코딩으로 안전하게 폴백
        # ffmpeg_params: -pix_fmt yuv420p 강제
        #   - h264_nvenc는 RGB 입력을 gbrp(GBR 플레이너)로 인코딩해 색왜곡(초록 우세) 발생
        #   - yuv420p 명시로 두 코덱 모두 표준 색공간 사용
        for video_codec in ("h264_nvenc", "libx264"):
            try:
                logger.info(f"영상 인코딩 중... codec={video_codec}")
                progress_callback(55, f"영상 인코딩 중... ({video_codec})")
                combined.write_videofile(
                    str(output_file),
                    fps            = 24,
                    logger         = mp_logger,
                    temp_audiofile = str(temp_audio),
                    codec          = video_codec,
                    audio_codec    = 'libmp3lame',
                    ffmpeg_params  = ["-pix_fmt", "yuv420p"]
                )
                logger.info(f"인코딩 완료: {video_codec}")
                break  # 성공이면 루프 탈출
            except Exception as enc_err:
                logger.warning(f"{video_codec} 인코딩 실패 → 다음 코덱으로 전환: {enc_err}")
                if video_codec == "libx264":
                    raise  # 마지막 폴백도 실패 → 상위로 에러 전파

        # ── 음량 정규화 (loudnorm) ──────────────────────────────────────────
        # MoviePy는 오디오를 미리 인코딩해 mux 단계에서 -c:a copy 로 합치므로
        # write_videofile 에 -af 를 줄 수 없다 → 인코딩 후 별도 ffmpeg 패스로
        # 오디오만 -14 LUFS(YouTube 기준)로 정규화 (비디오 스트림은 copy, 무손실)
        try:
            import subprocess
            progress_callback(95, "음량 정규화 중...")
            tmp_norm = output_file.with_suffix(".norm.mp4")
            norm = subprocess.run(
                ["ffmpeg", "-y", "-i", str(output_file),
                 "-c:v", "copy",
                 "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                 "-c:a", "aac", "-b:a", "192k",
                 str(tmp_norm)],
                capture_output=True, timeout=1800,
            )
            if norm.returncode == 0 and tmp_norm.exists() and tmp_norm.stat().st_size > 1000:
                os.replace(tmp_norm, output_file)
                logger.info("loudnorm 음량 정규화 완료: -14 LUFS")
            else:
                logger.warning(f"loudnorm 실패 (원본 유지): {norm.stderr[-300:] if norm.stderr else 'unknown'}")
                try: tmp_norm.unlink()
                except Exception: pass
        except Exception as norm_err:
            logger.warning(f"음량 정규화 스킵: {norm_err}")

        progress_callback(100, "렌더링 완료!")
        # 임시 mp3 파일만 삭제 (wav 파일은 재렌더링을 위해 보존)
        cleanup_temp_files(temp_dir)
        return True

    except Exception as e:
        logger.error(f"Rendering failed: {e}", exc_info=True)
        # WAV 캐시는 보존하고 중간 결과물(mp3, mp4 임시파일)만 삭제
        try:
            for f in temp_dir.glob("*"):
                if f.suffix.lower() != ".wav":
                    try: f.unlink()
                    except: pass
        except: pass
        raise

# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# End of file
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
