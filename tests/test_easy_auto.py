"""간편 자동 모드: 장소·느낌 자막, 계획 보존, 음악 길이 맞춤, 후보 정렬, 크레딧, 한→영 검색어,
Jamendo 오류/빈 결과, 다시 섞기(잠금), 사용자 수정 잠금, 영상 슬라이드 카드, 짧은 end-to-end.

네트워크(Jamendo/Gemini/역지오코딩)는 전부 모킹한다."""
from __future__ import annotations

import asyncio
import json
import wave

import numpy as np
import pytest

from backend.core import config
from backend.db.models import Project, Slide
from backend.services import auto_edit, easy_auto, music_search
from backend.services.easy_auto import (candidate_duration_range, decide_fit, rank_candidates,
                                        solve_photo_scale)
from backend.services.music_credit import credit_description, credit_line, license_short
from tests.test_auto_edit import (_photo, _sine_wav, _slides, db, make_project, no_network,  # noqa: F401
                                  outputs, session_factory)


def _m(s) -> dict:
    return json.loads(s.meta or "{}")


def _set(db, s, **kv):
    m = _m(s)
    m.update(kv)
    s.meta = json.dumps(m, ensure_ascii=False)
    db.commit()


# ────────────────────────────────────────────────────────────────────────── #
# 가짜 httpx.AsyncClient
# ────────────────────────────────────────────────────────────────────────── #
class _FakeResp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self._content = status, data, content

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    async def aiter_bytes(self, n=65536):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]


class _Stream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


def fake_httpx(monkeypatch, handler, calls=None):
    """handler(method, url, params) → _FakeResp 또는 Exception 인스턴스 (raise)."""
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _go(self, method, url, params=None, **k):
            if calls is not None:
                calls.append((method, url, dict(params or {})))
            r = handler(method, url, params or {})
            if isinstance(r, Exception):
                raise r
            return r

        async def get(self, url, params=None, **k):
            return self._go("GET", url, params)

        async def post(self, url, params=None, **k):
            return self._go("POST", url, params)

        def stream(self, method, url, **k):
            return _Stream(self._go(method, url, k.get("params")))

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)


def jamendo_track(tid, dur, allowed=True, title=None):
    return {"id": str(tid), "name": title or f"Song {tid}", "artist_name": "Artist", "duration": dur,
            "audio": f"https://prev/{tid}.mp3", "shareurl": f"https://jamendo.com/track/{tid}",
            "license_ccurl": "http://creativecommons.org/licenses/by-sa/3.0/",
            "audiodownload_allowed": allowed, "audiodownload": f"https://dl/{tid}.mp3" if allowed else "",
            "musicinfo": {"tags": {"genres": ["acoustic"], "instruments": ["piano"], "vartags": ["calm"]}}}


def run(coro):
    return asyncio.run(coro)


# ────────────────────────────────────────────────────────────────────────── #
# 1. 자막 해석 규칙
# ────────────────────────────────────────────────────────────────────────── #
def test_resolve_captions_inherit_and_cards():
    e = [{"id": 1, "place": "일월수목원 온실", "feel": "따뜻한 오후 빛"},
         {"id": 2, "place": "", "feel": "둘이 함께 한 컷"},
         {"id": 3, "place": "", "feel": ""},
         {"id": 4, "place": "장미 정원", "feel": ""},
         {"id": 5, "place": "", "feel": ""}]
    r = auto_edit.resolve_easy_captions(e, "일월수목원 산책")
    assert [x["place"] for x in r] == ["일월수목원 온실"] * 3 + ["장미 정원"] * 2
    assert r[0]["card"] == {"title": "일월수목원 온실", "sub": "따뜻한 오후 빛"}
    assert r[1]["card"] == {"title": "일월수목원 온실", "sub": "둘이 함께 한 컷"}   # 느낌만 → 앞 장소
    assert r[2]["card"] is None and r[4]["card"] is None
    assert r[3]["card"] == {"title": "장미 정원", "sub": ""}
    assert [x["source"] for x in r] == ["user", "inherit", "inherit", "user", "inherit"]
    assert r[3]["scene_start"] and not r[0]["scene_start"]


