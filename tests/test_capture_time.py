"""촬영 시각 정렬 키 (backend/services/capture_time.py) + Picker 목록 정렬 (photos_manager.list_picked_items).

표준 라이브러리만 쓰는 순수 함수 — DB·ffmpeg·네트워크 없음."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services import capture_time as ct
from backend.services import photos_manager

UTC = timezone.utc


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────── #
# 1. creationTime (ISO / RFC3339)
# ────────────────────────────────────────────────────────────────────────── #
@pytest.mark.parametrize("raw, want", [
    ("2026-09-23T10:02:07Z", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T10:02:07z", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T19:02:07+09:00", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T19:02:07+0900", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T05:02:07-05:00", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T10:02:07.5Z", utc(2026, 9, 23, 10, 2, 7, 500000)),
    ("2026-09-23T10:02:07.123Z", utc(2026, 9, 23, 10, 2, 7, 123000)),
    ("2026-09-23T10:02:07.123456789Z", utc(2026, 9, 23, 10, 2, 7, 123456)),   # 9자리 → 버림
    ("  2026-09-23 10:02:07Z ", utc(2026, 9, 23, 10, 2, 7)),
    ("2026-09-23T10:02:07", utc(2026, 9, 23, 10, 2, 7)),                       # 시간대 없음 → 기본 UTC
    ("2026-09-23", utc(2026, 9, 23)),
])
def test_parse_iso_utc(raw, want):
    got = ct.parse_iso_utc(raw)
    assert got == want and got.tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "   ", "garbage", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z",
                                 "2026-09-23T25:00:00Z", "0001-01-01T00:00:00+09:00", "1601-01-01T00:00:00Z",
                                 "2026-09-23T10:02:07Zjunk", 12345])
def test_parse_iso_utc_missing_or_garbage(raw):
    assert ct.parse_iso_utc(raw) is None


def test_parse_iso_naive_tz_and_datetime_input():
    assert ct.parse_iso_utc("2026-09-23T19:02:07", ct.LOCAL_TZ) == utc(2026, 9, 23, 10, 2, 7)
    assert ct.parse_iso_utc(datetime(2026, 9, 23, 19, 2, 7), ct.LOCAL_TZ) == utc(2026, 9, 23, 10, 2, 7)
    # 사진 EXIF 형식 (콜론 날짜)
    assert ct.parse_iso_utc("2026:09:23 19:02:07", ct.LOCAL_TZ) == utc(2026, 9, 23, 10, 2, 7)


# ────────────────────────────────────────────────────────────────────────── #
# 2. 파일명 (현지 시각 = KST)
# ────────────────────────────────────────────────────────────────────────── #
KST_1902 = utc(2026, 9, 23, 10, 2, 7)      # 2026-09-23 19:02:07 KST


@pytest.mark.parametrize("name, want", [
    ("20260923_190207.jpg", KST_1902),
    ("IMG_20260923_190207.jpg", KST_1902),
    ("IMG_20260923_190207~2.jpg", KST_1902),
    ("VID_20260923_190207.mp4", KST_1902),
    ("PXL_20260923_190207123.jpg", utc(2026, 9, 23, 10, 2, 7, 123000)),
    ("PXL_20260923_190207123.MP.jpg", utc(2026, 9, 23, 10, 2, 7, 123000)),
    ("KakaoTalk_20260923_190207123.jpg", utc(2026, 9, 23, 10, 2, 7, 123000)),
    ("Screenshot_2026-09-23-19-02-07.png", KST_1902),
    ("Screenshot_2026-09-23-19-02-07-123_com.app.png", KST_1902),
    ("2026-09-23 19.02.07.jpg", KST_1902),
    ("20260923190207.jpg", KST_1902),
    ("/some/dir/IMG_20260923_190207.jpg", KST_1902),
    ("1790157727000.jpg", utc(2026, 9, 23, 10, 2, 7)),            # epoch ms
    ("received_1790157727123.jpeg", utc(2026, 9, 23, 10, 2, 7, 123000)),
    ("1790157727.jpg", utc(2026, 9, 23, 10, 2, 7)),                # epoch s
])
def test_time_from_filename(name, want):
    assert ct.time_from_filename(name) == want


@pytest.mark.parametrize("name", ["", None, "IMG_0001.JPG", "photo.jpg", "20261323_190207.jpg",
                                  "20260230_120000.jpg", "20260923_250000.jpg", "IMG-20260923-WA0001.jpg",
                                  "0101234567.jpg", "123456789012345.jpg", "9999999999999.jpg"])
def test_time_from_filename_none(name):
    assert ct.time_from_filename(name) is None


def test_strip_import_stamp_keeps_original_name_only():
    assert ct.strip_import_stamp("20260923_190207123456.jpg") == "20260923.jpg"
    assert ct.strip_import_stamp("IMG_1_190207123456_h264.mp4") == "IMG_1_h264.mp4"
    assert ct.strip_import_stamp("IMG_20260923_190207_101010123456.jpg") == "IMG_20260923_190207.jpg"
    # 가져온 시각(HHMMSSffffff)이 날짜만 있는 파일명과 붙어 촬영 시각처럼 읽히지 않는다
    assert ct.time_from_filename(ct.strip_import_stamp("20260923_190207123456.jpg")) is None


def test_stored_capture_key_zone_by_type():
    # 사진 captured_at = EXIF 현지 시각, 영상 captured_at = ffprobe UTC, captured_utc 가 최우선
    assert ct.stored_capture_key({"captured_at": "2026-09-23T19:02:07"}) == KST_1902
    assert ct.stored_capture_key({"captured_at": "2026-09-23T10:02:07"}, is_video=True) == KST_1902
    assert ct.stored_capture_key({"captured_utc": "2026-09-23T10:02:07Z",
                                  "captured_at": "1999-01-01T00:00:00"}) == KST_1902
    assert ct.stored_capture_key({}) is None and ct.stored_capture_key("x") is None
    assert ct.to_utc_iso(KST_1902) == "2026-09-23T10:02:07Z"


def test_slide_capture_key_fallback_chain():
    calls = []

    def read():
        calls.append(1)
        return utc(2020, 1, 1)
    assert ct.slide_capture_key({"captured_at": "2026-09-23T19:02:07"}, False, ["IMG_20000101_000000.jpg"], read) \
        == KST_1902 and not calls
    assert ct.slide_capture_key({}, False, ["x.jpg", "IMG_20260923_190207.jpg"], read) == KST_1902 and not calls
    assert ct.slide_capture_key({}, False, ["x.jpg"], read) == utc(2020, 1, 1) and calls
    assert ct.slide_capture_key({}, False, ["x.jpg"]) is None


# ────────────────────────────────────────────────────────────────────────── #
# 3. 정렬 (시각 모르는 항목: 앞 항목에 붙어 간다 · 맨 앞은 맨 앞 · 같은 시각은 원래 순서)
# ────────────────────────────────────────────────────────────────────────── #
def _sort(rows, reverse=False):
    return [n for n, _ in ct.sort_by_capture(rows, lambda r: r[1], reverse=reverse)]


def test_sort_missing_keys_stay_with_preceding_neighbor():
    rows = [("lead", None), ("c", utc(2026, 1, 3)), ("c+", None), ("c++", None),
            ("a", utc(2026, 1, 1)), ("b", utc(2026, 1, 2)), ("b+", None)]
    assert _sort(rows) == ["lead", "a", "b", "b+", "c", "c+", "c++"]
    assert _sort(rows, reverse=True) == ["lead", "c", "c+", "c++", "b", "b+", "a"]


def test_sort_stable_for_ties_and_all_unknown():
    t = utc(2026, 1, 1)
    assert _sort([("x", t), ("y", t), ("z", t)]) == ["x", "y", "z"]
    assert _sort([("x", t), ("y", t), ("z", t)], reverse=True) == ["x", "y", "z"]
    assert _sort([("x", None), ("y", None), ("z", None)]) == ["x", "y", "z"]
    assert _sort([]) == []


# ────────────────────────────────────────────────────────────────────────── #
# 4. Picker 목록: 사진·영상 섞어서 오래된 순 (기본), 형식이 섞인 creationTime
# ────────────────────────────────────────────────────────────────────────── #
def _item(id_, filename, created, video=False):
    return {"id": id_, "createTime": "", "type": "VIDEO" if video else "PHOTO",
            "mediaFile": {"baseUrl": f"https://x/{id_}", "filename": filename,
                          "mimeType": "video/mp4" if video else "image/jpeg",
                          "mediaFileMetadata": {"creationTime": created}}}


class _Json:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


@pytest.fixture
def picker(monkeypatch):
    state = {"items": []}
    monkeypatch.setattr(photos_manager, "_get_headers", lambda: {})
    monkeypatch.setattr(photos_manager.http_requests, "get",
                        lambda url, **k: _Json({"mediaItems": state["items"]}))
    return state


def test_list_picked_items_mixed_oldest_default(picker):
    # Picker 순서: 최신 먼저, 같은 날은 영상이 사진 위 — 문자열 정렬이면 '+09:00'·소수 초 때문에 틀어진다
    picker["items"] = [
        _item("v2", "VID_2.mp4", "2026-10-25T10:05:00Z", video=True),
        _item("p2", "IMG_2.jpg", "2026-10-25T19:04:00+09:00"),          # = 10:04Z
        _item("pX", "IMG_0009.JPG", ""),                                  # 시각 모름 → p2 뒤에 붙어 간다
        _item("v1", "VID_1.mp4", "2026-10-25T10:01:00.123456789Z", video=True),
        _item("p1", "IMG_1.jpg", "2026-10-25T10:01:00Z"),
        _item("pF", "PXL_20261025_185900000.jpg", "garbage"),            # 파일명 → 09:59Z
    ]
    ids = [i["id"] for i in photos_manager.list_picked_items("s")]
    assert ids == ["pF", "p1", "v1", "p2", "pX", "v2"]
    newest = [i["id"] for i in photos_manager.list_picked_items("s", "newest")]
    assert newest == ["v2", "p2", "pX", "v1", "p1", "pF"]
    api = [i["id"] for i in photos_manager.list_picked_items("s", "api")]
    assert api == ["v2", "p2", "pX", "v1", "p1", "pF"]
