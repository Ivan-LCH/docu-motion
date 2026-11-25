"""사진 효과(sketch / sketch_soft / cartoon / watercolor): 배열 규약, 스키마, 짧은 샘플 영상."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from backend.schema.project import SlideCreate, SlideUpdate
from backend.services.photo_fx import (
    _cartoon_pil,
    _sketch_soft,
    _watercolor,
    _watercolor_pil,
    blend_rgb,
    reveal_weight,
    stylize_photo,
)
from backend.services.presets import (
    DEFAULT_EFFECT_REVEAL,
    DEFAULT_SKETCH_SOFTNESS,
    VALID_PHOTO_EFFECTS,
    clamp_effect_reveal,
    normalize_photo_effect,
    parse_sketch_softness,
)


def _sample_photo(w: int = 160, h: int = 120) -> np.ndarray:
    """색 영역이 분명한 합성 사진. 검정은 거의 없어서 만화 외곽선이 구분된다."""
    img = Image.new("RGB", (w, h), (170, 205, 230))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        c = int(120 + 80 * y / max(h - 1, 1))
        draw.line([(0, y), (w, y)], fill=(c, min(255, c + 40), 235))
    draw.rectangle([0, int(h * 0.62), w, h], fill=(64, 150, 72))
    draw.ellipse([int(w * 0.62), int(h * 0.08), int(w * 0.90), int(h * 0.42)], fill=(240, 196, 48))
    draw.rectangle([int(w * 0.12), int(h * 0.38), int(w * 0.42), int(h * 0.78)], fill=(186, 54, 48))
    draw.rectangle([int(w * 0.20), int(h * 0.50), int(w * 0.32), int(h * 0.66)], fill=(236, 236, 230))
    return np.asarray(img)


def _colorfulness(frame: np.ndarray) -> float:
    r = frame[..., 0].astype(np.int16)
    g = frame[..., 1].astype(np.int16)
    return float(np.abs(r - g).mean())


def test_photo_effect_ids_registered():
    assert "sketch" in VALID_PHOTO_EFFECTS
    assert "cartoon" in VALID_PHOTO_EFFECTS
    assert "sketch_soft" in VALID_PHOTO_EFFECTS
    assert "watercolor" in VALID_PHOTO_EFFECTS
    assert normalize_photo_effect("SKETCH") == "sketch"
    assert normalize_photo_effect(" Sketch_Soft ") == "sketch_soft"
    assert normalize_photo_effect("WATERCOLOR") == "watercolor"
    assert normalize_photo_effect("nope") == "none"
    assert normalize_photo_effect("oilpaint") == "none"
    assert normalize_photo_effect(None) == "none"
    assert clamp_effect_reveal(None) == DEFAULT_EFFECT_REVEAL
    assert clamp_effect_reveal(1.4) == 1.0
    assert clamp_effect_reveal(-0.2) == 0.0


def test_stylize_shape_and_dtype():
    src = _sample_photo()
    for effect in ("sketch", "cartoon"):
        out = stylize_photo(src, effect)
        assert out.shape == src.shape
        assert out.dtype == np.uint8
        assert out.nbytes == src.nbytes
        assert int(np.abs(out.astype(np.int16) - src.astype(np.int16)).mean()) > 8

    sketch = stylize_photo(src, "sketch")
    # 연필 스케치는 채널이 같다
    assert int(np.abs(sketch[..., 0].astype(np.int16) - sketch[..., 1].astype(np.int16)).max()) == 0
    assert int(np.abs(sketch[..., 1].astype(np.int16) - sketch[..., 2].astype(np.int16)).max()) == 0
    assert _colorfulness(sketch) < 1.0
    assert float(sketch.mean()) > 100  # 종이색에 가깝다

    cartoon = stylize_photo(src, "cartoon")
    assert _colorfulness(cartoon) > 8
    dark = float((cartoon.min(axis=2) < 24).mean())
    assert dark > 0.005  # 외곽선

    same = stylize_photo(src, "oilpaint")
    assert same.shape == src.shape and same.dtype == np.uint8
    assert np.array_equal(same, src)

    pil_cartoon = _cartoon_pil(src)
    assert pil_cartoon.shape == src.shape and pil_cartoon.dtype == np.uint8


def _busy_photo(w: int = 320, h: int = 220) -> np.ndarray:
    """잔 텍스처 + 도형이 많은 사진 (스케치 잔차가 많이 생긴다)."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([
        110 + 60 * np.sin(xx / 9.0) + rng.normal(0, 22, (h, w)),
        120 + 50 * np.cos(yy / 7.0) + rng.normal(0, 22, (h, w)),
        100 + 40 * np.sin((xx + yy) / 11.0) + rng.normal(0, 22, (h, w)),
    ], axis=-1)
    img = np.clip(base, 0, 255).astype(np.uint8)
    im = Image.fromarray(img)
    draw = ImageDraw.Draw(im)
    for k in range(14):
        x0, y0 = int(rng.integers(0, w - 40)), int(rng.integers(0, h - 40))
        draw.rectangle([x0, y0, x0 + 34, y0 + 28], outline=(20, 20, 20), width=2,
                       fill=tuple(int(c) for c in rng.integers(30, 240, 3)))
    return np.asarray(im)