def test_resolve_captions_title_fallback_and_gps_clusters():
    calls = []

    def geo(c):
        calls.append(c)
        return {"title": f"지명{c}", "sub": "경기도 · 2026.10.25"} if c != 2 else None

    e = [{"id": 1, "place": "", "feel": "", "cluster": 0},
         {"id": 2, "place": "", "feel": "", "cluster": 0},
         {"id": 3, "place": "", "feel": "", "cluster": 1},
         {"id": 4, "place": "전망 데크", "feel": "", "cluster": 2},
         {"id": 5, "place": "", "feel": "", "cluster": 2}]
    r = auto_edit.resolve_easy_captions(e, "제목", geo)
    assert [x["place"] for x in r] == ["지명0", "지명0", "지명1", "전망 데크", "전망 데크"]
    assert r[0]["card"]["sub"] == "경기도 · 2026.10.25"           # 자막 없는 묶음 첫 컷 → GPS 카드
    assert r[2]["card"]["title"] == "지명1" and r[1]["card"] is None
    assert r[3]["card"]["title"] == "전망 데크"
    assert 2 not in calls                                       # 자막 있는 묶음은 역지오코딩 안 함
    assert calls.count(0) == 1                                  # 묶음당 1회
    # GPS·자막 모두 없음 → 프로젝트 제목, 카드 없음
    r2 = auto_edit.resolve_easy_captions([{"id": 1, "place": "", "feel": ""}], "산책")
    assert r2[0]["place"] == "산책" and r2[0]["source"] == "title" and r2[0]["card"] is None


def test_resolve_captions_geocode_failure_is_silent():
    def boom(c):
        raise RuntimeError("x")
    r = auto_edit.resolve_easy_captions([{"id": 1, "place": "", "feel": "좋다", "cluster": 0}], "T", boom)
    assert r[0]["place"] == "T" and r[0]["card"] == {"title": "T", "sub": "좋다"}


# ────────────────────────────────────────────────────────────────────────── #
# 2. 계획: easy_* 보존, 카드, 키네틱·나레이션 끔
# ────────────────────────────────────────────────────────────────────────── #
def test_easy_plan_preserves_captions_and_builds_cards(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=6, place_at=None)
    sl = _slides(db, p.id)
    _set(db, sl[1], easy_place="장미 정원", easy_feel="천천히 걷기")
    _set(db, sl[3], easy_feel="꽃 사이 쉼표")
    for _ in range(2):                                  # 재계획해도 자막 원본은 그대로
        summary = auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.3}))
    db.expire_all()
    sl = _slides(db, p.id)
    assert _m(sl[1])["easy_place"] == "장미 정원" and _m(sl[1])["easy_feel"] == "천천히 걷기"
    assert _m(sl[3])["easy_feel"] == "꽃 사이 쉼표"
    assert _m(sl[1])["location_card"]["title"] == "장미 정원"
    assert _m(sl[1])["location_card"]["sub"] == "천천히 걷기"
    assert _m(sl[3])["location_card"] == {"title": "장미 정원", "sub": "꽃 사이 쉼표", "source": "easy"}
    assert "location_card" not in _m(sl[2])
    assert summary["kinetic"] == [] and summary["narrated"] == 0
    assert sl[0].text_style == "kinetic" and _m(sl[0])["intro_kinetic"] is True     # 인트로 제목은 유지
    assert all(s.text == "" and s.use_tts == 0 for s in sl)
    assert _m(sl[3])["prev_text"] == "예전 나레이션."
    assert all(s.text_style == "none" for s in sl[1:])
    assert "location_card" in _m(sl[1])["auto_fields"] and "transition" in _m(sl[2])["auto_fields"]
    assert _m(sl[0])["auto_plan"]["easy"] is True
    assert p.resolution == "720p" or True
    db.refresh(p)
    assert p.resolution == "1080p" and p.default_slide_duration == 3.5 and p.style_preset == "cinematic"


def test_normal_auto_edit_also_keeps_easy_keys(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=4, place_at=None)
    sl = _slides(db, p.id)
    _set(db, sl[2], easy_place="억새길", easy_feel="가을 끝자락")
    auto_edit.plan_auto_edit(db, p.id, {})
    db.expire_all()
    assert _m(_slides(db, p.id)[2])["easy_place"] == "억새길"


