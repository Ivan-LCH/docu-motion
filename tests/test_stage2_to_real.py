"""자동 편집 2단계: 그림/만화 → 실사 전환 (마스크 리빌, 계획, 슬라이드 렌더, meta 병합)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.services import auto_edit, photo_fx
from tests.test_auto_edit import (_photo, _slides, db, make_project, no_network,  # noqa: F401
                                  outputs, session_factory)


@pytest.mark.parametrize("style", ["brush", "wipe", "dissolve"])
def test_reveal_mask_monotonic_and_covers(style):
    m = photo_fx.RevealMask(90, 160, style, seed=7)
    prev = None
    for p in np.linspace(0, 1, 11):
        a = np.asarray(m.at(p), dtype=np.float32)
        mean = float(a.mean())
        assert abs(mean - p) < 0.08                 # 드러난 면적 ≈ 진행률
        if prev is not None:
            assert np.all(a >= prev - 1e-6)          # 한 번 드러난 실사는 다시 그림으로 안 돌아감
        prev = a
    assert float(np.max(m.at(0.0))) == 0.0 and float(np.min(m.at(1.0))) == 1.0


def test_brush_and_wipe_are_spatial():
    brush = photo_fx.RevealMask(90, 160, "brush", seed=1).at(0.5)
    wipe = photo_fx.RevealMask(90, 160, "wipe", seed=1).at(0.5)
    # 좌측부터 실사로 (붓결도 좌→우 바이어스)
    assert wipe[:, :40].mean() > 0.9 and wipe[:, -40:].mean() < 0.1
    assert brush[:, :40].mean() > brush[:, -40:].mean() + 0.3
    # 붓결은 경계가 들쭉날쭉 — 열마다 드러난 비율이 제각각
    col = brush[:, 60:100].mean(axis=0)
    assert col.std() > 0.02 and 0.05 < float(brush.mean()) < 0.95
    assert photo_fx.normalize_to_real("BRUSH") == "brush" and photo_fx.normalize_to_real("x") == ""


def test_to_real_progress_hold_then_reveal():
    d = 3.0
    assert photo_fx.to_real_progress(0.0, d, 0.5) == 0.0
    assert photo_fx.to_real_progress(0.85, d, 0.5) == 0.0        # 앞 30% 는 그림 유지
    assert 0.0 < photo_fx.to_real_progress(1.5, d, 0.5) < 1.0
    assert photo_fx.to_real_progress(2.9, d, 0.5) == 1.0          # 끝은 실사
    assert photo_fx.to_real_progress(0.5, d, 0.0) == 0.0 and photo_fx.to_real_progress(1.0, d, 0.0) == 1.0


def test_blend_mask_endpoints():
    real = np.full((4, 6, 3), 200, np.uint8)
    sty = np.full((4, 6, 3), 20, np.uint8)
    assert photo_fx.blend_mask(real, sty, np.zeros((4, 6))) is sty
    assert photo_fx.blend_mask(real, sty, np.ones((4, 6))) is real
    half = photo_fx.blend_mask(real, sty, np.full((4, 6), 0.5))
    assert np.all(np.abs(half.astype(int) - 110) <= 1)


@pytest.mark.parametrize("ken_burns", [0, 40])
def test_slide_clip_starts_drawn_ends_real(tmp_path, ken_burns):
    from backend.services.renderer import build_slide_clip
    _photo(tmp_path / "a.jpg", 640, 400)
    (tmp_path / "temp").mkdir()
    item = {"image_filename": "a.jpg", "label": "", "text": "", "slide_type": "image",
            "use_tts": 0, "photo_effect": "sketch", "effect_reveal": 0.5, "ken_burns": ken_burns,
            "image_fit": "cover", "transition": "crossfade", "overlays": "[]", "rotation": 0,
            "text_style": "none", "meta": json.dumps({"effect_to_real": "brush"})}
    clip = build_slide_clip(item, 0, tmp_path, tmp_path / "temp", (320, 180),
                            default_slide_duration=3.0, style_preset="none")
    first, mid, last = clip.get_frame(0.1), clip.get_frame(1.5), clip.get_frame(2.95)

    def chroma(f):
        f = f.astype(int)
        return float(np.abs(f[..., 0] - f[..., 2]).mean())   # 스케치는 회색조 → 채도 ≈ 0

    assert chroma(first) < 2.0                  # 그림(스케치)으로 시작
    assert chroma(last) > 20.0                  # 실사로 끝
    assert chroma(first) < chroma(mid) < chroma(last)
    clip.close()


def test_plan_stylize_prefers_card_and_resets(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=12, place_at=1)
    s = auto_edit.plan_auto_edit(db, p.id, {"stylize_ratio": 0.25})
    st = s["stylized"]
    assert 2 <= len(st) <= 4
    idx = [x["order_index"] for x in st]
    assert all(b - a > 1 for a, b in zip(idx, idx[1:]))            # 인접 금지
    assert all(i >= len(s["intro_slides"]) for i in idx)            # 인트로 제외
    assert s["cards"][0]["order_index"] in idx                      # 위치 카드 컷 우선
    assert {x["effect"] for x in st} <= {"sketch", "cartoon"}
    slides = {sl.order_index: sl for sl in _slides(db, p.id)}
    for x in st:
        sl = slides[x["order_index"]]
        assert sl.photo_effect == x["effect"] and sl.effect_reveal == 0.5
        assert json.loads(sl.meta)["effect_to_real"] == x["to_real"]

    # 다시 돌리며 끄면 자동으로 넣은 효과만 되돌린다 (사용자가 직접 건 효과는 유지)
    manual = slides[max(slides)]
    manual.photo_effect = "cartoon"
    db.commit()
    s2 = auto_edit.plan_auto_edit(db, p.id, {"stylize_ratio": 0})
    assert s2["stylized"] == []
    for sl in _slides(db, p.id):
        m = json.loads(sl.meta or "{}")
        assert "effect_to_real" not in m and "auto_stylized" not in m
        if sl.order_index == manual.order_index:
            assert sl.photo_effect == "cartoon"
        else:
            assert sl.photo_effect in (None, "none")


def test_default_plan_has_no_stylize(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=6, place_at=None)
    s = auto_edit.plan_auto_edit(db, p.id, {})
    assert s["stylized"] == [] and s["stylize_ratio"] == 0.0


def test_meta_merge_effect_to_real():
    from backend.api.v1.slides import _merge_style_meta
    m = json.loads(_merge_style_meta(json.dumps({"frames": ["f.png"]}),
                                     json.dumps({"effect_to_real": "Wipe", "frames": ["x"]})))
    assert m == {"frames": ["f.png"], "effect_to_real": "wipe"}
    m2 = json.loads(_merge_style_meta(json.dumps(m), json.dumps({"effect_to_real": "evil"})))
    assert "effect_to_real" not in m2


def _sat(frame):
    return float(np.abs(frame[..., 0].astype(np.int16) - frame[..., 1].astype(np.int16)).mean())


def _uniq16(frame):
    q = (frame.astype(np.uint16) // 16 * 16).astype(np.uint8)
    return int(len(np.unique(q.reshape(-1, 3), axis=0)))


def test_cartoon_flatter_and_more_saturated():
    """강화된 만화: 사진보다 채도가 높고, 색면이 단순하며 외곽선이 있다."""
    from backend.services.photo_fx import stylize_photo
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:180, 0:280]
    src = np.stack([
        np.clip(80 + 0.4 * xx + 0.2 * yy, 0, 255),
        np.clip(120 + 0.15 * (180 - yy) + rng.normal(0, 18, xx.shape), 0, 255),
        np.clip(90 + 0.25 * yy + rng.normal(0, 18, xx.shape), 0, 255),
    ], axis=-1).astype(np.uint8)
    # 간단한 도형으로 외곽선이 생기게
    src[40:120, 60:140] = (200, 60, 50)
    src[70:160, 160:240] = (40, 90, 200)
    cartoon = stylize_photo(src, "cartoon")
    assert _sat(cartoon) > _sat(src) * 1.05
    assert _uniq16(cartoon) < _uniq16(src) * 0.7
    ink = float((cartoon.min(axis=2) < 40).mean())
    assert ink > 0.01


def test_cinematic_skips_styled_hold(tmp_path):
    """그림 홀드 구간은 cinematic 을 입혀도 스타일 픽셀이 거의 그대로다. 실사 구간은 그레이딩된다."""
    from backend.services.photo_fx import stylize_photo
    from backend.services.renderer import build_slide_clip
    from tests.test_photo_effects import _sample_photo
    from PIL import Image

    photo = _sample_photo(900, 600)
    Image.fromarray(photo).save(tmp_path / "photo.png")
    temp = tmp_path / "temp"
    temp.mkdir()
    canvas = (640, 360)
    item = {
        "image_filename": "photo.png",
        "label": "",
        "text": "",
        "slide_type": "image",
        "use_tts": 0,
        "photo_effect": "cartoon",
        "effect_reveal": 0.5,
        "ken_burns": 0,
        "image_fit": "cover",
        "transition": "none",
        "overlays": "[]",
        "rotation": 0,
        "text_style": "none",
        "meta": '{"effect_to_real": "dissolve"}',
    }
    none_clip = build_slide_clip(item, 0, tmp_path, temp, canvas, tts_engine=None,
                                 default_slide_duration=2.0, style_preset="none")
    cine_clip = build_slide_clip(item, 0, tmp_path, temp, canvas, tts_engine=None,
                                 default_slide_duration=2.0, style_preset="cinematic")
    # hold = 0.3 * 2s = 0.6s
    hold_none = none_clip.get_frame(0.2)
    hold_cine = cine_clip.get_frame(0.2)
    real_none = none_clip.get_frame(1.85)
    real_cine = cine_clip.get_frame(1.85)
    # 그림 구간: 그레이딩 생략 → 두 프리셋이 거의 같음
    hold_mae = float(np.abs(hold_none.astype(np.int16) - hold_cine.astype(np.int16)).mean())
    assert hold_mae < 3.0
    # 실사 구간: cinematic 이 적용되어 차이가 난다
    real_mae = float(np.abs(real_none.astype(np.int16) - real_cine.astype(np.int16)).mean())
    assert real_mae > 8.0
    # 홀드 프레임은 실사와 분명히 다르고, 채도는 유지된다
    assert float(np.abs(hold_cine.astype(np.int16) - real_cine.astype(np.int16)).mean()) > 8.0
    assert _sat(hold_cine) > 8
    none_clip.close()
    cine_clip.close()
