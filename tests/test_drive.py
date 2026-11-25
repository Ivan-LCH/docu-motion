"""Google 드라이브 가져오기: 설정/인증 상태, 토큰 스코프 확인, 가져오기(정렬·건너뛰기·크기 상한). HTTP 는 모킹."""
from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from backend.db.models import Slide
from backend.services import drive_manager, photos_manager
from tests.test_auto_edit import db, make_project, outputs, session_factory  # noqa: F401

CLIENT = {"web": {"client_id": "123456789012-abc.apps.googleusercontent.com", "client_secret": "x",
                  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                  "token_uri": "https://oauth2.googleapis.com/token"}}


class _Creds:
    token = "tok-1"
    expiry = None


class _Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self._content = status, data, content

    def json(self):
        return self._data

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _jpeg(color=(200, 120, 40)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture
def drive_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_PICKER_API_KEY", "browser-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)
    monkeypatch.setattr(photos_manager, "_load_client_config", lambda: CLIENT)
    monkeypatch.setattr(photos_manager, "_get_credentials", lambda: _Creds())
    drive_manager._scope_cache.clear()


def _scopes(monkeypatch, scopes, files=None, calls=None):
    files = files or {}

    def get(url, params=None, headers=None, **k):
        if calls is not None:
            calls.append((url, dict(params or {})))
        if url == drive_manager.TOKENINFO_URL:
            return _Resp(200, {"scope": " ".join(scopes)})
        fid = url.rsplit("/", 1)[-1]
        assert headers and headers["Authorization"] == "Bearer tok-1"
        f = files.get(fid)
        if f is None:
            return _Resp(404, {})
        if (params or {}).get("alt") == "media":
            return _Resp(200, content=f["content"])
        return _Resp(200, f["meta"])
    monkeypatch.setattr(drive_manager.http_requests, "get", get)


def test_config_disabled_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_PICKER_API_KEY", raising=False)
    monkeypatch.setattr(photos_manager, "_load_client_config", lambda: None)
    cfg = drive_manager.get_config()
    assert cfg["enabled"] is False and "GOOGLE_PICKER_API_KEY" in cfg["reason"]
    assert "OAuth" in cfg["reason"]


def test_config_enabled_and_scope_check(drive_env, monkeypatch):
    _scopes(monkeypatch, [photos_manager.PICKER_SCOPE])
    cfg = drive_manager.get_config()
    assert cfg["enabled"] is True and cfg["app_id"] == "123456789012"   # client_id 앞 숫자
    assert cfg["api_key"] == "browser-key" and cfg["authenticated"] is False
    assert "drive.file" in cfg["reason"]
    with pytest.raises(drive_manager.DriveError) as ei:
        drive_manager.get_access_token()
    assert ei.value.status == 401

    drive_manager._scope_cache.clear()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "999")
    _scopes(monkeypatch, [photos_manager.PICKER_SCOPE, drive_manager.DRIVE_FILE_SCOPE])
    cfg = drive_manager.get_config()
    assert cfg["authenticated"] is True and cfg["app_id"] == "999"
    assert drive_manager.get_access_token()["access_token"] == "tok-1"


def test_auth_url_requests_both_picker_scopes(drive_env):
    url = drive_manager.generate_auth_url("http://localhost")
    q = parse_qs(urlparse(url).query)
    scopes = q["scope"][0].split()
    assert drive_manager.DRIVE_FILE_SCOPE in scopes and photos_manager.PICKER_SCOPE in scopes
    assert q["include_granted_scopes"] == ["true"] and q["access_type"] == ["offline"]