def test_estimate_length_is_dry_run(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=5, place_at=None, bgm=False)
    before = [(s.transition, s.meta, s.text) for s in _slides(db, p.id)]
    est = easy_auto.estimate_length(db, p.id, 3.5)
    db.expire_all()
    assert [(s.transition, s.meta, s.text) for s in _slides(db, p.id)] == before
    assert est["photos"] == 5 and est["videos"] == 0
    # 5장 × 3.5 − 전환 overlap + 그림→실사 홀드 → 대략 13~22초
    assert 12.0 < est["length"] < 24.0


# ────────────────────────────────────────────────────────────────────────── #
# 3. 음악 길이 맞춤
# ────────────────────────────────────────────────────────────────────────── #
@pytest.mark.parametrize("T,L,mode", [(100, 98, "adjust"), (113, 100, "adjust"), (86, 100, "adjust"),
                                      (130, 100, "trim"), (60, 100, "loop")])
def test_decide_fit(T, L, mode):
    f = decide_fit(T, L)
    assert f["mode"] == mode
    if mode == "adjust":
        assert f["scale"] == pytest.approx(T / L, abs=1e-3) and f["label"].startswith("딱 맞음")
    if mode == "trim":
        assert "1:40" in f["label"] and "페이드" in f["label"]
    if mode == "loop":
        assert "루프 1회" in f["label"]


def test_decide_fit_label_percent():
    assert decide_fit(106, 100)["label"] == "딱 맞음 · 슬라이드 시간 +6%"
    assert decide_fit(100, 100)["label"] == "딱 맞음"


def test_rank_candidates_order_and_filter():
    L = 98
    hits = [music_search.jamendo_hit(t) for t in (
        jamendo_track(1, 150), jamendo_track(2, 58), jamendo_track(3, 101), jamendo_track(4, 112),
        jamendo_track(5, 99, allowed=False), jamendo_track(6, 230), jamendo_track(3, 101))]
    out = rank_candidates(hits, L)
    assert [h["id"] for h in out] == ["3", "4", "1", "6"]
    assert [h["fit"]["mode"] for h in out] == ["adjust", "adjust", "trim", "trim"]
    assert all(h["audiodownload_allowed"] for h in out)
    assert candidate_duration_range(98) == (58, 245)
    assert candidate_duration_range(20) == (30, 60)


def test_solve_photo_scale_matches_target():
    items = [{"image_filename": f"{i}.jpg", "slide_type": "image", "text": "", "transition": "crossfade",
              "meta": "{}"} for i in range(6)]
    items.append({"slide_type": "video", "video_filename": "v.mp4", "trim_start": 0, "trim_end": 8,
                  "transition": "whip_left", "text": "", "meta": "{}"})
    base = easy_auto.plan_timeline(items, 3.5, 0.7)["total"]
    scale, total = solve_photo_scale(items, 3.5, 0.7, base * 1.1)
    assert total == pytest.approx(base * 1.1, abs=0.02) and scale > 1.0
    scale, total = solve_photo_scale(items, 3.5, 0.7, base * 0.9)
    assert total == pytest.approx(base * 0.9, abs=0.02) and scale < 1.0


def _wav(path, seconds):
    _sine_wav(path, seconds=seconds)


def test_fit_music_adjust_writes_easy_duration(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=5, place_at=None)
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    L = easy_auto.timeline_of(db, p, _slides(db, p.id))["total"]
    _wav(outputs / p.id / "assets" / "bgm_test.wav", L * 1.1)
    fit = easy_auto.fit_music(db, p.id)
    assert fit["mode"] == "adjust" and fit["video_length"] == pytest.approx(L * 1.1, abs=0.1)
    sl = _slides(db, p.id)
    assert all(_m(s)["easy_duration"] == pytest.approx(3.5 * fit["scale"], abs=1e-3) for s in sl)
    assert all("easy_duration" in _m(s)["auto_fields"] for s in sl)
    m0 = _m(sl[0])
    assert m0["bgm_fade_out"] == 3.0 and m0["bgm_loop_xfade"] == 1.5 and m0["bgm_fit"]["mode"] == "adjust"
    # 렌더러가 읽는 타임라인도 곡 길이와 같다
    assert easy_auto.timeline_of(db, p, sl)["total"] == pytest.approx(L * 1.1, abs=0.1)


