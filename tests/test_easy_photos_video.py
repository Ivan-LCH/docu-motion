"""간편 자동 — 구글 포토에서 동영상 선택: Picker 응답 해석, 영상 다운로드(=dv · .mp4 · 포스터 · 처리 상태 ·
부분 파일 정리 · 용량 상한), 긴 영상 미리 자르기, 클립 구간·오디오, 길이 추정·음악 맞춤, 순서/삭제, 그림 효과 제외,
렌더용 클립 준비(회전·캔버스 맞춤).

네트워크 없음: Picker/다운로드는 requests.get 을 가로채 ffmpeg 로 만든 작은 mp4 바이트를 돌려준다."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.db.models import Slide
from backend.services import auto_edit, easy_auto, media_import, photos_manager
from backend.services.presets import EASY_CLIP_MAX, easy_clip_window, plan_timeline
from tests.test_auto_edit import (_photo, _slides, db, make_project, no_network,  # noqa: F401
                                  outputs, session_factory)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


# ────────────────────────────────────────────────────────────────────────── #
# 테스트 영상 (ffmpeg lavfi testsrc + sine)
# ────────────────────────────────────────────────────────────────────────── #
def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def _make_video(path: Path, seconds: float, size="160x90", rotate: int = 0, audio=True, rate=12):
    tmp = path.with_name(path.stem + "_src.mp4") if rotate else path
    args = ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    _ffmpeg(*args, str(tmp))
    if rotate:
        _ffmpeg("-display_rotation", str(rotate), "-i", str(tmp), "-c", "copy", str(path))
        tmp.unlink()
    return path


@pytest.fixture(scope="module")
def vids(tmp_path_factory):
    d = tmp_path_factory.mktemp("vids")
    return {
        "land8": _make_video(d / "land8.mp4", 8),
        "land4": _make_video(d / "land4.mp4", 4),
        "land12": _make_video(d / "land12.mp4", 12),
        "portrait": _make_video(d / "portrait.mp4", 10, size="160x90", rotate=90),
        "long": _make_video(d / "long.mp4", 70, size="64x36", audio=False, rate=6),
    }


# ────────────────────────────────────────────────────────────────────────── #
# Picker 응답 / 다운로드 모킹
# ────────────────────────────────────────────────────────────────────────── #
def picked(id_, filename, mime="", kind="PHOTO", status=None, created="2026-10-25T10:00:00Z"):
    mf = {"baseUrl": f"https://lh3.example/{id_}", "filename": filename,
          "mediaFileMetadata": {"width": 1920, "height": 1080, "creationTime": created}}
    if mime:
        mf["mimeType"] = mime
    if status:
        mf["mediaFileMetadata"]["videoMetadata"] = {"fps": 30, "processingStatus": status}
    return {"id": id_, "createTime": created, "type": kind, "mediaFile": mf}


class FakeResp:
    def __init__(self, data: bytes, ctype="video/mp4", status=200, fail_after=None, length=None):
        self.data, self.status_code, self.fail_after = data, status, fail_after
        self.headers = {"Content-Type": ctype,
                        "Content-Length": str(length if length is not None else len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        sent = 0
        for i in range(0, len(self.data), 1024):
            if self.fail_after is not None and sent >= self.fail_after:
                raise ConnectionError("connection reset")
            chunk = self.data[i:i + 1024]
            sent += len(chunk)
            yield chunk


@pytest.fixture
def fake_google(monkeypatch):
    """requests.get: '…=dv' / '…=d' URL → 등록된 응답. 호출 기록은 calls."""
    state = {"routes": {}, "calls": [], "items": []}
    monkeypatch.setattr(photos_manager, "_get_headers",
                        lambda: {"Authorization": "Bearer test", "Content-Type": "application/json"})

    def fake_get(url, headers=None, params=None, stream=False, timeout=None):
        state["calls"].append({"url": url, "headers": headers, "timeout": timeout})
        if url.endswith("/mediaItems"):
            return FakeJson({"mediaItems": state["items"]})
        if url not in state["routes"]:
            raise AssertionError(f"unexpected url {url}")
        return state["routes"][url]
    monkeypatch.setattr(photos_manager.http_requests, "get", fake_get)
    monkeypatch.setattr(photos_manager.http_requests, "delete", lambda *a, **k: None)
    return state


class FakeJson:
    def __init__(self, data):
        self._d, self.status_code = data, 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def _jpeg_bytes(tmp_path, name="ph.jpg"):
    p = tmp_path / name
    _photo(p)
    return p.read_bytes()


@pytest.fixture
def photos_client(session_factory, outputs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import photos as photos_api, slides as slides_api
    from backend.db.session import get_db

    app = FastAPI()
    app.include_router(photos_api.router, prefix="/api/v1")
    app.include_router(slides_api.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(photos_api, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(slides_api, "OUTPUTS_DIR", outputs)
    return TestClient(app)


# ────────────────────────────────────────────────────────────────────────── #
# 1. Picker 응답 해석
# ────────────────────────────────────────────────────────────────────────── #
def test_parse_picked_item_detects_video_by_type_or_mime():
    v_type_only = photos_manager.parse_picked_item(picked("a", "IMG_1.MOV", mime="", kind="VIDEO",
                                                          status="READY"))
    assert v_type_only["isVideo"] and v_type_only["processingStatus"] == "READY"
    v_mime = photos_manager.parse_picked_item(picked("b", "x.mp4", mime="video/mp4", kind="TYPE_UNSPECIFIED"))
    assert v_mime["isVideo"] and v_mime["processingStatus"] == ""
    photo = photos_manager.parse_picked_item(picked("c", "p.jpg", mime="image/jpeg"))
    assert not photo["isVideo"] and photo["type"] == "PHOTO" and photo["width"] == 1920


def test_picker_uri_autoclose():
    assert photos_manager.with_autoclose("https://photos.google.com/picker/abc") == \
        "https://photos.google.com/picker/abc/autoclose"
    assert photos_manager.with_autoclose("https://x/abc/autoclose") == "https://x/abc/autoclose"


# ────────────────────────────────────────────────────────────────────────── #
# 2. 가져오기 (엔드투엔드, 모킹)
# ────────────────────────────────────────────────────────────────────────── #
def test_import_mixed_photos_and_videos(photos_client, db, outputs, fake_google, vids, tmp_path):
    p = make_project(db, outputs, n_photos=0, place_at=None)
    fake_google["items"] = [
        picked("p1", "IMG_0001.JPG", "image/jpeg", created="2026-10-25T10:00:00Z"),
        picked("v1", "IMG_0002.MOV", "video/quicktime", "VIDEO", "READY", created="2026-10-25T10:01:00Z"),
        picked("v2", "IMG_0003.MOV", "", "VIDEO", "READY", created="2026-10-25T10:02:00Z"),     # mimeType 없음
        picked("v3", "IMG_0004.MOV", "video/quicktime", "VIDEO", "PROCESSING", created="2026-10-25T10:03:00Z"),
        picked("v4", "IMG_0005.MOV", "video/quicktime", "VIDEO", "FAILED", created="2026-10-25T10:04:00Z"),
    ]
    jpg = _jpeg_bytes(tmp_path)
    fake_google["routes"] = {
        "https://lh3.example/p1=d": FakeResp(jpg, "image/jpeg"),
        "https://lh3.example/v1=dv": FakeResp(vids["land8"].read_bytes()),
        "https://lh3.example/v2=dv": FakeResp(vids["portrait"].read_bytes()),
    }
    r = photos_client.post(f"/api/v1/photos/import/{p.id}", json={"session_id": "s1", "sort_order": "selected"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["count"], body["photos"], body["videos"], body["failed"]) == (3, 1, 2, 2)
    assert {e["filename"] for e in body["errors"]} == {"IMG_0004.MOV", "IMG_0005.MOV"}
    assert "처리 중" in next(e["error"] for e in body["errors"] if e["filename"] == "IMG_0004.MOV")

    # PROCESSING/FAILED 는 다운로드조차 하지 않는다, 영상은 =dv + Bearer + 넉넉한 읽기 타임아웃
    urls = [c["url"] for c in fake_google["calls"]]
    assert "https://lh3.example/v3=dv" not in urls and "https://lh3.example/v3=d" not in urls
    dv = next(c for c in fake_google["calls"] if c["url"].endswith("v2=dv"))
    assert dv["headers"]["Authorization"] == "Bearer test" and dv["timeout"][1] >= 120

    db.expire_all()
    sl = _slides(db, p.id)
    assert [s.slide_type for s in sl] == ["image", "video", "video"]
    assets = outputs / p.id / "assets"
    for s in sl[1:]:
        assert s.video_filename.endswith(".mp4")                      # .MOV 여도 =dv 는 mp4
        m = json.loads(s.meta)
        assert (assets / m["poster"]).is_file() and m["poster"].endswith(".poster.jpg")
        assert m["video_duration"] > 3
        assert s.trim_start == 0 and s.trim_end == 0 and s.volume == 1.0   # 가져오기 직후는 원본 그대로
    assert not list(assets.glob("*.part"))


def test_import_rejects_thumbnail_bytes_for_video(photos_client, db, outputs, fake_google, tmp_path):
    """type VIDEO 인데 받은 내용이 영상이 아니면(썸네일 JPEG) 저장하지 않는다."""
    p = make_project(db, outputs, n_photos=0, place_at=None)
    fake_google["items"] = [picked("v1", "clip.MOV", "", "VIDEO", "READY")]
    fake_google["routes"] = {"https://lh3.example/v1=dv": FakeResp(_jpeg_bytes(tmp_path), "video/mp4")}
    body = photos_client.post(f"/api/v1/photos/import/{p.id}", json={"session_id": "s"}).json()
    assert body["count"] == 0 and body["failed"] == 1
    assets = outputs / p.id / "assets"
    assert not [f for f in assets.iterdir() if f.name.startswith("clip")]
    db.expire_all()
    assert _slides(db, p.id) == []


def test_download_partial_file_removed_on_error(fake_google, tmp_path, vids):
    data = vids["land8"].read_bytes()
    fake_google["routes"] = {"https://lh3.example/x=dv": FakeResp(data, fail_after=4096)}
    dest = tmp_path / "x.mp4"
    with pytest.raises(ConnectionError):
        photos_manager.download_item("https://lh3.example/x", "", str(dest), is_video=True)
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


def test_download_size_cap_and_thumbnail_guard(fake_google, tmp_path, vids):
    data = vids["land8"].read_bytes()
    fake_google["routes"] = {
        "https://lh3.example/big=dv": FakeResp(data, length=10 * 1024 ** 3),
        "https://lh3.example/stream=dv": FakeResp(data, length=0),
        "https://lh3.example/thumb=dv": FakeResp(data, ctype="image/jpeg"),
    }
    with pytest.raises(photos_manager.DownloadError):
        photos_manager.download_item("https://lh3.example/big", "", str(tmp_path / "a.mp4"), is_video=True)
    with pytest.raises(photos_manager.DownloadError):               # Content-Length 없이 스트림 중 초과
        photos_manager.download_item("https://lh3.example/stream", "", str(tmp_path / "b.mp4"),
                                     is_video=True, max_bytes=2048)
    with pytest.raises(photos_manager.DownloadError):
        photos_manager.download_item("https://lh3.example/thumb", "", str(tmp_path / "c.mp4"), is_video=True)
    assert not list(tmp_path.iterdir())


def test_pretrim_long_video(tmp_path, vids):
    # 키프레임 드묾(기본 x264 GOP 250 @ 6fps ≈ 41초) → 스트림 복사가 길게 남아 재인코딩 경로
    p = tmp_path / "long.mp4"
    shutil.copy(vids["long"], p)
    assert media_import.pretrim_long_video(p) is True
    d = media_import.ffprobe(p)["duration"]
    assert 10 <= d <= 20
    # 휴대폰처럼 1초 GOP → 스트림 복사
    p2 = tmp_path / "gop.mp4"
    _ffmpeg("-i", str(vids["long"]), "-c:v", "libx264", "-preset", "ultrafast", "-g", "6", str(p2))
    assert media_import.pretrim_long_video(p2) is True
    assert 14 <= media_import.ffprobe(p2)["duration"] <= 17
    short = tmp_path / "short.mp4"
    shutil.copy(vids["land8"], short)
    assert media_import.pretrim_long_video(short) is False


# ────────────────────────────────────────────────────────────────────────── #
# 3. 클립 구간 · 오디오
# ────────────────────────────────────────────────────────────────────────── #
def test_easy_clip_window_rule():
    assert easy_clip_window(4.0) == (0.0, 4.0)                    # 짧으면 전체
    assert easy_clip_window(EASY_CLIP_MAX) == (0.0, EASY_CLIP_MAX)
    assert easy_clip_window(6.4) == (0.4, 6.4)                    # 조금 길면 끝에 맞춰
    assert easy_clip_window(8.0) == (1.0, 7.0)                    # 가운데
    assert easy_clip_window(30.0) == (12.0, 18.0)
    ts, te = easy_clip_window(6.8)
    assert ts == 0.5 and te == 6.5                                # 앞 0.5초는 건너뜀


def _add_video(db, p, outputs, src: Path, order: int, name: str, **kw) -> Slide:
    assets = outputs / p.id / "assets"
    shutil.copy(src, assets / name)
    s = Slide(project_id=p.id, order_index=order, slide_type="video", video_filename=name, image_filename="",
              label=name, text="", volume=kw.pop("volume", 1.0), trim_start=kw.pop("trim_start", 0.0),
              trim_end=kw.pop("trim_end", 0.0), subtitles="[]", use_tts=1,
              meta=json.dumps(kw.pop("meta", {})), exif="{}")
    db.add(s)
    db.commit()
    return s


def _easy_opts(**kw):
    return {**easy_auto.easy_plan_opts({"resolution": "720p", "stylize_ratio": 0.5}), "use_geocode": False, **kw}


@pytest.fixture
def mixed(db, outputs, vids):
    """사진 6 + 영상 3 (8초 · 12초 세로 회전 · 4초)."""
    p = make_project(db, outputs, n_photos=6, place_at=None)
    for s in _slides(db, p.id):
        s.order_index *= 2
    db.commit()
    _add_video(db, p, outputs, vids["land8"], 1, "v8.mp4")
    _add_video(db, p, outputs, vids["portrait"], 5, "vport.mp4")
    _add_video(db, p, outputs, vids["land4"], 9, "v4.mp4")
    return p


def test_plan_sets_clip_windows_and_mutes(db, mixed, no_network):
    summary = auto_edit.plan_auto_edit(db, mixed.id, _easy_opts())
    vids_ = [s for s in _slides(db, mixed.id) if s.slide_type == "video"]
    assert [(s.trim_start, s.trim_end) for s in vids_] == [(1.0, 7.0), (2.0, 8.0), (0.0, 4.0)]
    assert all(s.volume == 0.0 for s in vids_)
    assert all(json.loads(s.meta)["video_fit"] == "auto" for s in vids_)
    assert len(summary["clips"]) == 3
    # 다시 계획해도 같은 값 (자동 값으로 인식)
    auto_edit.plan_auto_edit(db, mixed.id, _easy_opts(seed=3))
    assert [(s.trim_start, s.trim_end, s.volume) for s in _slides(db, mixed.id) if s.slide_type == "video"] == \
        [(1.0, 7.0, 0.0), (2.0, 8.0, 0.0), (0.0, 4.0, 0.0)]


def test_plan_respects_user_trim_volume_and_lock(db, mixed, no_network):
    auto_edit.plan_auto_edit(db, mixed.id, _easy_opts())
    v8, vport, v4 = [s for s in _slides(db, mixed.id) if s.slide_type == "video"]
    v8.trim_start, v8.trim_end = 0.0, 8.0            # 사용자가 전체 사용
    vport.volume = 0.4                              # 사용자가 원본 소리 살림
    m = json.loads(v4.meta)
    m["user_locked"] = True
    v4.meta = json.dumps(m)
    v4.trim_start, v4.trim_end, v4.volume = 0.5, 3.0, 1.0
    db.commit()
    auto_edit.plan_auto_edit(db, mixed.id, _easy_opts())
    v8, vport, v4 = [s for s in _slides(db, mixed.id) if s.slide_type == "video"]
    assert (v8.trim_start, v8.trim_end, v8.volume) == (0.0, 8.0, 0.0)
    assert (vport.trim_start, vport.trim_end, vport.volume) == (2.0, 8.0, 0.4)
    assert (v4.trim_start, v4.trim_end, v4.volume) == (0.5, 3.0, 1.0)


def test_stylize_and_ken_burns_skip_videos(db, mixed, no_network):
    summary = auto_edit.plan_auto_edit(db, mixed.id, _easy_opts())
    ids = {s.id for s in _slides(db, mixed.id) if s.slide_type == "video"}
    assert summary["stylized"] and not ({x["slide_id"] for x in summary["stylized"]} & ids)
    for s in _slides(db, mixed.id):
        if s.slide_type == "video":
            assert s.photo_effect in (None, "none") and not s.ken_burns
            assert "effect_to_real" not in json.loads(s.meta)
    auto_edit.reshuffle_effects(db, mixed.id, seed=11)
    assert all(s.photo_effect in (None, "none") for s in _slides(db, mixed.id) if s.slide_type == "video")


# ────────────────────────────────────────────────────────────────────────── #
# 4. 길이 추정 · 음악 맞춤 — 트림된 길이 사용, 사진만 늘고 줄어든다
# ────────────────────────────────────────────────────────────────────────── #
def test_estimate_uses_trimmed_clip_durations(db, mixed, no_network):
    est = easy_auto.estimate_length(db, mixed.id, 3.5)
    assert est["videos"] == 3 and est["photos"] == 6
    assert est["video_seconds"] == pytest.approx(6 + 6 + 4, abs=0.05)        # 원본 합 22초가 아님
    db.expire_all()
    assert all(s.trim_end == 0 for s in _slides(db, mixed.id) if s.slide_type == "video")   # dry-run


def test_fit_music_scales_only_photos(db, mixed, outputs, no_network):
    from tests.test_auto_edit import _sine_wav
    auto_edit.plan_auto_edit(db, mixed.id, _easy_opts(stylize_ratio=0.0))
    db.expire_all()
    natural = easy_auto.timeline_of(db, mixed, _slides(db, mixed.id))["total"]
    assets = outputs / mixed.id / "assets"
    target = round(natural * 1.1, 1)
    _sine_wav(assets / "bgm_test.wav", seconds=target)
    fit = easy_auto.fit_music(db, mixed.id)
    assert fit["mode"] == "adjust" and fit["scale"] > 1.0
    assert fit["video_length"] == pytest.approx(target, abs=0.6)
    db.expire_all()
    sl = _slides(db, mixed.id)
    for s in sl:
        m = json.loads(s.meta)
        if s.slide_type == "video":
            assert "easy_duration" not in m
        else:
            assert m["easy_duration"] == pytest.approx(3.5 * fit["scale"], abs=1e-3)
    tl = easy_auto.timeline_of(db, mixed, sl)
    vids_ = [d for d, s in zip(tl["durations"], sl) if s.slide_type == "video"]
    assert vids_ == pytest.approx([6.0, 6.0, 4.0], abs=0.01)


# ────────────────────────────────────────────────────────────────────────── #
# 5. 순서 변경 · 삭제 (영상 포함)
# ────────────────────────────────────────────────────────────────────────── #
def test_reorder_and_delete_video_slide(photos_client, db, mixed, outputs):
    assets = outputs / mixed.id / "assets"
    sl = _slides(db, mixed.id)
    vid = next(s for s in sl if s.video_filename == "vport.mp4")
    poster = media_import.make_poster(assets / "vport.mp4")
    assert poster and poster.is_file()
    ids = [s.id for s in sl]
    new = [vid.id] + [i for i in ids if i != vid.id]
    r = photos_client.put(f"/api/v1/projects/{mixed.id}/slides/order", json={"slide_ids": new})
    assert r.status_code == 200 and r.json()[0]["slide_type"] == "video"
    r = photos_client.delete(f"/api/v1/projects/{mixed.id}/slides/{vid.id}")
    assert r.status_code == 204
    assert not (assets / "vport.mp4").exists() and not poster.exists()
    db.expire_all()
    left = _slides(db, mixed.id)
    assert [s.order_index for s in left] == list(range(len(ids) - 1))


# ────────────────────────────────────────────────────────────────────────── #
# 6. 렌더용 클립 (회전 · 캔버스 맞춤 · 무음)
# ────────────────────────────────────────────────────────────────────────── #
def test_prepare_clip_bakes_rotation_and_fills_canvas(tmp_path, vids):
    info = media_import.probe_video(vids["portrait"])
    assert info["rotation"] in (90, 270) and info["height"] > info["width"]
    assert media_import.clip_fit_mode(info["width"], info["height"], (320, 180), "auto") == "blur"
    assert media_import.clip_fit_mode(160, 90, (320, 180), "auto") == "cover"
    out = media_import.prepare_clip(vids["portrait"], tmp_path / "o.mp4", 2.0, 8.0, (320, 180),
                                    fit="auto", audio=False)
    o = media_import.probe_video(out)
    assert (o["width"], o["height"], o["rotation"], o["has_audio"]) == (320, 180, 0, False)
    assert o["duration"] == pytest.approx(6.0, abs=0.15)


def test_build_slide_clip_easy_video(tmp_path, vids):
    from backend.services.renderer import build_slide_clip
    assets = tmp_path / "assets"
    (assets / "temp_render").mkdir(parents=True)
    shutil.copy(vids["portrait"], assets / "v.mp4")
    item = {"slide_type": "video", "video_filename": "v.mp4", "volume": 0.0, "trim_start": 2.0, "trim_end": 8.0,
            "subtitles": "[]", "text": "", "text_style": "none", "overlays": "[]",
            "meta": json.dumps({"video_fit": "auto"})}
    clip = build_slide_clip(item, 0, assets, assets / "temp_render", (320, 180))
    assert tuple(clip.size) == (320, 180)
    assert clip.duration == pytest.approx(6.0, abs=0.15)
    assert clip.audio is None
    frame = clip.get_frame(1.0)
    assert frame.shape == (180, 320, 3)
    assert frame[:, :40].mean() > 5          # 옆이 검은 띠가 아니라 흐린 배경
