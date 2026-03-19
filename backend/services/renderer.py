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

from backend.core.config import CANVAS_SIZE, FONT_PATH, TTS_SERVER_URL, TTS_VOICE_NAME
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
# Apply Transition
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def apply_transition(clip_a, clip_b, transition: str):
    """
    clip_a → clip_b 사이에 전환 효과를 적용하여 연결된 클립 리스트를 반환합니다.
    각 클립의 start 오프셋은 concatenate 방식 대신 직접 배치합니다.

    반환: (처리된 clip_a, 처리된 clip_b, offset_delta)
      offset_delta = clip_a가 끝나기 전에 clip_b가 시작되는 시간 (겹침)
    """
    t = TRANSITION_DURATION
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

def load_tts_engine():
    if TTSEngine is None:
        return None
    try:
        return TTSEngine(server_url=TTS_SERVER_URL, voice_name=TTS_VOICE_NAME)
    except Exception as e:
        logger.error(f"TTS Engine Load Error: {e}")
        return None


# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #
# Render Project
# ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── #

def render_project(project_id: str, slides: list, assets_dir: Path, output_file: Path,
                   progress_callback, on_tts_done=None,
                   bgm_path: str = "", bgm_volume: float = 0.3,
                   canvas_size: tuple = None, tts_master_volume: float = 1.0):
    """
    영상 렌더링 메인 함수
    slides: [{"image_filename": ..., "text": ...}, ...]
    on_tts_done: TTS 슬라이드 생성이 모두 끝난 직후 호용되는 콜백. GPU 메모리 해제 등에 활용.
    bgm_path: BGM 파일 절대 경로 (없으면 비어있음)
    canvas_size: (width, height) 튜플, None이면 CANVAS_SIZE(1280x720) 사용
    tts_master_volume: 전역 TTS 볼륨 (개별 tts_volume에 곱해짐)
    """
    if canvas_size is None:
        canvas_size = CANVAS_SIZE
    temp_dir = assets_dir / "temp_render"
    temp_dir.mkdir(exist_ok=True)

    total_slides = len(slides)
    final_clips  = []
    tts_engine   = load_tts_engine()

    try:
        progress_callback(0, "렌더링 시작...")

        for i, item in enumerate(slides):
            text = item.get('text', '').strip()
            slide_type = item.get('slide_type', 'image')

            current_progress = int((i / total_slides) * 50)
            progress_callback(current_progress, f"슬라이드 처리 중 {i+1}/{total_slides}")

            a_clip = None
            subtitle_clips = []

            # ── Video Slide ──────────────────────────────────
            if slide_type == 'video':
                import json as _json
                video_filename = item.get('video_filename', '')
                video_path = assets_dir / video_filename
                if not video_path.exists():
                    logger.error(f"Video file not found: {video_path}")
                    continue

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
                        tc = (
                            TextClip(
                                txt=sub_txt, font=FONT_PATH, fontsize=FONT_SIZE,
                                color=TEXT_COLOR, size=(canvas_size[0] - 100, None),
                                method='caption', align='center', interline=8
                            )
                            .set_start(start)
                            .set_duration(min(dur, total_duration - start))
                            .set_position(('center', canvas_size[1] - 90))
                        )
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
                        # MoviePy는 구간별 볼륨을 직접 지원하지 않으므로
                        # volumex 필터 함수로 구현
                        prev_audio = ducked_audio
                        def _make_duck_filter(t_start, t_end, duck_vol, fade):
                            def volume_filter(get_frame, t):
                                frame = get_frame(t)
                                if t_start - fade <= t <= t_end + fade:
                                    if t < t_start:
                                        mix = (t - (t_start - fade)) / fade
                                        vol = 1.0 - mix * (1.0 - duck_vol)
                                    elif t > t_end:
                                        mix = (t - t_end) / fade
                                        vol = duck_vol + mix * (1.0 - duck_vol)
                                    else:
                                        vol = duck_vol
                                    return frame * vol
                                return frame
                            return volume_filter
                        ducked_audio = ducked_audio.fl(_make_duck_filter(tts_start, tts_end, DUCK_VOLUME, FADE_DUR))
                    audio_clips.append(ducked_audio)
                    audio_clips.extend(tts_clip for _, _, tts_clip in tts_audio_clips)
                elif original_audio:
                    audio_clips.append(original_audio)
                else:
                    audio_clips.extend(tts_clip for _, _, tts_clip in tts_audio_clips)

                bg_clip = ColorClip(size=canvas_size, color=BG_COLOR).set_duration(total_duration)
                video_clip = video_clip.set_fps(24)
                final_clip = CompositeVideoClip([bg_clip, video_clip] + subtitle_clips)
                if audio_clips:
                    final_clip = final_clip.set_audio(CompositeAudioClip(audio_clips))

                final_clips.append(final_clip)
                continue  # 이미지 슬라이드 로직 건너뜀

            # ── Image Slide ──────────────────────────────────
            use_tts_flag = bool(item.get('use_tts', 1))

            if not text:
                # 텍스트가 없으면 3초간 이미지 유지
                total_duration = 3.0
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
                    txt_clip = (
                        TextClip(
                            txt       = s, 
                            font      = FONT_PATH, 
                            fontsize  = FONT_SIZE,
                            color     = TEXT_COLOR, 
                            size      = (canvas_size[0] - 100, None),
                            method    = 'caption',
                            align     = 'center',
                            interline = 8
                        )
                        .set_start(current_start)
                        .set_duration(dur)
                        .set_position(('center', canvas_size[1] - 90))
                    )
                    subtitle_clips.append(txt_clip)
                    current_start += dur
            else:
                # TTS - 이미 생성된 파일이 있으면 재사용 (재렌더링 속도 향상)
                a_path = temp_dir / f"v_{i}.wav"
                
                if a_path.exists() and a_path.stat().st_size < 100:
                    try: a_path.unlink()
                    except: pass
                    
                if a_path.exists():
                    logger.info(f"Reusing existing TTS file: {a_path}")
                elif tts_engine:
                    tts_engine.generate_with_fallback(text, str(a_path))

                if not a_path.exists() or a_path.stat().st_size < 100:
                    raise Exception(f"TTS 오디오 생성 실패 또는 타임아웃 (슬라이드 {i+1})")

                # Audio 정상 생성됨
                a_clip = AudioFileClip(str(a_path))
                # TTS 볼륨 독립 조절 (개별 * 마스터)
                tts_vol = item.get('tts_volume', 1.0) * tts_master_volume
                if tts_vol != 1.0:
                    a_clip = a_clip.volumex(tts_vol)
                PAD_DELAY      = 0.5
                total_duration = a_clip.duration + PAD_DELAY * 2
                a_clip         = a_clip.set_start(PAD_DELAY)

                # Subtitle timing
                sentences   = split_sentences(text)
                num_sent    = len(sentences)
                total_chars = sum(len(s) for s in sentences)
                SENTENCE_GAP       = 0.15
                total_gap_time     = SENTENCE_GAP * (num_sent - 1) if num_sent > 1 else 0
                available_sub_time = max(0, a_clip.duration - total_gap_time)

                current_start  = PAD_DELAY + 0.1
                for idx, s in enumerate(sentences):
                    char_ratio = len(s) / total_chars if total_chars > 0 else 1.0 / num_sent
                    dur = char_ratio * available_sub_time
                    txt_clip = (
                        TextClip(
                            txt=s, font=FONT_PATH, fontsize=FONT_SIZE,
                            color=TEXT_COLOR, size=(canvas_size[0] - 100, None),
                            method='caption', align='center', interline=8
                        )
                        .set_start(current_start)
                        .set_duration(dur)
                        .set_position(('center', canvas_size[1] - 90))
                    )
                    subtitle_clips.append(txt_clip)
                    current_start += dur
                    if idx < num_sent - 1:
                        current_start += SENTENCE_GAP

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

                # Ken Burns: 슬라이드별 줌인/줌아웃 번갈아 적용
                base_img = ImageClip(str(img_path)).resize(height=int(canvas_size[1] * 0.85))
                kb_start = 1.0
                kb_end = 1.08 if (i % 2 == 0) else 0.93  # 짝수: 줌인, 홀수: 줌아웃

                def _make_kb(clip, start_scale, end_scale, dur, csize):
                    def make_frame(t):
                        progress = t / dur if dur > 0 else 0
                        scale = start_scale + (end_scale - start_scale) * progress
                        from PIL import Image as PILImage
                        import numpy as np
                        frame = clip.get_frame(0)
                        h, w = frame.shape[:2]
                        new_w, new_h = int(w * scale), int(h * scale)
                        img = PILImage.fromarray(frame).resize((new_w, new_h), PILImage.LANCZOS)
                        # 중앙 크롭
                        left = (new_w - w) // 2
                        top = (new_h - h) // 2
                        cropped = img.crop((left, top, left + w, top + h))
                        return np.array(cropped)
                    from moviepy.editor import VideoClip
                    return VideoClip(make_frame, duration=dur).set_fps(24)

                img_clip = (
                    _make_kb(base_img, kb_start, kb_end, total_duration, canvas_size)
                    .set_position(('center', 'top'))
                )
            else:
                img_clip = ColorClip(size=canvas_size, color=(0, 0, 0)).set_duration(total_duration)

            bg_clip = ColorClip(size=canvas_size, color=BG_COLOR).set_duration(total_duration)

            # Assemble
            final_clip = CompositeVideoClip([bg_clip, img_clip] + subtitle_clips)
            if a_clip is not None:
                final_clip = final_clip.set_audio(a_clip)
            final_clips.append(final_clip)

        # 클립 인코딩 - TTS 완료 직후 GPU 메모리 해제 후 NVENC 인코딩
        progress_callback(50, "클립 합치는 중...")
        if not final_clips:
            raise Exception("렌더링할 클립이 없습니다 (텍스트가 입력된 슬라이드가 필요합니다)")

        # ── 전환 효과 적용 ────────────────────────────────────────────────
        # slides 데이터에서 transition 정보를 가져와 인접 클립 간 적용
        # transition은 '해당 슬라이드가 시작될 때' 적용되는 효과
        if len(final_clips) > 1:
            transitions = [item.get('transition', 'none') for item in slides]
            composed_clips = [final_clips[0]]
            current_end = final_clips[0].duration  # 누적 시작 오프셋

            for idx in range(1, len(final_clips)):
                clip_prev = composed_clips[-1]
                clip_next = final_clips[idx]
                trans = transitions[idx] if idx < len(transitions) else 'none'

                clip_prev_out, clip_next_in, overlap = apply_transition(clip_prev, clip_next, trans)
                composed_clips[-1] = clip_prev_out
                composed_clips.append(clip_next_in)

                if overlap > 0:
                    logger.info(f"Slide {idx}: applying '{trans}' transition (overlap={overlap}s)")
                else:
                    logger.info(f"Slide {idx}: applying '{trans}' transition")

            final_clips = composed_clips
        # ─────────────────────────────────────────────────────────────────

        # ── 임시 연결 (BGM 문제 없음) ─────────────────────────────────
        # 클립 연결
        combined = concatenate_videoclips(final_clips, method="compose")

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