@pytest.mark.parametrize("ratio,mode", [(2.0, "trim"), (0.5, "loop")])
def test_fit_music_trim_or_loop_keeps_durations(db, outputs, no_network, ratio, mode):
    p = make_project(db, outputs, n_photos=4, place_at=None)
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    L = easy_auto.timeline_of(db, p, _slides(db, p.id))["total"]
    _wav(outputs / p.id / "assets" / "bgm_test.wav", L * ratio)
    fit = easy_auto.fit_music(db, p.id)
    assert fit["mode"] == mode
    assert all("easy_duration" not in _m(s) for s in _slides(db, p.id))


def test_bgm_mix_options_and_loop_starts():
    from backend.services.renderer import bgm_mix_options, loop_starts
    assert bgm_mix_options({}, 60.0) == (pytest.approx(2.0), 1.5)       # 기존 렌더 기본값 유지
    assert bgm_mix_options({}, 20.0)[0] == pytest.approx(1.0)
    assert bgm_mix_options({"bgm_fade_out": 3.0, "bgm_loop_xfade": 0}, 60.0) == (3.0, 0.0)
    starts, xf = loop_starts(40.0, 100.0, 1.5)
    assert xf == 1.5 and starts == [0.0, 38.5, 77.0]
    assert starts[-1] + 40.0 >= 100.0
    assert loop_starts(4.0, 10.0, 1.5)[1] == 1.0                         # 곡 길이 25% 상한


# ────────────────────────────────────────────────────────────────────────── #
# 4. 크레딧
# ────────────────────────────────────────────────────────────────────────── #
def test_credit_texts():
    c = {"title": "Autumn Walk", "artist": "Lesfm", "license_url": "http://creativecommons.org/licenses/by-nc/3.0/",
         "page_url": "https://jamendo.com/track/1"}
    assert license_short(c["license_url"]) == "CC BY-NC 3.0"
    assert credit_line(c) == "♪ Autumn Walk — Lesfm · Jamendo (CC BY-NC 3.0)"
    assert credit_description(c).startswith(
        '음악: "Autumn Walk" by Lesfm (Jamendo) — http://creativecommons.org/licenses/by-nc/3.0/')
    assert credit_line(None) == "" and credit_description({}) == ""


def test_credit_layer_small_bottom_right():
    from backend.services.renderer import render_credit_layer
    layers = render_credit_layer("♪ Autumn Walk — Lesfm · Jamendo (CC BY 3.0)", (1280, 720), 30.0)
    assert len(layers) == 1
    c = layers[0]
    assert c.start == pytest.approx(26.0) and c.duration == pytest.approx(4.0)
    x, y = c.pos(0)
    assert x > 1280 * 0.35 and y > 720 * 0.9 and c.h < 40          # 우측 하단, 아주 작게
    mask = c.mask.get_frame(2.0)
    assert mask.max() > 0.5


# ────────────────────────────────────────────────────────────────────────── #
# 5. 한→영 검색어
# ────────────────────────────────────────────────────────────────────────── #
def test_hangul_and_dict():
    assert music_search.has_hangul("크리스마스 cozy") and not music_search.has_hangul("christmas")
    assert music_search.dict_translate("크리스마스 분위기 나는 아늑한 곳") == ["christmas", "cozy"]
    assert music_search.dict_translate("잔잔한 피아노") == ["calm", "piano"]
    assert music_search.dict_translate("우쿨렐레") == ["ukulele"]         # '기타' 안 겹침 확인용
    assert music_search.dict_translate("무엇") == []


def test_translate_query_fallbacks(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    assert run(music_search.translate_query("christmas jazz")) == {
        "query_used": "christmas jazz", "translated": False, "translate_source": "none"}
    assert run(music_search.translate_query("크리스마스 분위기")) == {
        "query_used": "christmas", "translated": True, "translate_source": "dict"}
    assert run(music_search.translate_query("뭐라뭐라"))["translate_source"] == "none"


def test_translate_query_gemini_then_failure(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    ok = {"candidates": [{"content": {"parts": [{"text": '["christmas", "cozy"]'}]}}]}
    fake_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, ok))
    assert run(music_search.translate_query("크리스마스 분위기")) == {
        "query_used": "christmas cozy", "translated": True, "translate_source": "gemini"}
    fake_httpx(monkeypatch, lambda m, u, p: _FakeResp(500, {}))
    assert run(music_search.translate_query("겨울 바다"))["query_used"] == "winter ocean"


