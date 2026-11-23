"""자동 편집 1단계: 역지오코딩, 위치 카드, 키네틱 타이포, 계획, 짧은 end-to-end 렌더.

네트워크는 전부 모킹한다 (httpx / TTS 엔진). DB 는 임시 sqlite 파일.
"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core import config
from backend.db.models import Project, Slide
from backend.db.session import Base
from backend.services import auto_edit, geo_label, map_service


# ────────────────────────────────────────────────────────────────────────── #
# 공용 픽스처
# ────────────────────────────────────────────────────────────────────────── #
@pytest.fixture
def outputs(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(config, "OUTPUTS_DIR", out)
    monkeypatch.setattr(config, "KAKAO_REST_API_KEY", "")
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(map_service, "_nominatim_sleep", lambda: None)
    return out


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call in test")
    monkeypatch.setattr(geo_label.httpx, "get", boom)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db(session_factory):
    s = session_factory()
    yield s
    s.close()


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _photo(path: Path, w=320, h=200, hue=0):
    img = Image.new("RGB", (w, h), (90 + hue % 120, 150, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(h * 0.6), w, h], fill=(60, 140 - hue % 60, 70))
    d.ellipse([int(w * 0.6), 10, int(w * 0.85), int(h * 0.4)], fill=(240, 200, 60))
    img.save(path, "JPEG", quality=85)


def _sine_wav(path: Path, seconds=12.0, sr=22050):
    t = np.arange(int(sr * seconds)) / sr
    # 0.5초마다 짧은 타격음 → 비트 스냅이 잡을 온셋
    env = 0.25 + 0.75 * (np.mod(t, 0.5) < 0.06)
    y = (0.3 * env * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())


ILWOL_ADDRESS = "일월수목원, 장안구, 수원시, 경기도, 16416, 대한민국"


def make_project(db, outputs: Path, n_photos=10, place_at=1, gps=None, bgm=True,
                 title="일월수목원 산책"):
    """사진 n장 (+ place 슬라이드). gps: 사진별 (lat,lng)|None 리스트."""
    p = Project(name="일월수목원", title_text=title, aspect_ratio="16:9", resolution="720p")
    db.add(p)
    db.commit()
    assets = outputs / p.id / "assets"
    assets.mkdir(parents=True)
    if bgm:
        _sine_wav(assets / "bgm_test.wav")
        p.bgm_filename = "bgm_test.wav"
    order = 0
    for i in range(n_photos):
        if place_at is not None and order == place_at:
            _photo(assets / "place.png", 320, 180)
            db.add(Slide(project_id=p.id, order_index=order, slide_type="place",
                         image_filename="place.png", label="일월수목원",
                         meta=json.dumps({"type": "place", "name": "일월수목원",
                                          "address": ILWOL_ADDRESS,
                                          "lat": 37.2883, "lng": 126.9758}, ensure_ascii=False)))
            order += 1
        fn = f"p{i}.jpg"
        _photo(assets / fn, hue=i * 23)
        minute = 50 + i * 4
        exif = {"captured_at": f"2026-10-25T{10 + minute // 60:02d}:{minute % 60:02d}:00",
                "gps": None}
        if gps and gps[i] is not None:
            exif["gps"] = {"lat": gps[i][0], "lng": gps[i][1]}
        db.add(Slide(project_id=p.id, order_index=order, slide_type="image",
                     image_filename=fn, label=fn, text="예전 나레이션." if i == 3 else "",
                     use_tts=1, exif=json.dumps(exif)))
        order += 1
    db.commit()
    return p


def _slides(db, pid):
    return db.query(Slide).filter(Slide.project_id == pid).order_by(Slide.order_index).all()


# ────────────────────────────────────────────────────────────────────────── #
# 1. 역지오코딩
# ────────────────────────────────────────────────────────────────────────── #
KAKAO_C2A = {"documents": [{
    "road_address": {"address_name": "경기 수원시 장안구 일월로 61", "region_1depth_name": "경기",
                     "region_2depth_name": "수원시 장안구", "region_3depth_name": "천천동",
                     "building_name": "일월수목원"},
    "address": {"address_name": "경기 수원시 장안구 천천동 1", "region_1depth_name": "경기",
                "region_2depth_name": "수원시 장안구", "region_3depth_name": "천천동"},
}]}

NOMINATIM_REV = {
    "name": "일월수목원",
    "display_name": ILWOL_ADDRESS,
    "address": {"leisure": "일월수목원", "borough": "장안구", "city": "수원시",
                "province": "경기도", "postcode": "16416", "country": "대한민국",
                "country_code": "kr"},
}


def test_reverse_geocode_kakao(outputs, monkeypatch):
    monkeypatch.setattr(config, "KAKAO_REST_API_KEY", "test-key")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        assert url.endswith("geo/coord2address.json")
        assert params == {"x": 126.9758, "y": 37.2883}
        return _Resp(KAKAO_C2A)

    monkeypatch.setattr(geo_label.httpx, "get", fake_get)
    r = geo_label.reverse_geocode(37.2883, 126.9758)
    assert r == {"name": "일월수목원", "city": "수원", "region": "경기도",
                 "country": "대한민국", "provider": "kakao"}
    assert len(calls) == 1


def test_reverse_geocode_kakao_regioncode_fallback():
    r = geo_label.parse_kakao_regioncode({"documents": [
        {"region_type": "B", "region_1depth_name": "서울특별시", "region_2depth_name": "종로구",
         "region_3depth_name": "세종로"},
        {"region_type": "H", "region_1depth_name": "서울특별시", "region_2depth_name": "종로구",
         "region_3depth_name": "사직동"}]})
    assert r["name"] == "사직동" and r["region"] == "서울특별시" and r["provider"] == "kakao"
    assert geo_label.parse_kakao_coord2address({"documents": []}) is None


def test_reverse_geocode_nominatim(outputs, monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _Resp(NOMINATIM_REV)

    monkeypatch.setattr(geo_label.httpx, "get", fake_get)
    r = geo_label.reverse_geocode(37.28832, 126.97583)
    assert r == {"name": "일월수목원", "city": "수원", "region": "경기도",
                 "country": "대한민국", "provider": "nominatim"}
    assert seen["url"].endswith("/reverse")
    assert seen["params"]["accept-language"] == "ko"
    assert seen["headers"]["User-Agent"] == config.MAP_USER_AGENT


def test_reverse_geocode_nominatim_without_name_uses_area():
    r = geo_label.parse_nominatim_reverse({"address": {
        "neighbourhood": "행궁동", "city": "Suwon-si", "state": "Gyeonggi-do", "country": "South Korea"}})
    assert r["name"] == "행궁동" and r["city"] == "Suwon-si" and r["region"] == "Gyeonggi-do"
    assert geo_label.parse_nominatim_reverse({"error": "Unable to geocode"}) is None


def test_reverse_geocode_cache_hit(outputs, monkeypatch):
    monkeypatch.setattr(geo_label.httpx, "get", lambda *a, **k: _Resp(NOMINATIM_REV))
    first = geo_label.reverse_geocode(37.28831, 126.97581)
    assert first and (outputs / "_geocache.json").exists()
    cache = json.loads((outputs / "_geocache.json").read_text(encoding="utf-8"))
    assert "37.2883,126.9758" in cache

    def boom(*a, **k):
        raise AssertionError("should be served from cache")
    monkeypatch.setattr(geo_label.httpx, "get", boom)
    # 소수점 4자리까지 같은 좌표 → 캐시
    assert geo_label.reverse_geocode(37.288349, 126.975849) == first


def test_reverse_geocode_failure_returns_none(outputs, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(geo_label.httpx, "get", fail)
    assert geo_label.reverse_geocode(37.5, 127.0) is None
    monkeypatch.setattr(geo_label.httpx, "get", lambda *a, **k: _Resp({}, status=503))
    assert geo_label.reverse_geocode(37.5, 127.0) is None
    assert geo_label.reverse_geocode("abc", None) is None
    assert geo_label.reverse_geocode(123.0, 0.0) is None
    assert not (outputs / "_geocache.json").exists()   # 실패는 캐시하지 않음


def test_city_short_and_address_parts():
    assert geo_label.city_short("수원시") == "수원"
    assert geo_label.city_short("수원시 장안구") == "수원"
    assert geo_label.city_short("서울특별시") == "서울"
    assert geo_label.city_short("부산광역시") == "부산"
    assert geo_label.city_short("양평군") == "양평"
    assert geo_label.city_short("Suwon-si") == "Suwon-si"
    assert geo_label.city_short("") == ""
    parts = geo_label.parse_address_parts(ILWOL_ADDRESS)
    assert parts == {"city": "수원시", "region": "경기도", "country": "대한민국"}
    kakao = geo_label.parse_address_parts("경기 수원시 장안구 일월로 61")
    assert kakao["city"] == "수원시" and kakao["region"] == "경기도"


# ────────────────────────────────────────────────────────────────────────── #
# 2. 위치 카드 계획
# ────────────────────────────────────────────────────────────────────────── #
SUWON = (37.2883, 126.9758)
HWASEONG = (37.2871, 127.0119)     # ~3.2km 떨어진 수원화성 → 다른 클러스터


def _fake_geo(lat, lng, use_cache=True):
    if abs(lng - SUWON[1]) < 0.01:
        return {"name": "일월수목원", "city": "수원", "region": "경기도",
                "country": "대한민국", "provider": "kakao"}
    return {"name": "화성행궁", "city": "수원", "region": "경기도",
            "country": "대한민국", "provider": "kakao"}


def test_cluster_two_gps_groups_two_cards(db, outputs, monkeypatch):
    monkeypatch.setattr(auto_edit, "reverse_geocode", _fake_geo)
    gps = [None, SUWON, (SUWON[0] + 0.001, SUWON[1]), HWASEONG, None, HWASEONG]
    p = make_project(db, outputs, n_photos=6, place_at=None, gps=gps)
    slides = _slides(db, p.id)
    clusters = auto_edit.cluster_slides(slides)
    assert [len(c["slides"]) for c in clusters] == [3, 3]   # 앞의 GPS 없는 컷은 첫 클러스터로

    cards = auto_edit.plan_location_cards(p, slides)
    db.commit()
    assert [c["source"] for c in cards] == ["gps", "gps"]
    assert [c["order_index"] for c in cards] == [0, 3]
    assert cards[0]["title"] == "수원 일월수목원"
    assert cards[1]["title"] == "수원 화성행궁"
    assert cards[0]["sub"] == "경기도 · 2026.10.25"
    m0 = json.loads(_slides(db, p.id)[0].meta)
    assert m0["location_card"]["source"] == "gps"


def test_fallback_card_uses_place_address(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=4, place_at=1)
    slides = _slides(db, p.id)
    cards = auto_edit.plan_location_cards(p, slides)
    assert len(cards) == 1
    c = cards[0]
    assert c["title"] == "수원 일월수목원"
    assert c["sub"] == "경기도 · 2026.10.25"
    assert c["source"] == "fallback"
    # place 슬라이드가 아니라 첫 사진 슬라이드에
    assert c["order_index"] == 0 and slides[0].slide_type == "image"
    assert "location_card" not in json.loads(slides[1].meta or "{}")


def test_fallback_card_without_place_uses_title(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=3, place_at=None, title="주말 나들이")
    cards = auto_edit.plan_location_cards(p, _slides(db, p.id))
    assert cards[0]["title"] == "주말 나들이" and cards[0]["source"] == "fallback"


def test_gps_but_geocode_fails_falls_back(db, outputs, monkeypatch):
    monkeypatch.setattr(auto_edit, "reverse_geocode", lambda *a, **k: None)
    p = make_project(db, outputs, n_photos=3, place_at=1, gps=[SUWON, SUWON, SUWON])
    cards = auto_edit.plan_location_cards(p, _slides(db, p.id))
    assert len(cards) == 1 and cards[0]["source"] == "fallback"
    assert cards[0]["title"] == "수원 일월수목원"


# ────────────────────────────────────────────────────────────────────────── #
# 3/4. 렌더: 위치 카드 + 키네틱
# ────────────────────────────────────────────────────────────────────────── #
@pytest.mark.parametrize("size", [(1280, 720), (720, 1280), (720, 720)])
def test_kinetic_and_card_layers_render_rgba(size):
    from backend.services.renderer import (
        compose_layers_rgba, render_kinetic_layer, render_location_card_layer)
    dur = 3.0
    kin = render_kinetic_layer("일월수목원 산책", size, dur)
    card = render_location_card_layer({"title": "수원 일월수목원", "sub": "경기도 · 2026.10.25"},
                                      size, dur)
    assert len(kin) == 1 and len(card) == 1

    start = compose_layers_rgba(kin + card, size, 0.0)
    mid = compose_layers_rgba(kin + card, size, 1.5)
    end = compose_layers_rgba(kin + card, size, dur - 0.01)
    for f in (start, mid, end):
        assert f.shape == (size[1], size[0], 4) and f.dtype == np.uint8
    assert start[..., 3].max() == 0            # 등장 전
    assert end[..., 3].max() < 40              # 퇴장 페이드
    alpha = mid[..., 3]
    assert (alpha > 200).sum() > 500           # 글자/카드가 실제로 그려짐

    # 카드: 좌측 상단 사분면, 키네틱: 세로 중앙보다 아래
    card_only = compose_layers_rgba(card, size, 1.5)[..., 3]
    ys, xs = np.nonzero(card_only > 128)
    assert xs.mean() < size[0] / 2 and ys.mean() < size[1] / 3
    kin_only = compose_layers_rgba(kin, size, 1.5)[..., 3]
    ys, _ = np.nonzero(kin_only > 128)
    assert size[1] * 0.45 < ys.mean() < size[1] * 0.8


def test_kinetic_highlight_is_yellow():
    from backend.services.renderer import compose_layers_rgba, render_kinetic_layer, _kinetic_units
    assert [u for u, _, _ in _kinetic_units("가을숲길")] == list("가을숲길")      # 짧은 한 단어 → 글자
    assert [h for _, h, _ in _kinetic_units("가을숲길", "숲길")] == [False, False, True, True]
    assert [h for _, h, _ in _kinetic_units("천천히 걷기")] == [False, True]    # 기본: 마지막 단어
    assert [h for _, h, _ in _kinetic_units("오전 11시", "오전")] == [True, False]

    size = (640, 360)
    f = compose_layers_rgba(render_kinetic_layer("가을 숲길", size, 3.0), size, 1.8)
    rgb, a = f[..., :3].astype(int), f[..., 3]
    solid = a > 250
    yellow = solid & (rgb[..., 0] > 220) & (rgb[..., 1] > 180) & (rgb[..., 2] < 80)
    white = solid & (rgb.min(axis=2) > 225)
    assert yellow.sum() > 100 and white.sum() > 100


def test_slide_clip_with_kinetic_card_and_photo_effect(tmp_path):
    from backend.services.renderer import build_slide_clip
    _photo(tmp_path / "a.jpg", 640, 400)
    (tmp_path / "temp").mkdir()
    item = {"image_filename": "a.jpg", "label": "", "text": "", "slide_type": "image",
            "use_tts": 0, "photo_effect": "sketch", "effect_reveal": 0.45, "ken_burns": 40,
            "image_fit": "cover", "transition": "crossfade", "overlays": "[]", "rotation": 0,
            "text_style": "kinetic",
            "meta": json.dumps({"kinetic_text": "가을 숲길",
                                "location_card": {"title": "수원 일월수목원", "sub": "경기도"}},
                               ensure_ascii=False)}
    clip = build_slide_clip(item, 0, tmp_path, tmp_path / "temp", (480, 270),
                            default_slide_duration=2.5, style_preset="cinematic")
    assert clip is not None and abs(clip.duration - 2.5) < 1e-6
    # CompositeVideoClip: 배경 + 사진 + 키네틱 + 카드
    assert len(clip.clips) == 4
    frame = clip.get_frame(1.4)
    assert frame.shape == (270, 480, 3)
    clip.close()


def test_kinetic_text_style_registered():
    from backend.schema.project import SlideUpdate
    from backend.services.presets import VALID_TEXT_STYLES, resolve_style_text
    assert "kinetic" in VALID_TEXT_STYLES
    assert SlideUpdate(id="s", order_index=0, text_style="kinetic").text_style == "kinetic"
    item = {"meta": json.dumps({"kinetic_text": "천천히 걷기"}, ensure_ascii=False)}
    assert resolve_style_text(item, "kinetic") == "천천히 걷기"
    assert resolve_style_text({"label": "일월수목원"}, "kinetic") == "일월수목원"


def test_slides_meta_merge_keeps_card_source():
    from backend.api.v1.slides import _merge_style_meta
    existing = json.dumps({"frames": ["f.png"], "location_card":
                           {"title": "A", "sub": "B", "source": "gps"}})
    merged = json.loads(_merge_style_meta(existing, json.dumps(
        {"kinetic_text": "가을 숲길", "location_card": {"title": "수원 일월수목원", "sub": "경기도"},
         "frames": ["evil.png"]}, ensure_ascii=False)))
    assert merged["frames"] == ["f.png"]
    assert merged["kinetic_text"] == "가을 숲길"
    assert merged["location_card"] == {"title": "수원 일월수목원", "sub": "경기도", "source": "gps"}
    cleared = json.loads(_merge_style_meta(json.dumps(merged), json.dumps(
        {"location_card": {"title": ""}, "kinetic_text": ""})))
    assert "location_card" not in cleared and "kinetic_text" not in cleared


def test_intro_options_and_codec_selection(monkeypatch):
    from backend.services import renderer
    slides = [{"text_style": "kinetic",
               "meta": json.dumps({"intro_kinetic": True, "bgm_fade_in": 1.0})}]
    assert renderer._intro_options(slides) == {"kinetic": True, "bgm_fade_in": 1.0}
    # 사용자가 첫 슬라이드 스타일을 바꾸면 기존 인트로 타이틀이 돌아온다
    assert renderer._intro_options([{**slides[0], "text_style": "none"}])["kinetic"] is False
    assert renderer._intro_options([{"meta": "{}"}]) == {"kinetic": False, "bgm_fade_in": 0.0}

    monkeypatch.setattr(config, "VIDEO_CODEC", "libx264")
    assert renderer._video_codecs() == ["libx264"]
    monkeypatch.setattr(config, "VIDEO_CODEC", "h264_nvenc")
    assert renderer._video_codecs() == ["h264_nvenc", "libx264"]
    monkeypatch.setattr(config, "VIDEO_CODEC", "")
    expected = ["h264_nvenc", "libx264"] if Path("/dev/nvidia0").exists() else ["libx264"]
    assert renderer._video_codecs() == expected


# ────────────────────────────────────────────────────────────────────────── #
# 5. 자동 편집 계획
# ────────────────────────────────────────────────────────────────────────── #
def test_template_phrases_ilwol():
    from datetime import datetime
    times = [datetime(2026, 10, 25, 10, 58), datetime(2026, 10, 25, 11, 9),
             datetime(2026, 10, 25, 11, 22)]
    ctx = "일월수목원 산책 일월수목원"
    assert auto_edit.template_phrases(3, times, ctx) == ["가을 숲길", "오전 11시", "천천히 걷기"]
    assert auto_edit.template_phrases(2, times[:2], ctx) == ["가을 숲길", "천천히 걷기"]
    no_time = auto_edit.template_phrases(3, [None, None, None], "")
    assert len(no_time) == 3 and len(set(no_time)) == 3
    assert all(len(p) <= auto_edit.KINETIC_MAX_CHARS for p in no_time)


def test_plan_auto_edit_ilwol_like(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=12, place_at=1)
    summary = auto_edit.plan_auto_edit(db, p.id, {})
    slides = _slides(db, p.id)
    n = len(slides)
    metas = [json.loads(s.meta or "{}") for s in slides]

    # 인트로: 5초 / 3초 → 2장, 무나레이션, 첫 장 키네틱 제목
    assert summary["intro_slides"] == [0, 1]
    assert summary["intro_seconds"] == 5.0
    for s in slides[:2]:
        assert s.use_tts == 0 and s.text == ""
    assert slides[0].text_style == "kinetic"
    assert metas[0]["kinetic_text"] == "일월수목원 산책"
    assert metas[0]["intro_kinetic"] is True and metas[0]["bgm_fade_in"] == 1.0

    # 위치 카드: GPS 없음 → place 주소 폴백, 첫 사진 슬라이드
    assert [(c["order_index"], c["title"], c["source"]) for c in summary["cards"]] == \
        [(0, "수원 일월수목원", "fallback")]
    assert metas[0]["location_card"]["sub"] == "경기도 · 2026.10.25"

    # 중간 키네틱: 2~3장, 서로/인트로와 인접하지 않음, 카드 슬라이드 아님
    kin = [k["order_index"] for k in summary["kinetic"]]
    assert 2 <= len(kin) <= 3
    assert all(b - a > 1 for a, b in zip(kin, kin[1:]))
    assert min(kin) > 2 and max(kin) < n - 1
    for k in summary["kinetic"]:
        s = slides[k["order_index"]]
        assert s.text_style == "kinetic" and s.slide_type == "image"
        assert json.loads(s.meta)["kinetic_text"] == k["text"]
    assert [k["text"] for k in summary["kinetic"]][0] == "가을 숲길"

    # 나머지: 텍스트 없음 + 무나레이션 (기존 나레이션은 meta 에 백업)
    special = {0, *kin}
    for i, s in enumerate(slides):
        assert s.use_tts == 0 and s.text == ""
        if i not in special:
            assert s.text_style == "none"
    assert any(m.get("prev_text") == "예전 나레이션." for m in metas)

    # 전환: 인트로 직후 zoom_through, 그 외 순환. Ken Burns 유지
    tr = summary["transitions"]
    assert tr[1] == "crossfade" and tr[2] == "zoom_through"
    assert tr[3:] == [auto_edit.AUTO_TRANSITION_CYCLE[i % len(auto_edit.AUTO_TRANSITION_CYCLE)]
                      for i in range(n - 3)]
    assert all(s.ken_burns >= 20 for s in slides if s.slide_type == "image")
    assert all((s.photo_effect or "none") == "none" for s in slides)   # Stage 2 미적용

    proj = db.query(Project).filter(Project.id == p.id).first()
    assert proj.style_preset == "cinematic" and proj.default_slide_duration == 3.0
    json.dumps(summary, ensure_ascii=False)   # 직렬화 가능

    # 재실행해도 같은 결과 (멱등)
    again = auto_edit.plan_auto_edit(db, p.id, {})
    assert again["kinetic"] == summary["kinetic"] and again["cards"] == summary["cards"]


def test_plan_whips_on_cluster_change(db, outputs, monkeypatch):
    monkeypatch.setattr(auto_edit, "reverse_geocode", _fake_geo)
    gps = [SUWON] * 5 + [HWASEONG] * 5 + [SUWON] * 4
    p = make_project(db, outputs, n_photos=14, place_at=None, gps=gps)
    summary = auto_edit.plan_auto_edit(db, p.id, {})
    assert [c["order_index"] for c in summary["cards"]] == [0, 5, 10]
    assert all(c["source"] == "gps" for c in summary["cards"])
    tr = summary["transitions"]
    assert tr[2] == "zoom_through"
    assert tr[5] == "whip_left" and tr[10] == "whip_right"
    kin = {k["order_index"] for k in summary["kinetic"]}
    assert not kin & {0, 5, 10}


def test_plan_requires_bgm(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=3, bgm=False)
    with pytest.raises(auto_edit.AutoEditError, match="BGM 필요"):
        auto_edit.plan_auto_edit(db, p.id, {})
    # bgm_path 를 주면 assets 로 복사해 사용
    src = outputs / "music.wav"
    _sine_wav(src, seconds=2)
    summary = auto_edit.plan_auto_edit(db, p.id, {"bgm_path": str(src)})
    assert summary["bgm"] == "bgm_music.wav"
    assert (outputs / p.id / "assets" / "bgm_music.wav").is_file()


def test_plan_options_off_keeps_old_behaviour(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=6, place_at=1)
    s = auto_edit.plan_auto_edit(db, p.id, {"location_cards": False, "kinetic": False,
                                            "music_intro": False})
    assert s["cards"] == [] and s["kinetic"] == [] and s["kinetic_intro"] is None
    assert s["intro_slides"] == []
    assert all(sl.text_style == "none" for sl in _slides(db, p.id))


def test_plan_ai_phrases_mocked(db, outputs, no_network, monkeypatch):
    from backend.services import narration
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(narration, "_post_gemini",
                        lambda parts, cfg=None: '["단풍 터널", "바람 한 줌", "느린 오후"]')
    p = make_project(db, outputs, n_photos=12, place_at=1)
    s = auto_edit.plan_auto_edit(db, p.id, {"use_ai": True})
    assert [k["text"] for k in s["kinetic"]] == ["단풍 터널", "바람 한 줌", "느린 오후"]
    # 형식이 틀리면 템플릿 폴백
    monkeypatch.setattr(narration, "_post_gemini", lambda parts, cfg=None: "음... 모르겠어요")
    s = auto_edit.plan_auto_edit(db, p.id, {"use_ai": True})
    assert s["kinetic"][0]["text"] == "가을 숲길"


# ────────────────────────────────────────────────────────────────────────── #
# 6. API
# ────────────────────────────────────────────────────────────────────────── #
def test_api_auto_edit(db, outputs, session_factory, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import auto_edit as api
    from backend.db.session import get_db
    from backend.services import task_status

    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(api, "OUTPUTS_DIR", outputs)
    started = []
    monkeypatch.setattr(api, "run_auto_edit", lambda pid, opts: started.append((pid, opts)))
    client = TestClient(app)

    assert client.post("/api/v1/projects/nope/auto-edit", json={}).status_code == 404
    no_bgm = make_project(db, outputs, n_photos=2, bgm=False)
    r = client.post(f"/api/v1/projects/{no_bgm.id}/auto-edit", json={})
    assert r.status_code == 400 and "BGM 필요" in r.json()["detail"]

    p = make_project(db, outputs, n_photos=3)
    r = client.post(f"/api/v1/projects/{p.id}/auto-edit",
                    json={"render": False, "resolution": "1080p", "kinetic": False})
    assert r.status_code == 202
    assert started and started[0][0] == p.id and started[0][1]["resolution"] == "1080p"
    assert started[0][1]["kinetic"] is False and started[0][1]["narration"] is False
    st = client.get(f"/api/v1/projects/{p.id}/auto-edit/status").json()
    assert st["task"]["status"] == "running" and st["render"]["status"] == "DRAFT"
    assert client.post(f"/api/v1/projects/{p.id}/auto-edit", json={}).status_code == 409
    assert client.post(f"/api/v1/projects/{p.id}/auto-edit",
                       json={"resolution": "4k"}).status_code == 422
    client.delete(f"/api/v1/projects/{p.id}/auto-edit")
    assert task_status.get_status(api.TASK_KEY.format(project_id=p.id)) is None


# ────────────────────────────────────────────────────────────────────────── #
# 7. 짧은 end-to-end 렌더 (계획 → worker.run_render → mp4)
# ────────────────────────────────────────────────────────────────────────── #
def test_end_to_end_render(db, outputs, session_factory, monkeypatch, no_network, caplog):
    import logging
    caplog.set_level(logging.INFO)
    from backend.services import renderer, task_status, worker

    class DummyTTS:
        def __init__(self, *a, **k):
            pass

        def load_model(self):
            raise AssertionError("TTS 로드 불필요 (음악 전용)")

        def unload_model(self):
            return True

    monkeypatch.setattr(worker, "TTSEngine", DummyTTS)
    monkeypatch.setattr(renderer, "TTSEngine", DummyTTS)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(renderer, "ASPECT_RATIO_MAP", {"16:9": (426, 240)})
    monkeypatch.setattr(config, "VIDEO_CODEC", "libx264")
    import backend.db.session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", session_factory)

    p = make_project(db, outputs, n_photos=3, place_at=None)
    auto_edit.run_auto_edit(p.id, {"slide_duration": 2.0, "intro_seconds": 2.0,
                                   "style_preset": "vlog"})
    st = task_status.get_status(auto_edit.TASK_KEY.format(project_id=p.id))
    db.expire_all()
    proj = db.query(Project).filter(Project.id == p.id).first()
    assert st["status"] == "done", (st, proj.message)
    assert proj.status == "COMPLETED"
    summary = st["summary"]
    assert summary["kinetic_intro"]["text"] == "일월수목원 산책"
    assert summary["cards"][0]["source"] == "fallback"
    log = caplog.text
    assert "Intro title skipped" in log          # 키네틱 인트로 → 기존 중앙 타이틀 중복 없음
    assert "TTS 모델 로드 생략" in log
    assert "codec=libx264" in log and "codec=h264_nvenc" not in log

    out = outputs / p.id / "result.mp4"
    assert out.exists() and out.stat().st_size > 5000
    from moviepy.editor import VideoFileClip
    v = VideoFileClip(str(out))
    try:
        assert v.size == [426, 240]
        assert 4.0 < v.duration < 8.0
        assert v.audio is not None
        assert v.get_frame(1.0).shape == (240, 426, 3)
    finally:
        v.close()
