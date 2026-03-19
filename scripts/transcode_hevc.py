"""
기존 HEVC 영상 → H.264 일괄 트랜스코딩 스크립트
사용: python -m scripts.transcode_hevc
"""
import os
import subprocess
from pathlib import Path

from backend.db.session import get_db, init_db
from backend.db.models import Slide
from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger

logger = get_logger(__name__)


def _is_hevc(file_path: Path) -> bool:
    try:
        with open(file_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            read_size = min(fsize, 1024 * 1024)
            f.seek(fsize - read_size)
            tail = f.read(read_size)
            return b'hvc1' in tail or b'hev1' in tail
    except Exception:
        return False


def _transcode_to_h264(src: Path, dst: Path) -> bool:
    for codec in ["h264_nvenc", "libx264"]:
        try:
            cmd = [
                "ffmpeg", "-y", "-i", str(src),
                "-c:v", codec, "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(dst)
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode == 0 and dst.exists() and dst.stat().st_size > 1000:
                logger.info(f"Transcoded ({codec}): {src.name} → {dst.name}")
                return True
            logger.warning(f"{codec} failed: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"{codec} error: {e}")
    return False


def main():
    init_db()
    db = next(get_db())

    video_slides = db.query(Slide).filter(Slide.slide_type == "video").all()
    logger.info(f"Found {len(video_slides)} video slides to check")

    converted = 0
    for slide in video_slides:
        assets_dir = OUTPUTS_DIR / slide.project_id / "assets"
        video_path = assets_dir / slide.video_filename

        if not video_path.exists():
            logger.warning(f"File not found: {video_path}")
            continue

        if not _is_hevc(video_path):
            logger.info(f"Already H.264: {slide.video_filename}")
            continue

        h264_name = f"{video_path.stem}_h264.mp4"
        h264_path = assets_dir / h264_name

        logger.info(f"Transcoding: {slide.video_filename} → {h264_name}")
        if _transcode_to_h264(video_path, h264_path):
            os.unlink(video_path)
            slide.video_filename = h264_name
            converted += 1
        else:
            logger.error(f"Failed to transcode: {slide.video_filename}")

    if converted:
        db.commit()
    logger.info(f"Done: {converted}/{len(video_slides)} transcoded")


if __name__ == "__main__":
    main()