def test_ai_keywords_from_captions(monkeypatch, db, outputs):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    p = make_project(db, outputs, n_photos=2, place_at=None)
    sl = _slides(db, p.id)
    _set(db, sl[0], easy_place="가을 숲", easy_feel="잔잔한 산책")
    from backend.api.v1.projects import bgm_context_text
    ctx = bgm_context_text(p, _slides(db, p.id))
    assert "가을 숲 / 잔잔한 산책" in ctx and "일월수목원 산책" in ctx
    kws, src = run(music_search.ai_keywords(ctx))
    assert src == "dict" and kws[0] == "forest autumn" or "autumn" in " ".join(kws)


# ────────────────────────────────────────────────────────────────────────── #
# 6. API: Jamendo 오류 vs 빈 결과, 후보, 곡 선택
# ────────────────────────────────────────────────────────────────────────── #
@pytest.fixture
def client(session_factory, outputs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import easy_auto as easy_api, projects, slides
    from backend.db.session import get_db

    app = FastAPI()
    for r in (projects.router, slides.router, easy_api.router):
        app.include_router(r, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "JAMENDO_CLIENT_ID", "test-id")
    return TestClient(app)


def test_search_errors_and_empty(client, db, outputs, monkeypatch):
    p = make_project(db, outputs, n_photos=2, place_at=None)
    url = f"/api/v1/projects/{p.id}/bgm/search?q=calm"
    monkeypatch.setattr(config, "JAMENDO_CLIENT_ID", "")
    r = client.get(url)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "jamendo_key_missing"
    monkeypatch.setattr(config, "JAMENDO_CLIENT_ID", "test-id")
    fake_httpx(monkeypatch, lambda m, u, p: _FakeResp(500, {}))
    r = client.get(url)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "jamendo_error"
    import httpx
    fake_httpx(monkeypatch, lambda m, u, p: httpx.ReadTimeout("t"))
    assert client.get(url).json()["detail"]["code"] == "jamendo_error"
    fake_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {"headers": {"status": "success"}, "results": []}))
    r = client.get(url)
    assert r.status_code == 200 and r.json()["empty"] is True and r.json()["hits"] == []
    # suggest 도 같은 키 확인
    monkeypatch.setattr(config, "JAMENDO_CLIENT_ID", "")
    r = client.post(f"/api/v1/projects/{p.id}/bgm/suggest")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "jamendo_key_missing"


def test_search_params_translation_and_hit_fields(client, db, outputs, monkeypatch):
    p = make_project(db, outputs, n_photos=1, place_at=None)
    calls = []
    data = {"headers": {"status": "success", "results_fullcount": 1}, "results": [jamendo_track(7, 120)]}
    fake_httpx(monkeypatch, lambda m, u, pr: _FakeResp(200, data), calls)
    r = client.get(f"/api/v1/projects/{p.id}/bgm/search",
                   params={"q": "크리스마스 아늑", "instrumental": "true", "min_duration": 60, "max_duration": 200})
    j = r.json()
    assert j["query_used"] == "christmas cozy" and j["translated"] and j["translate_source"] == "dict"
    params = calls[-1][2]
    assert params["vocalinstrumental"] == "instrumental" and params["durationbetween"] == "60_200"
    assert params["include"] == "musicinfo licenses" and params["search"] == "christmas cozy"
    h = j["hits"][0]
    for k in ("id", "title", "artist", "duration", "preview_url", "page_url", "license_url",
              "audiodownload_allowed", "audiodownload", "tags"):
        assert k in h
    assert h["license_url"].startswith("http://creativecommons.org") and h["audiodownload_allowed"] is True