def _legacy_sketch(rgb: np.ndarray) -> np.ndarray:
    """변경 전 sketch 파이프라인 (잔차 ×3). sketch 의 결과가 그대로인지 비교용."""
    import cv2
    sigma = max(1.2, min(rgb.shape[0], rgb.shape[1]) * 0.012)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(cv2.bitwise_not(gray), (0, 0), sigmaX=sigma, sigmaY=sigma)
    denom = np.maximum(256 - blur.astype(np.int32), 1)
    dodge = np.minimum(gray.astype(np.int32) * 256 // denom, 255)
    residual = np.minimum((255 - dodge) * 3, 255)
    out = (255 - residual).astype(np.uint8)
    return np.stack([out, out, out], axis=-1)


def _luma(frame: np.ndarray) -> float:
    f = frame.astype(np.float32)
    return float((0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]).mean())


def test_parse_sketch_softness():
    assert parse_sketch_softness(None) is None
    assert parse_sketch_softness("") is None
    assert parse_sketch_softness("abc") is None
    assert parse_sketch_softness(float("nan")) is None
    assert parse_sketch_softness(True) is None
    assert parse_sketch_softness(0) == 0.0
    assert parse_sketch_softness("0.4") == pytest.approx(0.4)
    assert parse_sketch_softness(1.7) == 1.0
    assert parse_sketch_softness(-0.3) == 0.0
    assert DEFAULT_SKETCH_SOFTNESS == pytest.approx(0.75)


def test_sketch_unchanged_without_softness():
    """sketch 는 softness 없음/0 이면 기존 ×3 잔차 스케치와 비트 단위로 같다."""
    for src in (_sample_photo(), _busy_photo()):
        legacy = _legacy_sketch(src)
        assert np.array_equal(stylize_photo(src, "sketch"), legacy)
        assert np.array_equal(stylize_photo(src, "sketch", softness=None), legacy)
        assert np.array_equal(stylize_photo(src, "sketch", softness=0), legacy)
        assert np.array_equal(stylize_photo(src, "sketch", softness="bad"), legacy)
        # 기존 cartoon 도 softness 와 무관
        assert np.array_equal(stylize_photo(src, "cartoon", softness=0.9), stylize_photo(src, "cartoon"))


def test_sketch_soft_lighter_than_sketch():
    src = _busy_photo()
    hard = stylize_photo(src, "sketch")
    soft = stylize_photo(src, "sketch_soft")
    assert soft.shape == src.shape and soft.dtype == np.uint8
    # 기본 softness 0.75 와 같다 / sketch + softness>0 은 같은 부드러운 파이프라인
    assert np.array_equal(soft, _sketch_soft(src, 0.75))
    assert np.array_equal(stylize_photo(src, "sketch", softness=0.75), soft)
    # 종이가 밝고 진한 선이 적다
    assert _luma(soft) > _luma(hard) + 10
    assert float((soft.min(axis=2) < 80).mean()) < float((hard.min(axis=2) < 80).mean()) * 0.5
    assert float(soft.mean()) > 170   # 종이가 회색으로 가라앉지 않는다 (휘도 맞춤 없음)
    # 따뜻한 종이색: R ≥ G ≥ B
    assert float(soft[..., 0].mean()) >= float(soft[..., 1].mean()) >= float(soft[..., 2].mean())
    assert float(soft[..., 0].mean()) - float(soft[..., 2].mean()) > 2
    # softness 가 높을수록 밝다
    assert _luma(stylize_photo(src, "sketch_soft", softness=1.0)) > \
        _luma(stylize_photo(src, "sketch_soft", softness=0.3))


