"""11/23 Ivan 피드백: 비네트로 화면이 어두워짐 → 평균 밝기 저하 2% 이내 + 노출 보정."""
import numpy as np
import pytest

from backend.services.color_grade import (
    STYLE_PRESETS, VIGNETTE_MAX_MEAN_DROP, _build_processor, _build_vignette,
)

PRESETS = [k for k, v in STYLE_PRESETS.items() if v]


def _y(a):
    a = a.astype(np.float32)
    return float((0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]).mean())


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (720, 1280)])
@pytest.mark.parametrize("strength", [0.05, 0.12, 0.55, 1.0])
def test_vignette_mean_drop_capped(size, strength):
    v = _build_vignette(size, strength)
    assert 1.0 - float(v.mean()) <= VIGNETTE_MAX_MEAN_DROP + 1e-4
    h, w = v.shape
    assert v[h // 2, w // 2] == pytest.approx(1.0)   # 중심은 그대로


@pytest.mark.parametrize("preset", PRESETS)
def test_preset_vignette_within_2pct(preset):
    v = _build_vignette((1280, 720), STYLE_PRESETS[preset]["texture"].get("vignette", 0.0))
    assert 1.0 - float(v.mean()) <= 0.02 + 1e-4


@pytest.mark.parametrize("preset", PRESETS)
def test_grade_keeps_mean_brightness(preset):
    rng = np.random.default_rng(0)
    # 자연 사진 비슷한 중간 밝기 그라디언트 + 노이즈
    base = np.linspace(40, 200, 1280, dtype=np.float32)[None, :, None]
    img = np.clip(base + rng.normal(0, 25, (720, 1280, 3)), 0, 255).astype(np.uint8)
    out = _build_processor(preset, (1280, 720))(img)
    assert abs(_y(out) / _y(img) - 1.0) <= 0.03