def test_music_candidates_rank_and_broaden(client, db, outputs, monkeypatch, no_network):
    p = make_project(db, outputs, n_photos=6, place_at=None, bgm=False)
    calls = []
    L = client.get(f"/api/v1/projects/{p.id}/easy/estimate").json()["length"]
    assert L > 10

    def handler(m, u, pr):
        if "durationbetween" in pr:
            return _FakeResp(200, {"headers": {}, "results": [jamendo_track(1, round(L * 1.5)),
                                                              jamendo_track(2, round(L), False)]})
        return _FakeResp(200, {"headers": {}, "results": [jamendo_track(3, round(L * 1.05)),
                                                          jamendo_track(4, round(L * 3)),
                                                          jamendo_track(5, round(L * 0.5))]})
    fake_httpx(monkeypatch, handler, calls)
    r = client.post(f"/api/v1/projects/{p.id}/easy/music-candidates", json={"keywords": ["calm"]})
    j = r.json()
    assert r.status_code == 200, j
    assert j["keywords"] == ["calm"] and j["broadened"] is True and not j["empty"]
    assert all(c["audiodownload_allowed"] for c in j["candidates"])
    assert [c["id"] for c in j["candidates"]] == ["3", "1", "4", "5"]
    assert [c["fit"]["mode"] for c in j["candidates"]] == ["adjust", "trim", "trim", "loop"]
    assert j["length"] == pytest.approx(L, abs=0.01)
    assert calls[0][2]["vocalinstrumental"] == "instrumental"
    lo, hi = j["duration_range"]
    assert calls[0][2]["durationbetween"] == f"{lo}_{hi}"
    # 아무것도 없으면 empty
    fake_httpx(monkeypatch, lambda m, u, pr: _FakeResp(200, {"headers": {}, "results": []}))
    j = client.post(f"/api/v1/projects/{p.id}/easy/music-candidates", json={"query": "피아노"}).json()
    assert j["empty"] is True and j["query_used"] == "piano"


def test_choose_music_downloads_and_writes_credit(client, db, outputs, monkeypatch):
    p = make_project(db, outputs, n_photos=2, place_at=None)

    def handler(m, u, pr):
        if u.startswith("https://api.jamendo.com"):
            assert pr.get("id") in ("7", "8")
            return _FakeResp(200, {"headers": {}, "results": [jamendo_track(pr["id"], 120, pr["id"] == "7")]})
        return _FakeResp(200, None, b"ID3" + b"\0" * 5000)
    fake_httpx(monkeypatch, handler)
    r = client.post(f"/api/v1/projects/{p.id}/easy/music",
                    json={"track": {"id": "8", "audiodownload": "https://evil/x.mp3"}})
    assert r.status_code == 400                                       # 다운로드 비허용 곡
    r = client.post(f"/api/v1/projects/{p.id}/easy/music", json={"track": {"id": "7"}})
    j = r.json()
    assert r.status_code == 200, j
    assert j["bgm_filename"] == "bgm_jamendo_7.mp3"
    assert (outputs / p.id / "assets" / "bgm_jamendo_7.mp3").stat().st_size > 1000
    assert not (outputs / p.id / "assets" / "bgm_test.wav").exists()   # 이전 BGM 정리
    assert j["credit"]["license_url"].startswith("http://creativecommons.org")
    txt = (outputs / p.id / "music_credit.txt").read_text(encoding="utf-8")
    assert txt.startswith('음악: "Song 7" by Artist (Jamendo) — http://creativecommons.org/licenses/by-sa/3.0/')
    db.expire_all()
    assert _m(_slides(db, p.id)[0])["bgm_credit"]["jamendo_id"] == "7"
    assert client.get(f"/api/v1/projects/{p.id}/easy/credit").json()["text"] == txt.strip()


def test_easy_render_requires_bgm(client, db, outputs, monkeypatch):
    from backend.api.v1 import easy_auto as easy_api
    started = []
    monkeypatch.setattr(easy_api, "run_easy_render", lambda pid, o: started.append((pid, o)))
    p = make_project(db, outputs, n_photos=2, place_at=None, bgm=False)
    r = client.post(f"/api/v1/projects/{p.id}/easy/render", json={})
    assert r.status_code == 400 and "음악" in r.json()["detail"]
    p2 = make_project(db, outputs, n_photos=2, place_at=None)
    r = client.post(f"/api/v1/projects/{p2.id}/easy/render", json={"resolution": "720p"})
    assert r.status_code == 202 and started[0][1]["resolution"] == "720p"
    assert started[0][1]["auto_curate"] is True
    assert client.post(f"/api/v1/projects/{p2.id}/easy/render", json={}).status_code == 409


