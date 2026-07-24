"""
DocuMotion - Slide Preview Service (6-19)
단일 슬라이드(또는 인접 슬라이드 + 트랜지션)를 저해상도·CPU(libx264)로 빠르게
렌더하여 캐시한다. 풀 렌더 없이 효과/자막 타이밍을 미리보기하기 위함.

설계:
  - renderer.build_slide_clip() (6-18) 을 재사용 → 풀 렌더와 동일 로직
  - GPU(h264_nvenc)는 풀 렌더 전용, 미리보기는 항상 CPU libx264 (GPU 회피)
  - 캐시: outputs/{project_id}/previews/{slide_id}_{hash}.mp4
  - TTS 토글 정책:
      * 기본(force_tts=False): 캐시된 v_*.wav 우선 사용. 일부 누락 시 use_tts=0 으로
        무음+추정길이 경로 우회 (build_slide_clip의 raise 회피, 수정 없음)
      * force_tts=True: TTS 엔진 로드 → 누락분 온디맨드 생성 (충실 오디오)
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from moviepy.editor import concatenate_videoclips

from backend.core.config import OUTPUTS_DIR, TTS_SERVER_URL, TTS_VOICE_NAME
from backend.core.logger import get_logger
from backend.db.session import SessionLocal
from backend.db.models import Project, Slide
from backend.services.renderer import (
    build_slide_clip, apply_transition, ASPECT_RATIO_MAP, split_sentences,
)

logger = get_logger(__name__)

PREVIEW_SCALE        = 0.5      # 캔버스 대비 축소 비율 (720p → 360p)
PREVIEW_FPS          = 24
PREVIEW_CODEC        = "libx264"  # CPU 고정 (GPU 회피)
PREVIEW_AUDIO_CODEC  = "libmp3lame"
MIN_VALID_MP4_BYTES  = 1000     # 이보다 작으면 렌더 실패로 간주
PREVIEW_TTL_KEEP     = 40       # 프로젝트당 보관 preview 파일 수 상한(stale 정리)

# 중복 렌더 방지용 in-flight 키 집합 (단일 프로세스)
_inflight_lock = threading.Lock()
_inflight: set[str] = set()


# ────────────────────────────────────────────────────────────────────────────
# 경로 헬퍼
# ────────────────────────────────────────────────────────────────────────────
def previews_dir(project_id: str) -> Path:
    d = OUTPUTS_DIR / project_id / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tts_cache_dir(project_id: str) -> Path:
    """미리보기 전용 TTS wav 캐시 (풀 렌더의 temp_render 정리에 영향받지 않음)"""
    d = previews_dir(project_id) / "_tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(project_id: str, slide_id: str, h: str) -> Path:
    return previews_dir(project_id) / f"{slide_id}_{h}.mp4"


# ────────────────────────────────────────────────────────────────────────────
# 직렬화 / 해시
# ────────────────────────────────────────────────────────────────────────────
def slide_to_item(s: Slide) -> dict:
    """Slide ORM → renderer 가 읽는 item dict (worker.py 직렬화와 동일)"""
    return {
        "image_filename": s.image_filename,
        "text": s.text,
        "slide_type": s.slide_type or "image",
        "video_filename": s.video_filename or "",
        "volume": s.volume if s.volume is not None else 1.0,
        "subtitles": s.subtitles or "[]",
        "use_tts": (s.use_tts if s.use_tts is not None else 1),
        "trim_start": s.trim_start or 0.0,
        "trim_end": s.trim_end or 0.0,
        "transition": s.transition or "none",
        "tts_volume": s.tts_volume if s.tts_volume is not None else 1.0,
        "rotation": s.rotation if s.rotation is not None else 0,
        "overlays": s.overlays or "[]",
        "image_fit": s.image_fit or "cover",
        "ken_burns": s.ken_burns if s.ken_burns is not None else 0,
        "meta": s.meta or "{}",
    }


def _asset_mtime(project_id: str, item: dict) -> float:
    assets = OUTPUTS_DIR / project_id / "assets"
    fn = item.get("video_filename") if item.get("slide_type") == "video" else item.get("image_filename")
    if not fn:
        return 0.0
    p = Path(fn) if Path(fn).is_absolute() else assets / fn
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _HASHABLE_FIELDS(it: dict, project_id: str) -> dict:
    return {
        "mt": _asset_mtime(project_id, it),
        "text": it.get("text", ""),
        "slide_type": it.get("slide_type"),
        "rotation": it.get("rotation"),
        "overlays": it.get("overlays", "[]"),
        "ken_burns": it.get("ken_burns"),
        "image_fit": it.get("image_fit"),
        "transition": it.get("transition"),
        "subtitles": it.get("subtitles", "[]"),
        "tts_volume": it.get("tts_volume"),
        "use_tts": it.get("use_tts"),
        "trim_start": it.get("trim_start"),
        "trim_end": it.get("trim_end"),
        "volume": it.get("volume"),
        "meta": it.get("meta", "{}"),
        "image_filename": it.get("image_filename", ""),
        "video_filename": it.get("video_filename", ""),
    }


def compute_hash(items: list, project: Project, force_tts: bool, include_neighbors: bool) -> str:
    payload = {
        "aspect": getattr(project, "aspect_ratio", "16:9"),
        "tts_master_volume": getattr(project, "tts_master_volume", 1.0),
        "font_size": getattr(project, "subtitle_font_size", 28),
        "font_color": getattr(project, "subtitle_font_color", "white"),
        "default_slide_duration": getattr(project, "default_slide_duration", 3.0),
        "force_tts": bool(force_tts),
        "include_neighbors": bool(include_neighbors),
        "slides": [_HASHABLE_FIELDS(it, project.id) for it in items],
    }
    js = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(js).hexdigest()[:12]


# ────────────────────────────────────────────────────────────────────────────
# 컨텍스트 로드 (POST/GET 동기 체크용 — 전달된 db 세션 사용)
# ────────────────────────────────────────────────────────────────────────────
def compute_context(db, project_id: str, slide_id: str,
                    include_neighbors: bool, force_tts: bool):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    slide = db.query(Slide).filter(Slide.id == slide_id,
                                   Slide.project_id == project_id).first()
    if not slide:
        return None

    all_slides = (db.query(Slide)
                  .filter(Slide.project_id == project_id)
                  .order_by(Slide.order_index).all())
    idx = next((i for i, s in enumerate(all_slides) if s.id == slide_id), None)

    target_item = slide_to_item(slide)
    items = [target_item]
    indices = [idx if idx is not None else 0]
    if include_neighbors and idx and idx > 0:
        items = [slide_to_item(all_slides[idx - 1]), target_item]
        indices = [idx - 1, idx]

    canvas_size = ASPECT_RATIO_MAP.get(
        getattr(project, "aspect_ratio", "16:9") or "16:9", (1280, 720))
    h = compute_hash(items, project, force_tts, include_neighbors)

    return {
        "project": project,
        "items": items,
        "indices": indices,
        "canvas_size": canvas_size,
        "hash": h,
        "cache_path": cache_path(project_id, slide_id, h),
        "slide_id": slide_id,
    }


# ────────────────────────────────────────────────────────────────────────────
# TTS 정책 헬퍼
# ────────────────────────────────────────────────────────────────────────────
def _needs_tts_but_missing(item: dict, slide_index: int, temp_dir: Path) -> bool:
    """이미지 슬라이드가 TTS 분기를 타야 하나 캐시 wav가 일부라도 없으면 True.
    비디오 슬라이드는 자막 전용 TTS라 누락돼도 raise하지 않으므로 제외."""
    if item.get("slide_type") == "video":
        return False
    text = (item.get("text") or "").strip()
    if not text or not bool(item.get("use_tts", 1)):
        return False
    sentences = split_sentences(text)
    if not sentences:
        return False
    for sent_idx in range(len(sentences)):
        wav = temp_dir / f"v_{slide_index}_{sent_idx}.wav"
        if not wav.exists() or wav.stat().st_size < 100:
            return True
    return False


def _load_tts_engine():
    from backend.services.tts_manager import TTSEngine
    tts = TTSEngine(server_url=TTS_SERVER_URL, voice_name=TTS_VOICE_NAME)
    for attempt in range(1, 4):
        if tts.load_model():
            return tts
        import time
        time.sleep(1)
    logger.warning("Preview: TTS load failed → edge-tts fallback 모드")
    return tts  # generate_with_fallback 이 edge-tts 폴백 처리


# ────────────────────────────────────────────────────────────────────────────
# 렌더 (백그라운드 태스크용 — 자체 DB 세션)
# ────────────────────────────────────────────────────────────────────────────
def render_slide_preview(project_id: str, slide_id: str,
                         include_neighbors: bool = False,
                         force_tts: bool = False) -> Path | None:
    db = SessionLocal()
    tts_engine = None
    clips = []
    try:
        ctx = compute_context(db, project_id, slide_id, include_neighbors, force_tts)
        if not ctx:
            return None
        cache = ctx["cache_path"]
        if cache.exists() and cache.stat().st_size >= MIN_VALID_MP4_BYTES:
            return cache  # hit (레이스 안전)

        project   = ctx["project"]
        items     = ctx["items"]
        indices   = ctx["indices"]
        canvas    = ctx["canvas_size"]
        assets    = OUTPUTS_DIR / project_id / "assets"
        temp_dir  = tts_cache_dir(project_id)

        font_size   = getattr(project, "subtitle_font_size", 28) or 28
        font_color  = getattr(project, "subtitle_font_color", "white") or "white"
        default_dur = getattr(project, "default_slide_duration", 3.0) or 3.0
        master_vol  = getattr(project, "tts_master_volume", 1.0) or 1.0

        tts_engine = _load_tts_engine() if force_tts else None

        for item, sidx in zip(items, indices):
            it = item
            # 기본 모드에서 캐시 TTS가 일부 누락 → 무음 추정 경로 우회 (raise 방지)
            if not force_tts and _needs_tts_but_missing(it, sidx, temp_dir):
                it = dict(item)
                it["use_tts"] = 0
            clip = build_slide_clip(
                it, sidx, assets, temp_dir, canvas,
                tts_engine=tts_engine,
                font_size=font_size, font_color=font_color,
                default_slide_duration=default_dur,
                tts_master_volume=master_vol,
            )
            if clip is not None:
                clips.append(clip)

        if not clips:
            logger.warning(f"Preview: 생성된 클립 없음 {project_id}/{slide_id}")
            return None

        # 인접 모드: 대상 슬라이드 진입 트랜지션 적용
        if len(clips) == 2:
            trans = items[-1].get("transition", "none")
            a_out, b_in, _overlap = apply_transition(clips[0], clips[1], trans)
            clips = [a_out, b_in]

        combined = (concatenate_videoclips(clips, method="compose")
                    if len(clips) > 1 else clips[0])
        combined = combined.resize(PREVIEW_SCALE)

        tmp_out = cache.with_suffix(".tmp.mp4")
        combined.write_videofile(
            str(tmp_out),
            fps=PREVIEW_FPS,
            codec=PREVIEW_CODEC,
            audio_codec=PREVIEW_AUDIO_CODEC,
            ffmpeg_params=["-pix_fmt", "yuv420p"],
            temp_audiofile=str(temp_dir / f"preview_{slide_id}.mp3"),
            logger=None,
        )
        os.replace(tmp_out, cache)
        _cleanup_stale(project_id, slide_id, cache)
        logger.info(f"Preview rendered: {project_id}/{slide_id} -> {cache.name}")
        return cache
    except Exception as e:
        logger.error(f"Preview render failed {project_id}/{slide_id}: {e}", exc_info=True)
        # 실패 시 잔여 tmp 정리
        try:
            for f in previews_dir(project_id).glob("*.tmp.mp4"):
                f.unlink()
        except Exception:
            pass
        return None
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        if tts_engine is not None:
            try:
                tts_engine.unload_model()
            except Exception:
                pass
        db.close()


def _cleanup_stale(project_id: str, slide_id: str, keep: Path):
    d = previews_dir(project_id)
    # 동일 슬라이드의 이전 해시 버전 제거
    for f in d.glob(f"{slide_id}_*.mp4"):
        if f != keep:
            try:
                f.unlink()
            except Exception:
                pass
    # 프로젝트 단위 총량 제한 (오래된 것부터)
    try:
        files = sorted(d.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[PREVIEW_TTL_KEEP:]:
            f.unlink()
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────────
# in-flight (중복 렌더 방지)
# ────────────────────────────────────────────────────────────────────────────
def inflight_key(project_id: str, slide_id: str, h: str) -> str:
    return f"{project_id}:{slide_id}:{h}"


def is_inflight(key: str) -> bool:
    with _inflight_lock:
        return key in _inflight


def mark_inflight(key: str):
    with _inflight_lock:
        _inflight.add(key)


def clear_inflight(key: str):
    with _inflight_lock:
        _inflight.discard(key)


def render_and_clear(project_id: str, slide_id: str,
                     include_neighbors: bool, force_tts: bool, key: str):
    try:
        render_slide_preview(project_id, slide_id, include_neighbors, force_tts)
    finally:
        clear_inflight(key)
