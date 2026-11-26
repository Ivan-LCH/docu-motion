"""YouTube 업로드: 메타 자동 생성(템플릿/Gemini), 스코프 없는 갱신 인증, upload_video, 업로드 API.

Google API·Gemini 는 전부 모킹한다 (네트워크 없음)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from google.auth import exceptions as google_exceptions
from PIL import Image

from backend.core import config
from backend.db.models import Project, Slide
from backend.services import youtube_job, youtube_manager, youtube_meta
from tests.test_auto_edit import db, outputs, session_factory  # noqa: F401

PLACE = "경기 수원시 일월수목원"
FEEL = "늦가을 오후의 고요함"
CREDIT = {"title": "Quiet Path", "artist": "Lumen", "jamendo_id": 123,
          "license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
          "page_url": "https://www.jamendo.com/track/123"}


class _P:
    name = "일월수목원 산책"


def _slides_data(credit=True):
    m0 = {"easy_place": PLACE, "easy_feel": FEEL}
    if credit:
        m0["bgm_credit"] = CREDIT
    return [{"meta": json.dumps(m0, ensure_ascii=False), "text": ""},
            {"meta": "{}", "text": "호수 옆 <벤치>"}]


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    youtube_manager.reset_cache()
    yield
    youtube_manager.reset_cache()


# ────────────────────────────────────────────────────────────────────────── #
# 메타데이터
# ────────────────────────────────────────────────────────────────────────── #
def test_template_metadata_without_key():
    r = youtube_meta.build_metadata(_P(), _slides_data())
    assert r["source"] == "template" and r["privacy"] == "private"
    assert r["title"] == "일월수목원 산책 · 늦가을 오후의 고요함"
    d = r["description"]
    assert "📍 경기 수원시 일월수목원" in d
    assert '음악: "Quiet Path" by Lumen (Jamendo)' in d and CREDIT["license_url"] in d
    assert d.count("#") == 3 and "#일월수목원" in d
    for t in ("일월수목원", "수원", "수원시", "경기", "가을", "arboretum", "autumn", "walk"):
        assert t in r["tags"]
    for v in [r["title"], d] + r["tags"]:
        assert "<" not in v and ">" not in v
    assert youtube_manager.tags_length(r["tags"]) <= 500


def test_template_falls_back_to_project_name():
    r = youtube_meta.build_metadata(_P(), [{"meta": "{}", "text": ""}])
    assert r["title"] == "일월수목원 산책" and "음악:" not in r["description"]


def test_region_tokens():
    assert youtube_meta.region_tokens(PLACE) == ["일월수목원", "수원", "수원시", "경기"]
    assert youtube_meta.region_tokens("서울특별시 종로구 경복궁") == ["경복궁", "종로", "종로구", "서울", "서울특별시"]
    assert youtube_meta.region_tokens("") == []


def test_tags_limit_dedupe_and_cost():
    tags = ["산책", "산책", "", "  ", "a <b>"] + [f"긴 태그 {i:03d} 테스트" for i in range(100)] + ["x" * 50]
    out = youtube_manager.clean_tags(tags)
    assert out[0] == "산책" and out.count("산책") == 1 and "a b" in out
    assert youtube_manager.tags_length(out) <= 500 and len(out) < 60
    assert all(len(t) <= 30 for t in out)
    assert youtube_manager.tag_cost("a b") == 5
    assert youtube_manager.clean_tags("가, 나 ,가") == ["가", "나"]


def test_title_description_limits():
    assert len(youtube_manager.clean_title("가" * 300)) == 100
    assert youtube_manager.clean_title(" <> ", "프로젝트") == "프로젝트"
    d = youtube_manager.clean_description("한" * 3000)
    assert len(d.encode("utf-8")) <= 5000 and d.endswith("한")


def test_gemini_valid_json(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    calls = []

    def fake(prompt, timeout=0):
        calls.append(prompt)
        return '```json\n{"title": "늦가을 <수목원>", "summary": "조용한 오후.\\n걷기 좋은 길.", ' \
               '"tags_ko": ["단풍길"], "tags_en": ["arboretum walk"]}\n```'
    monkeypatch.setattr(youtube_meta, "_gemini", fake)
    r = youtube_meta.build_metadata(_P(), _slides_data())
    assert r["source"] == "ai" and r["title"] == "늦가을 수목원"
    assert r["description"].startswith("조용한 오후.") and "Quiet Path" in r["description"]
    assert "단풍길" in r["tags"] and "arboretum walk" in r["tags"] and "수원" in r["tags"]
    assert PLACE in calls[0]


def test_gemini_garbage_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    monkeypatch.setattr(youtube_meta, "_gemini", lambda *a, **k: "음... 잘 모르겠어요 {not json")
    r = youtube_meta.build_metadata(_P(), _slides_data())
    assert r["source"] == "template" and "Quiet Path" in r["description"]
    monkeypatch.setattr(youtube_meta, "_gemini", lambda *a, **k: None)
    assert youtube_meta.build_metadata(_P(), _slides_data())["source"] == "template"


def test_gemini_http_layer_mocked(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")

    class R:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"title":"t","summary":"s"}'}]}}]}
    monkeypatch.setattr(youtube_meta.httpx, "post", lambda *a, **k: R())
    assert youtube_meta.build_metadata(_P(), _slides_data())["source"] == "ai"

    def boom(*a, **k):
        raise TimeoutError("slow")
    monkeypatch.setattr(youtube_meta.httpx, "post", boom)
    assert youtube_meta.build_metadata(_P(), _slides_data())["source"] == "template"


# ────────────────────────────────────────────────────────────────────────── #
# 인증
# ────────────────────────────────────────────────────────────────────────── #
@pytest.fixture
def token(tmp_path, monkeypatch):
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"token": "narrow", "refresh_token": "rt", "client_id": "cid",
                             "client_secret": "sec", "token_uri": "https://oauth2.googleapis.com/token",
                             "scopes": ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]}))
    monkeypatch.setattr(youtube_manager, "TOKEN_PATH", p)
    monkeypatch.setattr(youtube_manager, "_tokeninfo_scopes", lambda t: pytest.fail("tokeninfo called"))
    return p


def _grant(scopes, calls=None):
    def fake(request, token_uri, refresh_token, client_id, client_secret, scopes=None, **k):
        if calls is not None:
            calls.append(scopes)
        return ("at-1", refresh_token, datetime.utcnow() + timedelta(hours=1), {"scope": " ".join(scope_list)}, None)
    scope_list = scopes
    return fake


def _snapshot(p: Path):
    return p.stat().st_mtime_ns, p.read_bytes()


def test_auth_ok_unscoped_refresh_and_cache(token, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube_manager.reauth, "refresh_grant", _grant(youtube_manager.AUTH_SCOPES, calls))
    monkeypatch.setattr(youtube_manager, "_channel_title", lambda c: "내 채널")
    before = _snapshot(token)
    st = youtube_manager.get_auth_status()
    assert st == {"authenticated": True, "reason": None, "scopes_ok": True, "channel": "내 채널"}
    assert youtube_manager._youtube_credentials().token == "at-1"
    assert calls == [None]                     # 스코프 지정 없이 1회만 갱신 (캐시)
    assert _snapshot(token) == before


def test_auth_scope_missing(token, monkeypatch):
    monkeypatch.setattr(youtube_manager.reauth, "refresh_grant",
                        _grant(["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]))
    before = _snapshot(token)
    st = youtube_manager.get_auth_status()
    assert st["authenticated"] is False and st["reason"] == "scope_missing" and st["scopes_ok"] is False
    with pytest.raises(PermissionError, match="scope_missing"):
        youtube_manager.check_ready()
    assert _snapshot(token) == before


def test_auth_tokeninfo_fallback(token, monkeypatch):
    def fake(*a, **k):
        return ("at-2", "rt", datetime.utcnow() + timedelta(hours=1), {}, None)
    monkeypatch.setattr(youtube_manager.reauth, "refresh_grant", fake)
    monkeypatch.setattr(youtube_manager, "_tokeninfo_scopes", lambda t: {youtube_manager.UPLOAD_SCOPE})
    monkeypatch.setattr(youtube_manager, "_channel_title", lambda c: None)
    assert youtube_manager.get_auth_status()["authenticated"] is True


def test_auth_invalid_grant(token, monkeypatch):
    def fake(*a, **k):
        raise google_exceptions.RefreshError("invalid_grant: Token has been expired or revoked.", {})
    monkeypatch.setattr(youtube_manager.reauth, "refresh_grant", fake)
    before = _snapshot(token)
    st = youtube_manager.get_auth_status()
    assert st["authenticated"] is False and st["reason"] == "reauth_required"
    assert _snapshot(token) == before


def test_auth_no_token(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_manager, "TOKEN_PATH", tmp_path / "missing.json")
    assert youtube_manager.get_auth_status()["reason"] == "no_token"


# ────────────────────────────────────────────────────────────────────────── #
# upload_video
# ────────────────────────────────────────────────────────────────────────── #
class _Status:
    def __init__(self, p):
        self.p = p

    def progress(self):
        return self.p


class _Req:
    def __init__(self, steps):
        self.steps = list(steps)

    def next_chunk(self):
        return self.steps.pop(0)


class FakeYouTube:
    def __init__(self, thumb_fail=False):
        self.bodies, self.thumbs, self.thumb_fail = [], [], thumb_fail

    def videos(self):
        yt = self

        class V:
            def insert(self, part, body, media_body):
                yt.bodies.append(body)
                yt.media = media_body
                return _Req([(_Status(0.25), None), (_Status(0.5), None), (None, {"id": "vid123"})])
        return V()

    def thumbnails(self):
        yt = self

        class T:
            def set(self, videoId, media_body):
                class X:
                    def execute(self_inner):
                        if yt.thumb_fail:
                            raise RuntimeError("The authenticated user doesnt have permissions to upload thumbnails")
                        yt.thumbs.append(videoId)
                        return {}
                return X()
        return T()


@pytest.fixture
def fake_yt(monkeypatch, tmp_path):
    yt = FakeYouTube()
    monkeypatch.setattr(youtube_manager, "_youtube_credentials", lambda: object())
    monkeypatch.setattr(youtube_manager, "_build_service", lambda c: yt)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\0" * 1024)
    return yt, f


def test_upload_video_default_private_and_progress(fake_yt):
    yt, f = fake_yt
    pcts = []
    r = youtube_manager.upload_video(f, "제목 <x>", "설명", tags=["a", "a", "b"], progress_cb=pcts.append)
    body = yt.bodies[0]
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["title"] == "제목 x" and body["snippet"]["tags"] == ["a", "b"]
    assert body["snippet"]["categoryId"] == "22"
    assert yt.media.chunksize() == 8 * 1024 * 1024 and yt.media.resumable()
    assert pcts == [25.0, 50.0, 100.0]
    assert r == {"video_id": "vid123", "url": "https://www.youtube.com/watch?v=vid123",
                 "thumbnail_ok": False, "warning": None}


def test_upload_video_invalid_privacy(fake_yt):
    _, f = fake_yt
    with pytest.raises(ValueError):
        youtube_manager.upload_video(f, "t", "d", privacy="everyone")


def test_upload_video_thumbnail_failure_non_fatal(fake_yt, tmp_path):
    yt, f = fake_yt
    yt.thumb_fail = True
    thumb = tmp_path / "t.jpg"
    Image.new("RGB", (64, 36)).save(thumb)
    r = youtube_manager.upload_video(f, "t", "d", thumbnail_path=thumb)
    assert r["video_id"] == "vid123" and r["thumbnail_ok"] is False and r["warning"]
    yt.thumb_fail = False
    r = youtube_manager.upload_video(f, "t", "d", thumbnail_path=thumb)
    assert r["thumbnail_ok"] is True and yt.thumbs == ["vid123"]


def test_upload_short_wrapper(fake_yt):
    _, f = fake_yt
    assert youtube_manager.upload_short(f, "t", "d", "a,b") == "https://www.youtube.com/watch?v=vid123"


# ────────────────────────────────────────────────────────────────────────── #
# API
# ────────────────────────────────────────────────────────────────────────── #
@pytest.fixture
def api_env(session_factory, outputs, db, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import youtube
    from backend.db.session import get_db

    app = FastAPI()
    app.include_router(youtube.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db

    p = Project(name="일월수목원 산책")
    db.add(p)
    db.commit()
    assets = outputs / p.id / "assets"
    assets.mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (400, 300), (i * 60, 100, 100)).save(assets / f"p{i}.jpg")
        meta = {"easy_place": PLACE, "easy_feel": FEEL, "bgm_credit": CREDIT} if i == 0 else {}
        db.add(Slide(project_id=p.id, order_index=i, image_filename=f"p{i}.jpg",
                     meta=json.dumps(meta, ensure_ascii=False)))
    db.commit()
    monkeypatch.setattr(youtube_job, "_active", set())
    return TestClient(app), p.id, outputs / p.id


def test_api_meta_get_put_roundtrip(api_env):
    c, pid, d = api_env
    r = c.get(f"/api/v1/projects/{pid}/youtube/meta").json()
    assert r["title"] == "일월수목원 산책 · 늦가을 오후의 고요함" and r["privacy"] == "private"
    assert "Quiet Path" in r["credit"] and "Quiet Path" in r["description"]
    assert [t["index"] for t in r["thumbnails"]] == [0, 1, 2]
    assert r["thumbnails"][0]["url"] == f"/api/v1/projects/{pid}/assets/p0.jpg"
    assert (d / "youtube_meta.json").is_file()

    put = c.put(f"/api/v1/projects/{pid}/youtube/meta",
                json={"title": "새 제목", "description": "설명", "tags": "가, 나",
                      "privacy": "unlisted", "thumbnail": "slide:1"})
    assert put.status_code == 200
    r = c.get(f"/api/v1/projects/{pid}/youtube/meta").json()
    assert (r["title"], r["tags"], r["privacy"], r["thumbnail"]) == ("새 제목", ["가", "나"], "unlisted", "slide:1")
    regen = c.get(f"/api/v1/projects/{pid}/youtube/meta?regen=1&ai=0").json()
    assert regen["title"].startswith("일월수목원") and regen["privacy"] == "unlisted"
    assert c.put(f"/api/v1/projects/{pid}/youtube/meta", json={"privacy": "everyone"}).status_code == 422


def test_api_thumbnail_upload(api_env):
    import io
    c, pid, d = api_env
    buf = io.BytesIO()
    Image.new("RGB", (600, 900), "red").save(buf, "PNG")
    r = c.post(f"/api/v1/projects/{pid}/youtube/thumbnail", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    im = Image.open(d / "yt_thumb.jpg")
    assert im.size == (1280, 720) and im.format == "JPEG"
    assert (d / "yt_thumb.jpg").stat().st_size < 2 * 1024 * 1024
    assert c.get(f"/api/v1/projects/{pid}/youtube/thumbnail").status_code == 200
    bad = c.post(f"/api/v1/projects/{pid}/youtube/thumbnail", files={"file": ("a.png", b"nope", "image/png")})
    assert bad.status_code == 400


def test_api_upload_404_without_video(api_env):
    c, pid, _ = api_env
    assert c.post(f"/api/v1/projects/{pid}/youtube/upload", json={"title": "t"}).status_code == 404


def test_api_upload_401_when_auth_missing(api_env, monkeypatch):
    c, pid, d = api_env
    (d / "result.mp4").write_bytes(b"\0" * 10)

    def no(*a, **k):
        raise PermissionError("scope_missing")
    monkeypatch.setattr(youtube_manager, "check_ready", no)
    r = c.post(f"/api/v1/projects/{pid}/youtube/upload", json={"title": "t"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "scope_missing"
    assert c.get(f"/api/v1/projects/{pid}/youtube/status").json()["status"] == "idle"


def test_api_upload_409_while_uploading(api_env, monkeypatch):
    c, pid, d = api_env
    (d / "result.mp4").write_bytes(b"\0" * 10)
    monkeypatch.setattr(youtube_manager, "check_ready", lambda: None)
    youtube_job._active.add(pid)
    youtube_job.update_state(pid, status="uploading", progress=40.0)
    assert c.post(f"/api/v1/projects/{pid}/youtube/upload", json={"title": "t"}).status_code == 409
    assert c.get(f"/api/v1/projects/{pid}/youtube/status").json()["status"] == "uploading"
    youtube_job._active.discard(pid)          # 프로세스 재시작 → 끊긴 작업은 오류로 보임
    assert c.get(f"/api/v1/projects/{pid}/youtube/status").json()["status"] == "error"


def test_api_upload_202_then_done(api_env, monkeypatch):
    c, pid, d = api_env
    (d / "result.mp4").write_bytes(b"\0" * 10)
    monkeypatch.setattr(youtube_manager, "check_ready", lambda: None)
    seen = {}

    def fake_upload(file_path, title, description, tags=None, privacy="private", thumbnail_path=None,
                    progress_cb=None, category_id="22", fallback_title=""):
        seen.update(title=title, privacy=privacy, tags=tags, thumb=thumbnail_path, desc=description)
        seen["mid"] = youtube_job.get_state(pid)["status"]
        progress_cb(45.0)
        seen["pct"] = youtube_job.get_state(pid)["progress"]
        return {"video_id": "v1", "url": "https://www.youtube.com/watch?v=v1", "thumbnail_ok": True, "warning": None}
    monkeypatch.setattr(youtube_manager, "upload_video", fake_upload)
    r = c.post(f"/api/v1/projects/{pid}/youtube/upload",
               json={"title": "제목", "description": "설명", "tags": ["a", "b"], "thumbnail": "slide:2"})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    st = c.get(f"/api/v1/projects/{pid}/youtube/status").json()
    assert st["status"] == "done" and st["url"] == "https://www.youtube.com/watch?v=v1" and st["privacy"] == "private"
    assert seen["mid"] == "uploading" and seen["pct"] == 45.0
    assert seen["privacy"] == "private" and seen["tags"] == ["a", "b"] and seen["thumb"].endswith("yt_thumb_slide.jpg")
    assert Image.open(seen["thumb"]).size == (1280, 720)
    saved = json.loads((d / "youtube_meta.json").read_text())
    assert saved["title"] == "제목" and saved["thumbnail"] == "slide:2"
    # 레거시 본문 (tags 쉼표 문자열, privacy 없음) + 잘못된 privacy
    assert c.post(f"/api/v1/projects/{pid}/youtube/upload", json={"title": "t", "tags": "x, y"}).status_code == 202
    assert seen["tags"] == ["x", "y"] and seen["privacy"] == "private"
    assert c.post(f"/api/v1/projects/{pid}/youtube/upload", json={"title": "t", "privacy": "all"}).status_code == 422


def test_api_upload_error_state(api_env, monkeypatch):
    c, pid, d = api_env
    (d / "result.mp4").write_bytes(b"\0" * 10)
    monkeypatch.setattr(youtube_manager, "check_ready", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("quotaExceeded")
    monkeypatch.setattr(youtube_manager, "upload_video", boom)
    assert c.post(f"/api/v1/projects/{pid}/youtube/upload", json={}).status_code == 202
    st = c.get(f"/api/v1/projects/{pid}/youtube/status").json()
    assert st["status"] == "error" and "한도" in st["error"]
    assert not youtube_job.is_active(pid)


def test_auth_status_route_shape(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import youtube
    app = FastAPI()
    app.include_router(youtube.router, prefix="/api/v1")
    monkeypatch.setattr(youtube_manager, "_youtube_credentials",
                        lambda: (_ for _ in ()).throw(PermissionError("reauth_required")))
    r = TestClient(app).get("/api/v1/youtube/auth-status").json()
    assert r == {"authenticated": False, "reason": "reauth_required", "scopes_ok": False, "channel": None}


# ────────────────────────────────────────────────────────────────────────── #
# 금지어
# ────────────────────────────────────────────────────────────────────────── #
def test_banned_word_absent():
    root = Path(__file__).resolve().parent.parent
    words = ["".join(map(chr, (0xC9C0, 0xBE0C, 0xB9AC))), "".join(map(chr, (71, 104, 105, 98, 108, 105)))]
    hits = []
    for base in (root / "backend", root / "frontend" / "src"):
        for f in base.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".ts", ".tsx", ".css", ".json", ".html", ".md"):
                text = f.read_text(encoding="utf-8", errors="ignore")
                if any(w.lower() in text.lower() for w in words):
                    hits.append(str(f))
    assert hits == []