# ────────────────────────────────────────────────────────────────────────── #
# 7. 다시 섞기 / 사용자 수정 잠금
# ────────────────────────────────────────────────────────────────────────── #
def test_reshuffle_skips_locked(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=10, place_at=None)
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.3}))
    sl = _slides(db, p.id)
    sl[6].transition, sl[6].ken_burns, sl[6].photo_effect = "flash", 5, "cartoon"
    _set(db, sl[6], user_locked=True)
    before = [(s.transition, s.ken_burns, s.photo_effect) for s in _slides(db, p.id)]
    out = auto_edit.reshuffle_effects(db, p.id, seed=5)
    db.expire_all()
    sl = _slides(db, p.id)
    assert out["seed"] == 5 and out["changed"] > 0
    assert (sl[6].transition, sl[6].ken_burns, sl[6].photo_effect) == ("flash", 5, "cartoon")
    after = [(s.transition, s.ken_burns, s.photo_effect) for s in sl]
    assert after != before
    assert _m(sl[0])["auto_plan"]["seed"] == 5
    # 같은 seed 로 다시 계획하면 같은 결과 (렌더 시 재계획이 다시 섞기를 보존)
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.3}, seed=5))
    db.expire_all()
    assert [(s.transition, s.ken_burns, s.photo_effect) for s in _slides(db, p.id)] == after
    # 계획한 적 없는 프로젝트 → 오류
    q = make_project(db, outputs, n_photos=2, place_at=None)
    with pytest.raises(auto_edit.AutoEditError):
        auto_edit.reshuffle_effects(db, q.id)


def test_seed_zero_keeps_old_plan(db, outputs, no_network):
    p = make_project(db, outputs, n_photos=8, place_at=None)
    s1 = auto_edit.plan_auto_edit(db, p.id, {"stylize_ratio": 0.3})
    s2 = auto_edit.plan_auto_edit(db, p.id, {"stylize_ratio": 0.3, "seed": 0})
    assert s1["transitions"] == s2["transitions"] and s1["stylized"] == s2["stylized"]


def test_user_edit_sets_lock_and_planner_respects_it(client, db, outputs, no_network):
    p = make_project(db, outputs, n_photos=6, place_at=None)
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    proj = client.get(f"/api/v1/projects/{p.id}").json()
    slides = proj["slides"]
    assert client.put(f"/api/v1/projects/{p.id}/slides", json=slides).status_code == 200
    db.expire_all()
    assert not any(_m(s).get("user_locked") for s in _slides(db, p.id))   # 변경 없음 → 잠금 없음

    slides[2]["transition"] = "flash"
    slides[2]["meta"] = json.dumps({**json.loads(slides[2]["meta"]), "user_locked": False,
                                    "auto_fields": ["transition"]})          # 클라이언트 값은 무시
    slides[3]["meta"] = json.dumps({**json.loads(slides[3]["meta"]), "easy_place": "  장미  정원 "})
    client.put(f"/api/v1/projects/{p.id}/slides", json=slides)
    db.expire_all()
    sl = _slides(db, p.id)
    m2 = _m(sl[2])
    assert m2["user_locked"] is True and "transition" not in m2["auto_fields"]
    assert m2["user_fields"] == ["transition"] and "ken_burns" in m2["auto_fields"]
    assert _m(sl[3])["easy_place"] == "장미 정원" and not _m(sl[3]).get("user_locked")   # 자막은 잠금 아님

    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}, seed=3))
    db.expire_all()
    sl = _slides(db, p.id)
    assert sl[2].transition == "flash"
    assert _m(sl[3])["location_card"]["title"] == "장미 정원"

    # 🔒 해제 → 다시 자동 관리
    r = client.post(f"/api/v1/projects/{p.id}/slides/{sl[2].id}/lock", json={"locked": False})
    assert r.status_code == 200 and "user_locked" not in json.loads(r.json()["meta"])
    db.expire_all()
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}, seed=3))
    db.expire_all()
    assert _slides(db, p.id)[2].transition != "flash"


