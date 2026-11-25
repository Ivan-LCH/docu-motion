"""
DocuMotion - 가져온 미디어 파일 → 슬라이드 (Google 포토 / Google 드라이브 공용)

  · 영상: HEVC(H.265) → H.264 트랜스코딩 (Chrome 재생 호환), ffprobe 로 촬영 시각(creation_time)
  · 사진: HEIC/HEIF → JPEG 변환 (ffmpeg, 실패 시 호출 측 폴백), EXIF 촬영 시각/GPS
  · ffprobe 길이 조회 (간편 자동 길이 추정·미리보기용, 파일 mtime 캐시)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from backend.core.logger import get_logger
from backend.db.models import Slide
from backend.services.exif_service import extract_exif

logger = get_logger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".ts", ".3gp"}
HEIC_EXTS = {".heic", ".heif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"} | HEIC_EXTS


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:80]


def is_hevc(file_path) -> bool:
    """MP4 파일이 HEVC(H.265) 코덱인지 확인 — 끝 1MB 의 moov 에서 hvc1/hev1 검색"""
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


def transcode_to_h264(src, dst) -> bool:
    """HEVC → H.264 GPU(NVENC) 트랜스코딩, 실패 시 CPU(libx264) 폴백"""
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
            if result.returncode == 0 and Path(dst).exists() and Path(dst).stat().st_size > 1000:
                logger.info(f"Transcoded HEVC→H.264 ({codec}): {Path(src).name} → {Path(dst).name}")
                return True
            logger.warning(f"Transcode with {codec} failed: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"Transcode with {codec} error: {e}")
    return False


def heic_to_jpeg(src: Path) -> Optional[Path]:
    """HEIC/HEIF → 같은 이름의 .jpg (ffmpeg). 실패 시 None (호출 측에서 썸네일 폴백/건너뛰기)."""
    dst = src.with_suffix(".jpg")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(dst)],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000:
            return dst
    except Exception as e:
        logger.warning(f"HEIC 변환 실패 {src.name}: {e}")
    try:
        if dst.exists():
            dst.unlink()
    except OSError:
        pass
    return None


def ffprobe(path) -> dict:
    """{duration: float|None, creation_time: ISO|None}. 실패 시 빈 값."""
    out = {"duration": None, "creation_time": None}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time",
             "-of", "json", str(path)], capture_output=True, timeout=30)
        if r.returncode != 0:
            return out
        fmt = json.loads(r.stdout or b"{}").get("format", {}) or {}
        if fmt.get("duration") not in (None, "N/A"):
            out["duration"] = float(fmt["duration"])
        ct = (fmt.get("tags") or {}).get("creation_time")
        if ct:
            try:
                dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                if dt.year > 1971:          # 1970/1904 기본값은 촬영 시각이 아님
                    out["creation_time"] = dt.replace(tzinfo=None).isoformat(timespec="seconds")
            except ValueError:
                pass
    except Exception as e:
        logger.debug(f"ffprobe 실패 {path}: {e}")
    return out


@lru_cache(maxsize=512)
def _duration_cached(path: str, mtime: float) -> Optional[float]:
    return ffprobe(path)["duration"]


def media_duration(path) -> Optional[float]:
    """오디오/비디오 길이(초). 파일 mtime 기준 캐시."""
    p = Path(path)
    try:
        return _duration_cached(str(p), p.stat().st_mtime)
    except OSError:
        return None


def video_duration_fn(assets_dir: Path):
    """presets.static_slide_duration 용 콜백: item → 원본 영상 길이."""
    def fn(item: dict):
        name = item.get("video_filename") or ""
        return media_duration(assets_dir / name) if name else None
    return fn


def make_media_slide(project_id: str, path: Path, original_name: str, mime: str,
                     order_index: int, captured_hint: Optional[str] = None,
                     gps_hint: Optional[dict] = None) -> tuple[Slide, str, str]:
    """다운로드/업로드된 파일 → Slide (DB add 는 호출 측). 반환: (slide, 저장 파일명, 'image'|'video').
    HEVC 는 H.264 로 바꾸고, HEIC 는 JPEG 로 바꾼다 (실패 시 ValueError).
    captured_hint/gps_hint: 파일 EXIF 가 없을 때 쓸 촬영 시각/좌표 (예: Drive imageMediaMetadata)."""
    ext = path.suffix.lower()
    is_video = (mime or "").startswith("video/") or ext in VIDEO_EXTS
    if is_video:
        if is_hevc(path):
            logger.info(f"HEVC detected: {path.name}, transcoding to H.264...")
            h264 = path.with_name(f"{path.stem}_h264.mp4")
            if transcode_to_h264(path, h264):
                os.unlink(path)
                path = h264
            else:
                logger.warning(f"HEVC transcode failed, keeping original: {path.name}")
        info = ffprobe(path)
        captured = info.get("creation_time") or captured_hint
        exif = {"captured_at": captured, "gps": gps_hint} if (captured or gps_hint) else None
        slide = Slide(project_id=project_id, order_index=order_index, image_filename="",
                      label=original_name, text="", slide_type="video", video_filename=path.name,
                      volume=1.0, subtitles="[]", use_tts=1, trim_start=0.0, trim_end=0.0,
                      exif=json.dumps(exif, ensure_ascii=False) if exif else "{}")
        return slide, path.name, "video"

    if ext in HEIC_EXTS or (mime or "").lower() in ("image/heic", "image/heif"):
        jpg = heic_to_jpeg(path)
        if jpg is None:
            raise ValueError("HEIC 변환 실패")
        try:
            exif_src = extract_exif(path)        # 원본 HEIC 의 EXIF 를 먼저 읽는다
        except Exception:
            exif_src = {}
        os.unlink(path)
        path = jpg
    else:
        exif_src = None
    try:
        exif_data = exif_src if exif_src and (exif_src.get("captured_at") or exif_src.get("gps")) \
            else extract_exif(path)
    except Exception:
        exif_data = {"captured_at": None, "gps": None}
    if not exif_data.get("captured_at") and captured_hint:
        exif_data["captured_at"] = captured_hint
    if not exif_data.get("gps") and gps_hint:
        exif_data["gps"] = gps_hint
    exif_json = json.dumps(exif_data, ensure_ascii=False) \
        if (exif_data.get("captured_at") or exif_data.get("gps")) else "{}"
    slide = Slide(project_id=project_id, order_index=order_index, image_filename=path.name,
                  label=original_name, text="", exif=exif_json)
    return slide, path.name, "image"
