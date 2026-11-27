"""촬영 시각순 — 구글 포토 가져오기 기본 'oldest'(사진·영상 섞어서, captured_utc 저장) +
POST /projects/{id}/slides/sort-by-capture-time (저장 시각 → 파일명 → EXIF / ffprobe, 시각 모르는 슬라이드는 앞에 붙어 간다).

네트워크 없음: Picker/다운로드는 test_easy_photos_video 의 fake_google 로 가로챈다."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend.db.models import Slide
from tests.test_auto_edit import _photo, _slides, db, make_project, outputs, session_factory  # noqa: F401
from tests.test_easy_photos_video import (FakeResp, _jpeg_bytes, fake_google, photos_client,  # noqa: F401
                                          picked, vids)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


# ────────────────────────────────────────────────────────────────────────── #
# 1. 가져오기 기본값 = 오래된 순 (Picker 는 최신 먼저 · 같은 날은 영상이 사진 위)
# ────────────────────────────────────────────────────────────────────────── #
def test_import_default_oldest_mixes_photos_and_videos(photos_client, db, outputs, fake_google, vids, tmp_path):
    from backend.api.v1.photos import ImportSessionRequest
    assert ImportSessionRequest(session_id="s").sort_order == "oldest"

    p = make_project(db, outputs, n_photos=0, place_at=None)
    fake_google["items"] = [
        picked("v2", "VID_2.MOV", "video/quicktime", "VIDEO", "READY", created="2026-10-25T10:05:00Z"),
        picked("p2", "IMG_2.JPG", "image/jpeg", created="2026-10-25T19:04:00+09:00"),
        picked("pX", "IMG_0009.JPG", "image/jpeg", created=""),                  # 시각 모름 → p2 뒤
        picked("v1", "VID_1.MOV", "video/quicktime", "VIDEO", "READY", created="2026-10-25T10:01:00.5Z"),
        picked("p1", "IMG_1.JPG", "image/jpeg", created="2026-10-25T10:00:00Z"),
    ]
    jpg, mp4 = _jpeg_bytes(tmp_path), vids["land4"].read_bytes()
    fake_google["routes"] = {
        "https://lh3.example/p1=d": FakeResp(jpg, "image/jpeg"),
        "https://lh3.example/p2=d": FakeResp(jpg, "image/jpeg"),
        "https://lh3.example/pX=d": FakeResp(jpg, "image/jpeg"),
        "https://lh3.example/v1=dv": FakeResp(mp4),
        "https://lh3.example/v2=dv": FakeResp(mp4),
    }
    r = photos_client.post(f"/api/v1/photos/import/{p.id}", json={"session_id": "s1"})   # sort_order 생략
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 5

    db.expire_all()
    sl = _slides(db, p.id)
    assert [s.label for s in sl] == ["IMG_1.JPG", "VID_1.MOV", "IMG_2.JPG", "IMG_0009.JPG", "VID_2.MOV"]
    assert [s.slide_type for s in sl] == ["image", "video", "image", "image", "video"]
    exifs = [json.loads(s.exif or "{}") for s in sl]
    assert exifs[0]["captured_utc"] == "2026-10-25T10:00:00Z"
    assert exifs[1]["captured_utc"] == "2026-10-25T10:01:00.500000Z"
    assert exifs[2]["captured_utc"] == "2026-10-25T10:04:00Z"
    assert "captured_utc" not in exifs[3]


# ────────────────────────────────────────────────────────────────────────── #
# 2. 기존 슬라이드 다시 정렬
# ────────────────────────────────────────────────────────────────────────── #
@pytest.fixture
def organize_client(session_factory, outputs):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import organize
    from backend.db.session import get_db

    app = FastAPI()
    app.include_router(organize.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _exif_jpeg(path: Path, when: str):
    img = Image.new("RGB", (64, 48), (10, 20, 30))
    ex = Image.Exif()
    ex[0x010F] = "TestCam"                                 # Make (IFD0)
    ex[0x0132] = when                                      # DateTime (IFD0, 현지 시각) — Pillow 10.2 는
                                                           # get_ifd(0x8769) 수정분을 저장하지 않아 IFD0 사용
    img.save(path, "JPEG", exif=ex)


def _video_with_creation_time(path: Path, when: str):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=6", "-t", "1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-metadata", f"creation_time={when}", str(path)], check=True, capture_output=True)


def test_sort_by_capture_time_endpoint(organize_client, db, outputs):
    p = make_project(db, outputs, n_photos=0, place_at=None)
    assets = outputs / p.id / "assets"
    _photo(assets / "place.png")
    _photo(assets / "unknown_101010123456.jpg")                                    # EXIF 없음
    _exif_jpeg(assets / "nothing_101010123456.jpg", "2026:10:25 09:00:00")         # = 00:00Z
    _video_with_creation_time(assets / "clip_101010123456.mp4", "2026-10-25T00:30:00Z")
    specs = [  # (이름, 종류, 파일, label, exif)   — 원래 순서
        ("place", "place", "place.png", "일월수목원", {}),
        ("utc", "image", "late.jpg", "late.jpg", {"captured_utc": "2026-10-25T03:00:00Z"}),     # 03:00Z
        ("unknown", "image", "unknown_101010123456.jpg", "unknown.jpg", {}),                    # → utc 뒤
        ("vid_at", "video", "v2.mp4", "v2.mp4", {"captured_at": "2026-10-25T01:00:00"}),        # 영상 = UTC
        ("name", "image", "IMG_20261025_103000_101010123456.jpg", "IMG_20261025_103000.jpg", {}),  # 01:30Z
        ("exif", "image", "nothing_101010123456.jpg", "nothing.jpg", {}),                       # 00:00Z
        ("probe", "video", "clip_101010123456.mp4", "clip.mp4", {}),                            # 00:30Z
    ]
    ids = {}
    for i, (key, kind, fname, label, exif) in enumerate(specs):
        s = Slide(project_id=p.id, order_index=i, slide_type=kind, label=label, exif=json.dumps(exif),
                  image_filename="" if kind == "video" else fname,
                  video_filename=fname if kind == "video" else "")
        db.add(s)
        db.flush()
        ids[s.id] = key
    db.commit()

    r = organize_client.post(f"/api/v1/projects/{p.id}/slides/sort-by-capture-time")
    assert r.status_code == 200, r.text
    body = r.json()
    want = ["place", "exif", "probe", "vid_at", "name", "utc", "unknown"]
    assert [ids[i] for i in body["order"]] == want
    assert body["changed"] is True and (body["with_time"], body["without_time"]) == (5, 2)

    db.expire_all()
    sl = _slides(db, p.id)
    assert [ids[s.id] for s in sl] == want and [s.order_index for s in sl] == list(range(7))

    again = organize_client.post(f"/api/v1/projects/{p.id}/slides/sort-by-capture-time").json()
    assert again["changed"] is False and [ids[i] for i in again["order"]] == want

    assert organize_client.post("/api/v1/projects/nope/slides/sort-by-capture-time").status_code == 404


def test_sort_by_capture_time_all_unknown_keeps_order(organize_client, db, outputs):
    p = make_project(db, outputs, n_photos=0, place_at=None)
    for i in range(3):
        db.add(Slide(project_id=p.id, order_index=i, slide_type="image", image_filename=f"x{i}.jpg",
                     label=f"x{i}.jpg", exif="{}"))
    db.commit()
    before = [s.id for s in _slides(db, p.id)]
    body = organize_client.post(f"/api/v1/projects/{p.id}/slides/sort-by-capture-time").json()
    assert body["changed"] is False and body["order"] == before and body["with_time"] == 0