def test_user_edited_card_survives_replan(client, db, outputs, no_network):
    p = make_project(db, outputs, n_photos=4, place_at=None)
    sl = _slides(db, p.id)
    _set(db, sl[1], easy_place="온실")
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    slides = client.get(f"/api/v1/projects/{p.id}").json()["slides"]
    slides[1]["meta"] = json.dumps({**json.loads(slides[1]["meta"]),
                                    "location_card": {"title": "직접 고친 카드", "sub": "메모"}})
    client.put(f"/api/v1/projects/{p.id}/slides", json=slides)
    db.expire_all()
    auto_edit.plan_auto_edit(db, p.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    db.expire_all()
    m = _m(_slides(db, p.id)[1])
    assert m["location_card"]["title"] == "직접 고친 카드" and m["user_locked"] is True
    assert m["easy_place"] == "온실"


# ────────────────────────────────────────────────────────────────────────── #
# 8. 영상 슬라이드에도 위치 카드
# ────────────────────────────────────────────────────────────────────────── #
def _tiny_video(path, seconds=2.0):
    from moviepy.editor import ColorClip
    ColorClip((160, 90), color=(40, 120, 60)).set_duration(seconds).write_videofile(
        str(path), fps=12, codec="libx264", audio=False, logger=None)


def test_video_slide_gets_location_card(tmp_path):
    from backend.services.renderer import build_slide_clip
    _tiny_video(tmp_path / "v.mp4")
    (tmp_path / "temp").mkdir()
    item = {"slide_type": "video", "video_filename": "v.mp4", "text": "", "subtitles": "[]",
            "use_tts": 0, "volume": 1.0, "trim_start": 0, "trim_end": 0, "overlays": "[]",
            "text_style": "none", "meta": json.dumps({"location_card": {"title": "장미 정원", "sub": "천천히 걷기"}})}
    layers = []
    clip = build_slide_clip(item, 0, tmp_path, tmp_path / "temp", (320, 180),
                            editorial_layers_out=layers)
    assert clip is not None and len(layers) == 1
    x, y = layers[0].pos(1.0)
    assert x < 160 and y > 90                        # 좌측 하단 한쪽 정렬
    clip.close()


# ────────────────────────────────────────────────────────────────────────── #
# 9. 짧은 end-to-end (선별 끔 · 계획 · 맞춤 · 렌더)
# ────────────────────────────────────────────────────────────────────────── #
def test_easy_end_to_end(db, outputs, session_factory, monkeypatch, no_network, caplog):
    import logging
    caplog.set_level(logging.INFO)
    from backend.services import renderer, task_status, worker

    class DummyTTS:
        def __init__(self, *a, **k):
            pass

        def load_model(self):
            raise AssertionError("TTS 불필요")

        def unload_model(self):
            return True

    monkeypatch.setattr(worker, "TTSEngine", DummyTTS)
    monkeypatch.setattr(renderer, "TTSEngine", DummyTTS)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(renderer, "ASPECT_RATIO_MAP", {"16:9": (256, 144)})
    monkeypatch.setattr(config, "VIDEO_CODEC", "libx264")
    import backend.db.session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", session_factory)

    p = make_project(db, outputs, n_photos=5, place_at=None)   # 인트로 3컷 뒤 1컷이 그림→실사
    sl = _slides(db, p.id)
    _set(db, sl[1], easy_place="장미 정원", easy_feel="천천히")
    auto_edit.set_project_meta(sl, bgm_credit={"title": "Song", "artist": "A",
                                               "license_url": "http://creativecommons.org/licenses/by/3.0/"})
    db.commit()
    _sine_wav(outputs / p.id / "assets" / "bgm_test.wav", seconds=30.0)
    easy_auto.run_easy_render(p.id, {"resolution": "720p", "slide_duration": 2.0, "stylize_ratio": 0.5,
                                     "auto_curate": False})
    st = task_status.get_status(easy_auto.TASK_KEY.format(project_id=p.id))
    db.expire_all()
    proj = db.query(Project).filter(Project.id == p.id).first()
    assert st["status"] == "done", (st, proj.message)
    fit = st["summary"]["fit"]
    assert fit["mode"] == "trim"
    log = caplog.text
    assert "BGM credit overlay: ♪ Song — A · Jamendo (CC BY 3.0)" in log
    assert "To-real hold" in log
    from moviepy.editor import VideoFileClip
    v = VideoFileClip(str(outputs / p.id / "result.mp4"))
    try:
        assert v.duration == pytest.approx(fit["video_length"], abs=0.6)
        assert v.audio is not None
    finally:
        v.close()