def test_watercolor_color_soft_ink_and_luma():
    for src in (_sample_photo(320, 220), _busy_photo()):
        water = stylize_photo(src, "watercolor")
        cartoon = stylize_photo(src, "cartoon")
        assert water.shape == src.shape and water.dtype == np.uint8
        assert _colorfulness(water) > 8                       # 컬러 (회색조 아님)
        assert int(np.abs(water.astype(np.int16) - src.astype(np.int16)).mean()) > 4
        # 만화보다 옅은 잉크
        assert float((water.min(axis=2) < 24).mean()) < float((cartoon.min(axis=2) < 24).mean())
        assert float((water.min(axis=2) < 40).mean()) <= float((cartoon.min(axis=2) < 40).mean())
        # _match_luma: 평균 휘도가 원본에 가깝다
        assert abs(_luma(water) - _luma(src)) < _luma(src) * 0.04
    # 긴 변 1280 초과 입력도 원 해상도로 돌아온다
    big = _sample_photo(1600, 900)
    assert _watercolor(big).shape == big.shape


def test_new_effects_pil_fallback(monkeypatch):
    """cv2 를 못 읽어도 렌더가 죽지 않는다."""
    import sys
    src = _busy_photo(160, 120)
    pil_water = _watercolor_pil(src)
    assert pil_water.shape == src.shape and pil_water.dtype == np.uint8
    assert _colorfulness(pil_water) > 8
    assert abs(_luma(pil_water) - _luma(src)) < _luma(src) * 0.05
    monkeypatch.setitem(sys.modules, "cv2", None)
    soft = stylize_photo(src, "sketch_soft")
    water = stylize_photo(src, "watercolor")
    assert soft.shape == src.shape and water.shape == src.shape
    assert float(soft.mean()) > 150


def test_reveal_weight_and_blend():
    assert reveal_weight(0.0, 2.0, 0.5) == 0.0
    assert reveal_weight(1.0, 2.0, 0.5) == 1.0  # 창 길이 = 1s
    assert reveal_weight(0.5, 2.0, 0.5) == pytest.approx(0.5)  # smoothstep(0.5)
    assert reveal_weight(0.0, 2.0, 0.0) == 1.0

    a = np.zeros((4, 4, 3), np.uint8)
    b = np.full((4, 4, 3), 200, np.uint8)
    mid = blend_rgb(a, b, 0.5)
    assert mid.dtype == np.uint8 and mid.shape == a.shape
    assert 90 <= int(mid[0, 0, 0]) <= 110
    assert np.array_equal(blend_rgb(a, b, 0.0), a)
    assert np.array_equal(blend_rgb(a, b, 1.0), b)


def test_schema_accepts_effect_ids_and_reveal():
    for effect in ("sketch", "cartoon", "SKETCH", "sketch_soft", "Watercolor"):
        created = SlideCreate(photo_effect=effect)
        assert created.photo_effect == effect.lower()
        updated = SlideUpdate(id="s1", order_index=1, photo_effect=effect, effect_reveal=0.25)
        assert updated.photo_effect == effect.lower()
        assert updated.effect_reveal == pytest.approx(0.25)

    fresh = SlideCreate()
    assert fresh.photo_effect == "none"
    assert fresh.effect_reveal == pytest.approx(DEFAULT_EFFECT_REVEAL)

    empty = SlideUpdate(id="s", order_index=0, photo_effect=None, effect_reveal=None)
    assert empty.photo_effect == "none"
    assert empty.effect_reveal == pytest.approx(DEFAULT_EFFECT_REVEAL)

    high = SlideUpdate(id="s", order_index=0, effect_reveal=1.7)
    low = SlideUpdate(id="s", order_index=0, effect_reveal=-0.4)
    assert high.effect_reveal == 1.0
    assert low.effect_reveal == 0.0

    with pytest.raises(ValidationError):
        SlideCreate(photo_effect="oilpaint")
    with pytest.raises(ValidationError):
        SlideUpdate(id="s", order_index=0, effect_reveal="fast")


def _slide_item(filename: str, effect: str, ken_burns: int) -> dict:
    return {
        "image_filename": filename,
        "label": "",
        "text": "",
        "slide_type": "image",
        "use_tts": 0,
        "photo_effect": effect,
        "effect_reveal": 0.45,
        "ken_burns": ken_burns,
        "image_fit": "cover",
        "transition": "crossfade",
        "overlays": "[]",
        "rotation": 0,
        "text_style": "none",
        "meta": "{}",
    }


