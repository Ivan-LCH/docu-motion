"""
DocuMotion - 가져온 미디어 파일 → 슬라이드 (Google 포토 / Google 드라이브 공용)

  · 영상: HEVC(H.265) → H.264 트랜스코딩 (Chrome 재생 호환), ffprobe 로 촬영 시각(creation_time)
  · 사진: HEIC/HEIF → JPEG 변환 (ffmpeg, 실패 시 호출 측 폴백), EXIF 촬영 시각/GPS
  · ffprobe 길이 조회 (간편 자동 길이 추정·미리보기용, 파일 mtime 캐시)
  · 영상 포스터 jpg · 긴 영상 미리 자르기(스트림 복사) · 렌더용 클립 준비(구간·회전·캔버스 맞춤)
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
        vmeta = video_slide_meta(path)          # 길이 + 포스터 jpg (썸네일에 영상을 싣지 않도록)
        slide = Slide(project_id=project_id, order_index=order_index, image_filename="",
                      label=original_name, text="", slide_type="video", video_filename=path.name,
                      volume=1.0, subtitles="[]", use_tts=1, trim_start=0.0, trim_end=0.0,
                      exif=json.dumps(exif, ensure_ascii=False) if exif else "{}",
                      meta=json.dumps(vmeta, ensure_ascii=False) if vmeta else "{}")
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


# ────────────────────────────────────────────────────────────────────────── #
# 영상: 스트림 정보 · 포스터 · 긴 영상 미리 자르기 · 렌더용 클립 준비 (모두 ffmpeg 서브프로세스 —
# 영상을 메모리에 통째로 올리지 않는다)
# ────────────────────────────────────────────────────────────────────────── #
LONG_VIDEO_CAP = float(os.getenv("LONG_VIDEO_CAP", "60"))   # 이보다 길면 가져올 때 디스크에서 미리 자른다
PRETRIM_KEEP = 15.0      # 미리 자를 때 남길 길이 (가운데 기준) — 간편 클립(6초) + 앞뒤 여유
POSTER_MAX = 480         # 포스터 jpg 긴 변(px)


def probe_video(path) -> dict:
    """{duration, width, height, rotation, has_video, has_audio}. width/height 는 회전 적용 후(화면 기준)."""
    out = {"duration": None, "width": 0, "height": 0, "rotation": 0, "has_video": False, "has_audio": False}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
             "-of", "json", str(path)], capture_output=True, timeout=30)
        if r.returncode != 0:
            return out
        data = json.loads(r.stdout or b"{}")
    except Exception as e:
        logger.debug(f"ffprobe 실패 {path}: {e}")
        return out
    fmt = data.get("format") or {}
    if fmt.get("duration") not in (None, "N/A"):
        try:
            out["duration"] = float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "audio":
            out["has_audio"] = True
        elif kind == "video" and not out["has_video"]:
            out["has_video"] = True
            w, h = int(st.get("width") or 0), int(st.get("height") or 0)
            rot = 0
            for sd in st.get("side_data_list") or []:
                if sd.get("rotation") not in (None, ""):
                    rot = int(round(float(sd["rotation"])))
            if not rot and (st.get("tags") or {}).get("rotate"):
                rot = int(float(st["tags"]["rotate"]))
            rot = rot % 360
            out["rotation"] = rot
            out["width"], out["height"] = (h, w) if rot in (90, 270) else (w, h)
    return out


def poster_name(video_filename: str) -> str:
    return f"{Path(video_filename).stem}.poster.jpg"


def make_poster(video_path, at: Optional[float] = None) -> Optional[Path]:
    """영상 한 프레임 → 같은 폴더의 '<stem>.poster.jpg' (긴 변 POSTER_MAX). 실패 시 None."""
    video_path = Path(video_path)
    dst = video_path.with_name(poster_name(video_path.name))
    if at is None:
        d = ffprobe(video_path)["duration"] or 0.0
        at = d / 2.0 if d else 0.0
    vf = f"scale={POSTER_MAX}:{POSTER_MAX}:force_original_aspect_ratio=decrease"
    for ss in (max(0.0, at), 0.0):
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.3f}", "-i", str(video_path),
                                "-frames:v", "1", "-vf", vf, "-q:v", "5", str(dst)],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and dst.exists() and dst.stat().st_size > 200:
                return dst
        except Exception as e:
            logger.warning(f"포스터 생성 실패 {video_path.name}: {e}")
    try:
        dst.unlink()
    except OSError:
        pass
    return None


def pretrim_long_video(path, cap: float = LONG_VIDEO_CAP, keep: float = PRETRIM_KEEP) -> bool:
    """cap 초보다 긴 영상은 가운데 keep 초만 남긴다 (스트림 복사 — 재인코딩·디코딩 없음, 키프레임 경계;
    키프레임이 드물어 너무 길게 남으면 그 구간만 veryfast 재인코딩).
    간편 클립 구간(가운데 6초)이 그 안에 들어간다. 바꿨으면 True. 실패하면 원본 유지."""
    path = Path(path)
    d = ffprobe(path)["duration"]
    if not d or d <= cap:
        return False
    start = max(0.0, d / 2.0 - keep / 2.0)
    tmp = path.with_name(f"{path.stem}.trim.part{path.suffix}")
    base = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{keep:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?"]
    tail = ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(base + ["-c", "copy"] + tail, capture_output=True, timeout=300)
        nd = ffprobe(tmp)["duration"] if tmp.exists() else None
        if r.returncode != 0 or not nd or nd > keep * 1.5:
            # 키프레임 간격이 너무 길면(스트림 복사가 앞을 많이 끌고 옴) 그 구간만 빠르게 다시 인코딩
            r = subprocess.run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"] + tail,
                               capture_output=True, timeout=600)
            nd = ffprobe(tmp)["duration"] if tmp.exists() else None
        if r.returncode != 0 or not nd or nd < 1.0:
            logger.warning(f"긴 영상 미리 자르기 실패 {path.name}: {r.stderr[-200:]!r}")
            return False
        os.replace(tmp, path)
        logger.info(f"긴 영상 미리 자르기: {path.name} {d:.1f}s → {nd:.1f}s")
        return True
    except Exception as e:
        logger.warning(f"긴 영상 미리 자르기 오류 {path.name}: {e}")
        return False
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def video_slide_meta(path) -> dict:
    """영상 슬라이드 meta: {video_duration, poster} (포스터는 가운데 프레임 — 간편 클립 구간과 같은 곳)."""
    d = ffprobe(path)["duration"]
    meta: dict = {}
    if d:
        meta["video_duration"] = round(d, 3)
    poster = make_poster(path, (d / 2.0) if d else 0.0)
    if poster:
        meta["poster"] = poster.name
    return meta


def clip_fit_mode(src_w: int, src_h: int, canvas: tuple, fit: str) -> str:
    """'auto' → 화면비가 캔버스와 비슷하면(±20%) 'cover'(살짝 잘라 꽉 채움), 아니면 'blur'(흐린 배경 + 전체)."""
    if fit != "auto":
        return fit
    if not src_w or not src_h:
        return "blur"
    ratio = (src_w / src_h) / (canvas[0] / canvas[1])
    return "cover" if 1 / 1.2 <= ratio <= 1.2 else "blur"


def clip_filter(mode: str, canvas: tuple, fps: int = 24) -> str:
    W, H = int(canvas[0]) // 2 * 2, int(canvas[1]) // 2 * 2
    tail = f"fps={fps},format=yuv420p,setsar=1"
    if mode == "cover":
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},{tail}[v]"
    if mode == "blur":
        w4, h4 = W // 4 // 2 * 2, H // 4 // 2 * 2
        r = max(2, min(w4, h4) // 20)
        return (f"[0:v]split=2[a][b];"
                f"[a]scale={w4}:{h4}:force_original_aspect_ratio=increase,crop={w4}:{h4},"
                f"boxblur={r}:2,eq=brightness=-0.06:saturation=0.85,scale={W}:{H}[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}[v]")
    # letterbox: 회전만 굽고 크기는 그대로 (렌더러가 기존처럼 높이에 맞춘다)
    return f"[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,{tail}[v]"


def prepare_clip(src, dst, ts: float = 0.0, te: float = 0.0, canvas: Optional[tuple] = None,
                 fit: str = "letterbox", audio: bool = True, fps: int = 24) -> Path:
    """렌더용 클립: 구간만 잘라(ts~te, te<=ts 면 끝까지) 회전 메타데이터를 픽셀에 굽고 캔버스에 맞춘다.
    fit: 'auto' | 'cover' | 'blur' | 'letterbox'. 실패 시 RuntimeError."""
    src, dst = Path(src), Path(dst)
    info = probe_video(src)
    mode = clip_fit_mode(info["width"], info["height"], canvas, fit) if canvas else "letterbox"
    if mode != "letterbox" and not canvas:
        mode = "letterbox"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if ts and ts > 0:
        cmd += ["-ss", f"{ts:.3f}"]
    cmd += ["-i", str(src)]
    if te and te > ts:
        cmd += ["-t", f"{te - ts:.3f}"]
    cmd += ["-filter_complex", clip_filter(mode, canvas or (0, 0), fps), "-map", "[v]"]
    if audio and info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "44100"]
    else:
        cmd += ["-an"]
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(fps),
            "-threads", "4", "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
            raise RuntimeError(f"클립 준비 실패 {src.name}: {r.stderr[-300:]!r}")
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    logger.info(f"클립 준비: {src.name} [{ts:.2f}~{te:.2f}] {mode} rot={info['rotation']} → {dst.name}")
    return dst