def test_hints_and_sort():
    from backend.api.v1.drive import sort_imported
    meta = {"imageMediaMetadata": {"time": "2026:10:25 10:30:05", "location": {"latitude": 37.2, "longitude": 127.0}}}
    assert drive_manager.captured_hint(meta) == "2026-10-25T10:30:05"
    assert drive_manager.gps_hint(meta) == {"lat": 37.2, "lng": 127.0}
    assert drive_manager.captured_hint({}) is None
    rows = [{"n": "c", "captured": "2026-10-25T12:00:00"}, {"n": "v", "captured": None},
            {"n": "a", "captured": "2026-10-25T09:00:00"}, {"n": "b", "captured": "2026-10-25T10:00:00"}]
    assert [r["n"] for r in sort_imported(rows, "captured")] == ["a", "b", "c", "v"]
    assert [r["n"] for r in sort_imported(rows, "selected")] == ["c", "v", "a", "b"]


def _client(session_factory, outputs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import drive
    from backend.db.session import get_db
    app = FastAPI()
    app.include_router(drive.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(drive, "OUTPUTS_DIR", outputs)
    return TestClient(app)


def test_import_sorts_skips_and_creates_slides(drive_env, monkeypatch, session_factory, outputs, db):
    files = {
        "f1": {"meta": {"id": "f1", "name": "late.jpg", "mimeType": "image/jpeg", "size": "900",
                        "imageMediaMetadata": {"time": "2026:10:25 12:00:00"}}, "content": _jpeg()},
        "f2": {"meta": {"id": "f2", "name": "doc.pdf", "mimeType": "application/pdf", "size": "10"},
               "content": b"%PDF"},
        "f3": {"meta": {"id": "f3", "name": "early.jpg", "mimeType": "image/jpeg", "size": "900",
                        "imageMediaMetadata": {"time": "2026:10:25 09:00:00",
                                               "location": {"latitude": 37.29, "longitude": 126.97}}},
               "content": _jpeg((10, 200, 30))},
        "f4": {"meta": {"id": "f4", "name": "huge.mp4", "mimeType": "video/mp4", "size": str(3 * 1024 ** 3)},
               "content": b""},
    }
    _scopes(monkeypatch, [drive_manager.DRIVE_FILE_SCOPE], files)
    client = _client(session_factory, outputs, monkeypatch)
    p = make_project(db, outputs, n_photos=1, place_at=None)
    r = client.post(f"/api/v1/drive/import/{p.id}",
                    json={"file_ids": ["f1", "f2", "f3", "f4", "nope"], "sort_order": "captured"})
    j = r.json()
    assert r.status_code == 200, j
    assert j["count"] == 2 and all(i["id"] for i in j["imported"])
    reasons = {s["file_id"]: s["reason"] for s in j["skipped"]}
    assert "지원하지 않는 형식" in reasons["f2"] and "2GB" in reasons["f4"] and "nope" in reasons
    db.expire_all()
    sl = db.query(Slide).filter(Slide.project_id == p.id).order_by(Slide.order_index).all()
    assert len(sl) == 3 and sl[0].image_filename == "p0.jpg"             # 기존 슬라이드 뒤에 추가
    assert sl[1].label == "early.jpg" and sl[2].label == "late.jpg"     # 촬영 시각순
    ex = json.loads(sl[1].exif)
    assert ex["captured_at"] == "2026-10-25T09:00:00" and ex["gps"] == {"lat": 37.29, "lng": 126.97}
    assert (outputs / p.id / "assets" / sl[1].image_filename).stat().st_size > 100


def test_import_requires_drive_scope(drive_env, monkeypatch, session_factory, outputs, db):
    _scopes(monkeypatch, [photos_manager.PICKER_SCOPE])
    client = _client(session_factory, outputs, monkeypatch)
    p = make_project(db, outputs, n_photos=1, place_at=None)
    r = client.post(f"/api/v1/drive/import/{p.id}", json={"file_ids": ["a"]})
    assert r.status_code == 401
    assert client.get("/api/v1/drive/token").status_code == 401


def test_stream_size_cap(tmp_path):
    with pytest.raises(drive_manager.DriveError):
        drive_manager._stream_to(_Resp(content=b"x" * 5000), str(tmp_path / "f"), max_bytes=1000)
    assert not (tmp_path / "f").exists()