def test_effects_render_in_short_sample_video(tmp_path: Path):
    """스케치·만화를 Ken Burns + 크로스페이드와 함께 짧은 mp4 로 인코딩한다."""
    from backend.services.renderer import build_slide_clip, compose_timeline

    photo = _sample_photo(900, 600)
    Image.fromarray(photo).save(tmp_path / "photo.png")
    assets = tmp_path
    temp = tmp_path / "temp"
    temp.mkdir()
    canvas = (640, 360)

    sketch_clip = build_slide_clip(
        _slide_item("photo.png", "sketch", ken_burns=50),
        0, assets, temp, canvas, tts_engine=None,
        default_slide_duration=1.6, style_preset="none",
    )
    cartoon_clip = build_slide_clip(
        _slide_item("photo.png", "cartoon", ken_burns=40),
        1, assets, temp, canvas, tts_engine=None,
        default_slide_duration=1.6, style_preset="none",
    )
    assert sketch_clip is not None and cartoon_clip is not None

    sketch_start = sketch_clip.get_frame(0.0)
    sketch_end = sketch_clip.get_frame(max(0.0, sketch_clip.duration - 0.05))
    cartoon_start = cartoon_clip.get_frame(0.0)
    cartoon_end = cartoon_clip.get_frame(max(0.0, cartoon_clip.duration - 0.05))

    for frame in (sketch_start, sketch_end, cartoon_start, cartoon_end):
        assert frame.dtype == np.uint8
        assert frame.shape == (canvas[1], canvas[0], 3)

    # 드러내기: 시작은 컬러 사진, 끝은 스케치(무채색)
    assert _colorfulness(sketch_start) > 15
    assert _colorfulness(sketch_end) < 2
    assert float(np.abs(sketch_start.astype(np.int16) - sketch_end.astype(np.int16)).mean()) > 12

    # 만화 끝 프레임은 색이 남고, 시작보다 검정 외곽선이 많다
    assert _colorfulness(cartoon_end) > 8
    start_dark = float((cartoon_start.min(axis=2) < 24).mean())
    end_dark = float((cartoon_end.min(axis=2) < 24).mean())
    assert end_dark > start_dark + 0.004

    combined, _starts, _overlaps = compose_timeline(
        [sketch_clip, cartoon_clip],
        ["none", "crossfade"],
        transition_duration=0.35,
        canvas_size=canvas,
    )
    out = tmp_path / "sample.mp4"
    combined.write_videofile(
        str(out),
        fps=12,
        codec="libx264",
        audio=False,
        logger=None,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-preset", "ultrafast"],
    )
    assert out.exists() and out.stat().st_size > 1000
    assert combined.duration > 2.0

    # 인코딩된 파일을 다시 읽어 프레임이 나오는지 확인
    from moviepy.editor import VideoFileClip
    encoded = VideoFileClip(str(out))
    try:
        mid = encoded.get_frame(min(1.2, encoded.duration - 0.05))
        assert mid.shape[0] > 0 and mid.dtype == np.uint8
    finally:
        encoded.close()

    artifact_dir = os.environ.get("PHOTO_FX_ARTIFACTS", "")
    if artifact_dir:
        dest = Path(artifact_dir)
        dest.mkdir(parents=True, exist_ok=True)
        Image.fromarray(sketch_start).save(dest / "sketch-before.png")
        Image.fromarray(sketch_end).save(dest / "sketch-after.png")
        Image.fromarray(cartoon_start).save(dest / "cartoon-before.png")
        Image.fromarray(cartoon_end).save(dest / "cartoon-after.png")
        # 샘플 영상은 아티팩트로만 복사 (저장소에는 넣지 않음)
        (dest / "sample.mp4").write_bytes(out.read_bytes())

    sketch_clip.close()
    cartoon_clip.close()
    combined.close()


@pytest.mark.parametrize("effect,meta", [
    ("sketch_soft", "{}"),
    ("sketch", '{"sketch_softness": 0.6}'),
    ("watercolor", '{"effect_to_real": "brush"}'),
])
def test_new_effects_build_slide_clip(tmp_path: Path, effect: str, meta: str):
    from backend.services.renderer import build_slide_clip

    photo = _sample_photo(900, 600)
    Image.fromarray(photo).save(tmp_path / "photo.png")
    temp = tmp_path / "temp"
    temp.mkdir()
    canvas = (640, 360)
    item = _slide_item("photo.png", effect, ken_burns=40)
    item["meta"] = meta
    clip = build_slide_clip(item, 0, tmp_path, temp, canvas, tts_engine=None,
                            default_slide_duration=1.6, style_preset="cinematic")
    assert clip is not None
    frames = [clip.get_frame(t) for t in (0.0, 0.5, max(0.0, clip.duration - 0.05))]
    for f in frames:
        assert f.dtype == np.uint8 and f.shape == (canvas[1], canvas[0], 3)
    start, end = frames[0], frames[-1]
    assert float(np.abs(start.astype(np.int16) - end.astype(np.int16)).mean()) > 6
    if effect == "watercolor":      # 그림 → 실사: 수채로 시작 (컬러 유지)
        assert _colorfulness(start) > 8
    elif meta == "{}":              # 실사 → 부드러운 스케치: 밝은 종이로 끝
        assert _colorfulness(start) > 15 and float(end.mean()) > 150
    clip.close()
