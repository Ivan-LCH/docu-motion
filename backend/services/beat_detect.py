"""
DocuMotion - BGM 비트 감지 (Auto-Style Phase B, Tier 2)

numpy-only 온셋/비트 추정. 컷 싱크용(나레이션 없는 자유 길이 슬라이드의 종료 시점을
비트에 스냅). 외부 의존성(librosa/scipy) 없이 동작.

파이프라인:
  1. ffmpeg(imageio_ffmpeg 번들 바이너리)로 BGM → mono 22050Hz float32 PCM 디코드
  2. hop=256 / win=512 로 RMS 에너지 엔벨로프
  3. positive diff (온셋 Strength) → 이동평균 스무스 → 0..1 정규화
  4. min_spacing 0.3s + 상대 임계(>0.25*max) 피크픽 → 초 단위 비트 시각 배열

★ 한계 (솔직): 시간영역 에너지 플럭스는 타악/온셋 중심 비트는 잘 잡지만,
  선율/약음 비트는 약하다. 컷 싱크 용도엔 충분하며, 감지 실패/빈 BGM 은 빈 배열을
  반환 → 호출자는 "스냅 안 함"으로 graceful 처리한다.

캐시: outputs/_beatcache/<sha(path|mtime|size)>.npy — 재렌더 시 재계산 회피.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger

logger = get_logger(__name__)

_SR = 22050          # 분석 샘플링레이트
_WIN = 512           # 분석 윈도우 샘플
_HOP = 256           # 홉 샘플
_MIN_SPACING = 0.30  # 비트 간 최소 간격(초)
_REL_THRESH = 0.25   # max 대비 상대 임계


def _ffmpeg_exe() -> Optional[str]:
    """moviepy 와 동일한 번들 ffmpeg 바이너리 경로. 없으면 None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        logger.debug(f"imageio_ffmpeg 사용 불가: {e}")
        return None


def _decode_mono(bgm_path: str) -> np.ndarray:
    """BGM → mono float32 PCM numpy 배열. 실패 시 빈 배열."""
    exe = _ffmpeg_exe()
    if not exe:
        return np.empty(0, dtype=np.float32)
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error",
             "-i", bgm_path, "-vn", "-ac", "1", "-ar", str(_SR),
             "-f", "f32le", "-"],
            capture_output=True, check=True,
        )
        return np.frombuffer(proc.stdout, dtype=np.float32).copy()
    except Exception as e:
        logger.debug(f"BGM 디코드 실패({bgm_path}): {e}")
        return np.empty(0, dtype=np.float32)


def _energy_envelope(x: np.ndarray) -> np.ndarray:
    """RMS 에너지 윈도우 시퀀스."""
    if x.size < _WIN:
        return np.empty(0)
    n = 1 + (x.size - _WIN) // _HOP
    env = np.empty(n, dtype=np.float32)
    for i in range(n):
        s = i * _HOP
        env[i] = np.sqrt(np.mean(x[s:s + _WIN] ** 2))
    return env


def _detect_beats_from_pcm(x: np.ndarray) -> np.ndarray:
    """PCM → 정렬된 비트 시각(초) 배열."""
    env = _energy_envelope(x)
    if env.size < 4:
        return np.empty(0)
    # 온셋: positive energy flux
    flux = np.maximum(env[1:] - env[:-1], 0.0)
    # 이동평균 스무스 (윈도우 5)
    k = 5
    if flux.size >= k:
        c = np.cumsum(flux)
        sm = (c[k - 1:] - np.concatenate(([0.0], c[:-k]))) / k
    else:
        sm = flux
    mx = float(sm.max()) if sm.size else 0.0
    if mx <= 1e-6:
        return np.empty(0)
    norm = sm / mx
    thr = _REL_THRESH

    # 피크픽 (국소 최대 + 임계 + min_spacing)
    peaks: list[float] = []
    last_t = -1e9
    for i in range(1, norm.size - 1):
        if norm[i] >= thr and norm[i] >= norm[i - 1] and norm[i] >= norm[i + 1]:
            t = (i + (k - 1) / 2.0) * _HOP / _SR  # 스무스 오프셋 보정 근사
            if t - last_t >= _MIN_SPACING:
                peaks.append(t)
                last_t = t
    return np.asarray(peaks, dtype=np.float32)


def _cache_path(bgm_path: str) -> Path:
    st = Path(bgm_path).stat()
    key = f"{Path(bgm_path).resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()
    h = hashlib.sha1(key).hexdigest()[:16]
    d = OUTPUTS_DIR / "_beatcache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.npy"


def detect_beats(bgm_path: str) -> np.ndarray:
    """
    BGM 파일 → 정렬된 비트 시각(초) 배열.
    실패/빈 BGM/디코드 불가 → 빈 배열 (호출자는 스냅 안 함).
    결과를 outputs/_beatcache 에 캐시(재렌더 회피).
    """
    if not bgm_path or not Path(bgm_path).exists():
        return np.empty(0)
    try:
        cache = _cache_path(bgm_path)
        if cache.exists():
            return np.load(cache)
        x = _decode_mono(bgm_path)
        beats = _detect_beats_from_pcm(x) if x.size else np.empty(0)
        try:
            np.save(cache, beats)
        except Exception:
            pass  # 캐시는 최적화 — 실패해도 결과 반환
        if beats.size:
            logger.info(f"Beat detect: {beats.size} beats from {Path(bgm_path).name} "
                        f"(first={beats[0]:.2f}s last={beats[-1]:.2f}s)")
        return beats
    except Exception as e:
        logger.warning(f"Beat detect failed (skipping): {e}")
        return np.empty(0)


def nearest_beat(beats: np.ndarray, t: float, tolerance: float) -> Optional[float]:
    """t 에 가장 가까운 비트 시각. tolerance 이내면 반환, 아니면 None."""
    if beats.size == 0:
        return None
    idx = int(np.argmin(np.abs(beats - t)))
    target = float(beats[idx])
    return target if abs(target - t) <= tolerance else None
