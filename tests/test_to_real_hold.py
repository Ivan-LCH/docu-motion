"""그림→실사 홀드: 실사가 다 드러난 뒤 real_hold + 다음 전환 overlap 이 슬라이드 안에 들어가게 (presets 공용 헬퍼)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.services import photo_fx, presets
from backend.services.presets import (apply_to_real_hold, plan_timeline, to_real_hold_seconds,
                                      to_real_min_duration, to_real_reveal_end)
from tests.test_auto_edit import _photo  # noqa: F401


def _item(effect="watercolor", to_real="brush", reveal=0.5, transition="crossfade", **meta):
    m = {"effect_to_real": to_real, **meta} if to_real else dict(meta)
    return {"image_filename": "a.jpg", "slide_type": "image", "text": "", "photo_effect": effect,
            "effect_reveal": reveal, "transition": transition, "meta": json.dumps(m)}


def _plain(transition="crossfade"):
    return {"image_filename": "b.jpg", "slide_type": "image", "text": "", "photo_effect": "none",
            "transition": transition, "meta": "{}"}


@pytest.mark.parametrize("reveal", [0.0, 0.2, 0.5, 0.7, 0.9, 1.0])
def test_reveal_end_matches_photo_fx(reveal):
    d = 3.5
    end = to_real_reveal_end(d, reveal)
    assert photo_fx.to_real_progress(end + 1e-6, d, reveal) == 1.0
    if reveal > 0:
        assert photo_fx.to_real_progress(end - 0.02, d, reveal) < 1.0
    assert end <= d


@pytest.mark.parametrize("nxt,need", [
    ("crossfade", 2.8 + 1.5 + 0.7),     # 프로젝트 전환 길이 0.7
    ("whip_left", 2.8 + 1.5 + 0.35),    # 빠른 휩 0.35
    ("fade_black", 2.8 + 1.5),          # overlap 없는 전환
    (None, 2.8 + 1.5),                  # 마지막 슬라이드
])
def test_min_duration_by_next_transition(nxt, need):
    d = to_real_min_duration(3.5, 0.5, 1.5, nxt, None if nxt is None else 3.5, 0.7)
    assert d == pytest.approx(need, abs=1e-6)


def test_min_duration_overlap_capped_by_short_next_slide():
    # 다음 슬라이드 1.0s → overlap = min(0.7, 0.45 × 1.0) = 0.45
    assert to_real_min_duration(3.5, 0.5, 1.5, "crossfade", 1.0, 0.7) == pytest.approx(4.75)


def test_long_slide_not_extended():
    # reveal 0.3 → 드러나기 끝 4.8s + 1.5 + 0.7 = 7.0 ≤ 8.0
    assert to_real_min_duration(8.0, 0.3, 1.5, "crossfade", 3.5, 0.7) == 8.0


def test_apply_only_extends_that_slide_and_keeps_reveal_base():
    items = [_plain("none"), _item(transition="crossfade"), _plain("whip_left"), _plain("crossfade")]
    tr = [it["transition"] for it in items]
    durs, bases = apply_to_real_hold([3.5] * 4, items, tr, 0.7)
    assert durs[0] == durs[2] == durs[3] == 3.5
    assert durs[1] == pytest.approx(2.8 + 1.5 + 0.35)       # 다음이 whip
    assert bases == [None, 3.5, None, None]
    # 결과 타임라인에서 다음 슬라이드 시작 ≥ 드러나기 끝 + 홀드
    starts, _ = presets.compute_slide_starts(durs, tr, 0.7)
    reveal_end_abs = starts[1] + to_real_reveal_end(3.5, 0.5)
    assert starts[2] >= reveal_end_abs + 1.5 - 1e-6


def test_hold_opt_outs():
    assert to_real_hold_seconds(_item(to_real_hold=0)) == 0.0
    assert to_real_hold_seconds(_item(to_real_hold=2.5)) == 2.5
    assert to_real_hold_seconds(_item(to_real="")) == 0.0            # 실사→그림(기존) 은 대상 아님
    assert to_real_hold_seconds(_item(effect="none")) == 0.0
    assert to_real_hold_seconds({**_item(), "slide_type": "video"}) == 0.0
    assert to_real_hold_seconds(_item(), hold_off=True) == 0.0
    # 프로젝트 전체 끄기: slides[0].meta.to_real_hold_off
    first = {**_plain(), "meta": json.dumps({"to_real_hold_off": True})}
    items = [first, _item()]
    durs, bases = apply_to_real_hold([3.5, 3.5], items, ["none", "none"], 0.7)
    assert durs == [3.5, 3.5] and bases == [None, None]


def test_next_slide_hold_is_applied_first():
    # 뒤에서부터 계산: 다음 슬라이드가 먼저 늘어나면 overlap 상한(0.45 × min)이 커질 수 있다
    items = [_item(transition="none"), _item(transition="crossfade", reveal=0.7)]
    durs, _ = apply_to_real_hold([3.5, 1.2], items, ["none", "crossfade"], 2.0)
    starts, ov = presets.compute_slide_starts(durs, ["none", "crossfade"], 2.0)
    assert starts[1] >= to_real_reveal_end(3.5, 0.5) + 1.5 - 1e-6
    assert durs[1] > 1.2


def test_plan_timeline_uses_easy_duration_and_hold():
    a = {**_plain("none"), "meta": json.dumps({"easy_duration": 4.2})}
    b = _item(transition="crossfade")
    tl = plan_timeline([a, b, _plain("crossfade")], 3.5, 0.7)
    assert tl["durations"][0] == 4.2
    assert tl["durations"][1] == pytest.approx(5.0)
    assert tl["reveal_bases"][1] == 3.5
    assert tl["total"] == pytest.approx(4.2 + 5.0 + 3.5 - 0.7 - 0.7)


def test_build_slide_clip_reveal_base_keeps_reveal_absolute(tmp_path):
    from backend.services.renderer import build_slide_clip
    _photo(tmp_path / "a.jpg", 640, 400)
    (tmp_path / "temp").mkdir()
    item = {**_item(effect="sketch", transition="none"), "use_tts": 0, "ken_burns": 0,
            "image_fit": "cover", "overlays": "[]", "rotation": 0, "text_style": "none",
            "_reveal_base": 3.0}
    clip = build_slide_clip(item, 0, tmp_path, tmp_path / "temp", (320, 180),
                            default_slide_duration=3.0, style_preset="none", free_slide_duration=5.0)
    assert clip.duration == pytest.approx(5.0)

    def chroma(f):
        f = f.astype(int)
        return float(np.abs(f[..., 0] - f[..., 2]).mean())

    end = to_real_reveal_end(3.0, 0.5)                  # 2.4s — 늘어난 5s 가 아니라 원래 3s 기준
    assert chroma(clip.get_frame(0.1)) < 2.0
    real = chroma(clip.get_frame(4.9))
    assert chroma(clip.get_frame(end + 0.05)) == pytest.approx(real, abs=0.5)
    clip.close()


def test_preview_context_applies_hold(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.db.models import Project, Slide
    from backend.db.session import Base
    from backend.services import preview

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    monkeypatch.setattr(preview, "OUTPUTS_DIR", tmp_path)
    p = Project(name="t", default_slide_duration=3.5, transition_duration=0.7)
    db.add(p)
    db.commit()
    s0 = Slide(project_id=p.id, order_index=0, image_filename="a.jpg", text="", photo_effect="watercolor",
               effect_reveal=0.5, transition="none", meta=json.dumps({"effect_to_real": "wipe"}))
    s1 = Slide(project_id=p.id, order_index=1, image_filename="b.jpg", text="", transition="whip_right")
    db.add_all([s0, s1])
    db.commit()
    ctx = preview.compute_context(db, p.id, s0.id, include_neighbors=False, force_tts=False)
    it = ctx["items"][0]
    assert it["_free_duration"] == pytest.approx(2.8 + 1.5 + 0.35)
    assert it["_reveal_base"] == pytest.approx(3.5)
    db.close()
