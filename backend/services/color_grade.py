"""
DocuMotion - 시네마틱 색보정/질감 후반처리 (Auto-Style Phase A, Tier 1)

스타일 프리셋(cinematic / vlog / documentary / trending)을 고르면 렌더 시
색보정(contrast·saturation·temperature·split-toning) + 비네트 + 필름 그레인 +
라이트 릭 을 한 번의 numpy 패스로 적용한다.

설계:
  - 외부 .cube LUT 파일(라이선스 이슈) 없이 numpy로 절차적 색보정.
  - 커스텀 블렌드 모드(multiply 비네트 / screen 그레인·릭)가 필요 → CompositeVideoClip
    (alpha 합성만) 대신 clip.fl_image 단일 numpy 패스로 grade→vignette→grain→leak 처리.
  - per-channel 256엔트리 LUT 사전계산 → 프레임당 fancy indexing O(1).
    (주의: 1D per-channel LUT는 진짜 3D LUT의 근사 — cross-channel 매핑 불가.
     Tier-1 색감엔 충분. Phase B에서 필요하면 3D LUT 도입 검토.)

★ 핵심 제약: moviepy 1.0.3 의 fl_image 는 clip.audio 를 버린다
  (build_slide_clip 의 L397/L480/L484 패턴이 이를 증명 — fl_image 전에 오디오 캡처,
   후에 재부착). 따라서 apply_cinematic 은 내부에서 오디오를 캡처/재부착해야 한다.
  그렇지 않으면 BGM 믹싱 직전 combined.audio is None 이 되어 TTS 음성이 사라진다.

프리셋 스키마는 forward-compatible: DB엔 preset name 문자열만 저장하고 파라미터는
이 모듈의 Python dict 에 둔다. rhythm/motion/structure 키는 Phase B/C/D 확장용
None 플레이스홀더 → DB 마이그레이션 없이 값만 채우면 확장 가능.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

import numpy as np

from backend.core.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# 스타일 프리셋 (Tier-1: color/texture 만 값 채움)
# ──────────────────────────────────────────────────────────────────────────
# color.contrast     : 1.0=변화없음, >1 대비 강화 (pivot 128)
# color.saturation   : 1.0=변화없음, >1 채도 증가
# color.temperature  : +warm(R↑/B↓), -cool
# color.shadows_tint / highlights_tint : (R,G,B) delta 0~1, luminance-masked split-toning
# texture.vignette   : 0~1 강도, corners 어둡게 (multiply)
# texture.grain      : 0~1 필름 그레인 양 (additive)
# texture.light_leak : {type:"none"|"warm_radial", intensity, pos}
STYLE_PRESETS: dict = {
    "none": None,  # passthrough — apply_cinematic 이 clip 을 그대로 반환

    "cinematic": {
        "color": {
            "contrast": 1.15,
            "saturation": 0.92,
            "temperature": 0.06,
            "shadows_tint": (0.0, 0.10, 0.12),    # 틸(teal) 섀도우 리프트
            "highlights_tint": (0.10, 0.06, 0.0),  # 오렌지 하이라이트
        },
        "texture": {
            "vignette": 0.55,
            "grain": 0.12,
            "light_leak": {"type": "warm_radial", "intensity": 0.22, "pos": "top_left"},
        },
        "rhythm": None,    # Phase B: beat_sync, cut_pacing
        "motion": None,    # Phase C: motion_intensity, parallax
        "structure": None,  # Phase D: intro_style, outro_style
    },

    "vlog": {
        "color": {
            "contrast": 1.05,
            "saturation": 1.18,
            "temperature": 0.03,
            "shadows_tint": (0.0, 0.0, 0.0),
            "highlights_tint": (0.03, 0.02, 0.0),
        },
        "texture": {
            "vignette": 0.18,
            "grain": 0.05,
            "light_leak": {"type": "none", "intensity": 0.0},
        },
        "rhythm": None, "motion": None, "structure": None,
    },

    "documentary": {
        "color": {
            "contrast": 1.08,
            "saturation": 0.85,
            "temperature": 0.0,
            "shadows_tint": (0.0, 0.0, 0.0),
            "highlights_tint": (0.0, 0.0, 0.0),
        },
        "texture": {
            "vignette": 0.25,
            "grain": 0.08,
            "light_leak": {"type": "none", "intensity": 0.0},
        },
        "rhythm": None, "motion": None, "structure": None,
    },

    "trending": {
        "color": {
            "contrast": 1.10,
            "saturation": 1.05,
            "temperature": 0.12,
            "shadows_tint": (0.05, 0.03, 0.0),
            "highlights_tint": (0.12, 0.06, 0.0),
        },
        "texture": {
            "vignette": 0.40,
            "grain": 0.10,
            "light_leak": {"type": "warm_radial", "intensity": 0.35, "pos": "bottom_right"},
        },
        "rhythm": None, "motion": None, "structure": None,
    },
}

VALID_STYLE_PRESETS = list(STYLE_PRESETS.keys())


# ──────────────────────────────────────────────────────────────────────────
# 사전계산 빌더들
# ──────────────────────────────────────────────────────────────────────────
def _build_luts(color_cfg: dict) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """per-channel 256엔트리 uint8 LUT — contrast(pivot 128) + temperature."""
    x = np.arange(256, dtype=np.float32)
    contrast = float(color_cfg.get("contrast", 1.0))
    temp = float(color_cfg.get("temperature", 0.0))

    # contrast: pivot 128 기준 S 없는 선형 대비
    y = ((x / 128.0 - 1.0) * contrast + 1.0) * 128.0

    # temperature: +temp → R↑ / B↓ (warm), -temp → cool
    r = y + temp * 128.0
    g = y
    b = y - temp * 128.0

    return (
        np.clip(r, 0, 255).astype(np.uint8),
        np.clip(g, 0, 255).astype(np.uint8),
        np.clip(b, 0, 255).astype(np.uint8),
    )


def _build_vignette(size: Tuple[int, int], strength: float) -> np.ndarray:
    """radial 비네트 가중치 (1=변화없음, 코너로 갈수록 감소). multiply 용."""
    w, h = size
    ys = (np.arange(h, dtype=np.float32) - (h - 1) / 2.0) / ((h - 1) / 2.0)
    xs = (np.arange(w, dtype=np.float32) - (w - 1) / 2.0) / ((w - 1) / 2.0)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    dist = np.sqrt(xx * xx + yy * yy)            # 0(중심) ~ ~1.41(코너)
    weight = 1.0 - strength * (dist ** 2.2)
    return np.clip(weight, 0.15, 1.0).astype(np.float32)   # 코너 완전 흑 방지


def _build_light_leak(size: Tuple[int, int], leak_cfg: dict) -> Optional[np.ndarray]:
    """warm 가우시안 orb 라이트 릭. RGB [0,1] additive/screen 용. type none → None."""
    if not leak_cfg or leak_cfg.get("type", "none") == "none":
        return None
    w, h = size
    intensity = float(leak_cfg.get("intensity", 0.2))
    pos = leak_cfg.get("pos", "top_left")
    # orb 중심 (정규화 좌표 → 픽셀)
    pos_map = {
        "top_left":     (0.2, 0.2),
        "top_right":    (0.8, 0.2),
        "bottom_left":  (0.2, 0.8),
        "bottom_right": (0.8, 0.8),
        "center":       (0.5, 0.5),
    }
    fx, fy = pos_map.get(pos, (0.2, 0.2))
    cx, cy = fx * w, fy * h
    sigma = max(w, h) * 0.32

    ys = np.arange(h, dtype=np.float32)
    xs = np.arange(w, dtype=np.float32)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    g = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * sigma * sigma))

    # warm 색 (R>G>B) × falloff × intensity
    warm = np.stack([
        g * 1.0,
        g * 0.62,
        g * 0.28,
    ], axis=-1).astype(np.float32) * intensity
    return np.clip(warm, 0.0, 1.0)


# ──────────────────────────────────────────────────────────────────────────
# 프레임 처리 클로저
# ──────────────────────────────────────────────────────────────────────────
def _build_processor(preset_name: str, canvas_size: Tuple[int, int]) -> Callable[[np.ndarray], np.ndarray]:
    """
    LUT/vignette/leak/grain rng 를 사전계산하고 프레임당 처리하는 클로저 반환.
    한 번의 numpy 패스: grade(LUT+sat+split-tone) → vignette(multiply) → grain(additive) → leak(screen)
    """
    cfg = STYLE_PRESETS[preset_name]
    color_cfg = cfg["color"]
    texture_cfg = cfg["texture"]

    lut_r, lut_g, lut_b = _build_luts(color_cfg)
    sat = float(color_cfg.get("saturation", 1.0))
    shadows_tint = np.array(color_cfg.get("shadows_tint", (0, 0, 0)), dtype=np.float32)
    highlights_tint = np.array(color_cfg.get("highlights_tint", (0, 0, 0)), dtype=np.float32)

    vignette_strength = float(texture_cfg.get("vignette", 0.0))
    vignette_map = _build_vignette(canvas_size, vignette_strength) if vignette_strength > 0 else None

    grain_amt = float(texture_cfg.get("grain", 0.0))
    grain_rng = np.random.default_rng(1234) if grain_amt > 0 else None

    leak = _build_light_leak(canvas_size, texture_cfg.get("light_leak"))

    def process(frame: np.ndarray) -> np.ndarray:
        # frame: uint8 (H,W,3) RGB
        # 1) per-channel LUT (uint8 fast path)
        out = np.empty_like(frame)
        out[..., 0] = lut_r[frame[..., 0]]
        out[..., 1] = lut_g[frame[..., 1]]
        out[..., 2] = lut_b[frame[..., 2]]

        # 이후 float32
        f = out.astype(np.float32)

        # 2) saturation (luminance 보존)
        if sat != 1.0:
            gray = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
            f = gray[..., None] + (f - gray[..., None]) * sat

        # 3) split-toning (luminance-masked)
        if shadows_tint.any() or highlights_tint.any():
            gray = (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]) / 255.0
            shadow_w = np.clip(1.0 - gray * 2.0, 0.0, 1.0)[..., None]
            high_w = np.clip((gray - 0.5) * 2.0, 0.0, 1.0)[..., None]
            f += shadows_tint * 255.0 * shadow_w
            f += highlights_tint * 255.0 * high_w

        # 4) vignette (multiply)
        if vignette_map is not None:
            f *= vignette_map[..., None]

        # 5) grain (additive, broadcast across channels)
        if grain_rng is not None:
            noise = grain_rng.standard_normal(f.shape[:2]).astype(np.float32)
            f += noise[..., None] * (grain_amt * 35.0)

        # 6) light leak (screen blend: 255 - (255-a)(255-b)/255)
        if leak is not None:
            f = 255.0 - (255.0 - f) * (255.0 - leak * 255.0) / 255.0

        return np.clip(f, 0, 255).astype(np.uint8)

    return process


# ──────────────────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────────────────
def apply_cinematic(clip, preset_name: Optional[str], canvas_size: Tuple[int, int]):
    """
    clip 에 스타일 프리셋 후반처리 적용. fl_image 사용.
    ★ fl_image 가 clip.audio 를 버리므로 오디오를 캡처/재부착한다.
    실패 시 원본 clip 반환 (후반처리가 렌더를 망치면 안 됨).
    """
    if not preset_name or preset_name == "none" or preset_name not in STYLE_PRESETS:
        return clip
    try:
        original_audio = clip.audio                       # fl_image 전 캡처 (핵심)
        processor = _build_processor(preset_name, canvas_size)
        graded = clip.fl_image(processor)                 # 여기서 오디오 drop
        if original_audio is not None:
            graded = graded.set_audio(original_audio)
        return graded
    except Exception as e:
        logger.warning(f"Style preset '{preset_name}' failed (skipping): {e}")
        return clip
