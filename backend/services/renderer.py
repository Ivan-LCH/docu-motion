"""
DocuMotion - Video Renderer (MoviePy 기반)
기존 renderer.py를 backend/services/ 으로 이식 (경로 설정만 변경)
"""
import os
import asyncio
import logging
import re
import shutil
from pathlib import Path

from moviepy.editor import (
    ImageClip, AudioFileClip, concatenate_videoclips,
    TextClip, CompositeVideoClip, ColorClip
)
from PIL import Image
import edge_tts
from proglog import ProgressBarLogger

try:
    from backend.services.tts_manager import TTSEngine
except ImportError:
    TTSEngine = None

from backend.core.config import CANVAS_SIZE, FONT_PATH, TTS_SERVER_URL, TTS_VOICE_NAME
from backend.core.logger import get_logger

logger = get_logger(__name__)

# PIL 호환성
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS

FONT_SIZE  = 28
TEXT_COLOR = 'white'
BG_COLOR   = (0, 0, 0)


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────
def split_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s for s in sentences if s]


def cleanup_temp_files(temp_dir: Path):
    for f in temp_dir.glob("v_*.wav"):
        try: os.unlink(f)
        except: pass
    for f in Path(".").glob("*TEMP_MPY*.mp3"):
        try: os.unlink(f)
        except: pass


class WorkerProgressLogger(ProgressBarLogger):
    def __init__(self, callback, base_progress=50):
        super().__init__()
        self._progress_callback = callback    # 실제 DB 업데이트용 콜백
        self.callback = lambda **kw: None     # ProgressBarLogger의 __call__ 우회 (0% 덮어쓰기 방지)
        self.base_progress = base_progress

    def bars_callback(self, bar, attr, value, old_value=None):
        if bar == 't' and attr == 'index':
            total = self.bars[bar]['total']
            if total > 0:
                percent = int(value / total * 100)
                overall = self.base_progress + int(percent * 0.5)
                self._progress_callback(overall, f"인코딩 중... ({percent}%)")


# ─────────────────────────────────────────────
# Render Logic
# ─────────────────────────────────────────────
def load_tts_engine():
    if TTSEngine is None:
        return None
    try:
        return TTSEngine(server_url=TTS_SERVER_URL, voice_name=TTS_VOICE_NAME)
    except Exception as e:
        logger.error(f"TTS Engine Load Error: {e}")
        return None


def render_project(project_id: str, slides: list, assets_dir: Path, output_file: Path, progress_callback):
    """
    영상 렌더링 메인 함수
    slides: [{"image_filename": ..., "text": ...}, ...]
    """
    temp_dir = assets_dir / "temp_render"
    temp_dir.mkdir(exist_ok=True)

    total_slides = len(slides)
    final_clips  = []
    tts_engine   = load_tts_engine()

    try:
        progress_callback(0, "렌더링 시작...")

        for i, item in enumerate(slides):
            text = item.get('text', '').strip()

            current_progress = int((i / total_slides) * 50)
            progress_callback(current_progress, f"슬라이드 처리 중 {i+1}/{total_slides}")

            a_clip = None
            subtitle_clips = []
            
            if not text:
                # 텍스트가 없으면 3초간 이미지 유지
                total_duration = 3.0
            else:
                # TTS - 이미 생성된 파일이 있으면 재사용 (재렌더링 속도 향상)
                a_path = temp_dir / f"v_{i}.wav"
                success = a_path.exists()

                if not success:
                    if tts_engine:
                        success = tts_engine.generate(text, str(a_path))
                    if not success:
                        try:
                            asyncio.run(edge_tts.Communicate(text, "ko-KR-SunHiNeural").save(str(a_path)))
                            success = True
                        except Exception as e:
                            logger.error(f"Edge-TTS failed: {e}")
                else:
                    logger.info(f"Reusing existing TTS file: {a_path}")

                if not success or not a_path.exists():
                    logger.error(f"Audio generation failed for slide {i+1}")
                    continue

                # Audio
                a_clip = AudioFileClip(str(a_path))
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
                            color=TEXT_COLOR, size=(CANVAS_SIZE[0] - 100, None),
                            method='caption', align='center', interline=8
                        )
                        .set_start(current_start)
                        .set_duration(dur)
                        .set_position(('center', CANVAS_SIZE[1] - 90))
                    )
                    subtitle_clips.append(txt_clip)
                    current_start += dur
                    if idx < num_sent - 1:
                        current_start += SENTENCE_GAP

            # Image clip
            img_filename = item.get('image_filename', '')
            img_path = Path(img_filename) if Path(img_filename).is_absolute() else assets_dir / img_filename

            if img_path.exists():
                img_clip = (
                    ImageClip(str(img_path))
                    .resize(height=int(CANVAS_SIZE[1] * 0.85))
                    .set_position(('center', 'top'))
                    .set_duration(total_duration)
                )
            else:
                img_clip = ColorClip(size=CANVAS_SIZE, color=(0, 0, 0)).set_duration(total_duration)

            bg_clip = ColorClip(size=CANVAS_SIZE, color=BG_COLOR).set_duration(total_duration)

            # Assemble
            final_clip = CompositeVideoClip([bg_clip, img_clip] + subtitle_clips)
            if a_clip is not None:
                final_clip = final_clip.set_audio(a_clip)
            final_clips.append(final_clip)

        # Encoding
        progress_callback(50, "클립 합치는 중...")
        if not final_clips:
            raise Exception("렌더링할 클립이 없습니다 (텍스트가 입력된 슬라이드가 필요합니다)")

        mp_logger  = WorkerProgressLogger(progress_callback, base_progress=50)
        temp_audio = temp_dir / "temp_audio.mp3"

        concatenate_videoclips(final_clips, method="compose").write_videofile(
            str(output_file),
            fps=24,
            logger=mp_logger,
            temp_audiofile=str(temp_audio),
            codec='libx264',
            audio_codec='libmp3lame'
        )
        progress_callback(100, "렌더링 완료!")
        # 임시 mp3 파일만 삭제 (wav 파일은 재렌더링을 위해 보존)
        cleanup_temp_files(temp_dir)
        return True

    except Exception as e:
        logger.error(f"Rendering failed: {e}", exc_info=True)
        try: shutil.rmtree(temp_dir)
        except: pass
        raise
