"""
DocuMotion - 촬영 시각 정렬 키 (순수 함수 · 표준 라이브러리만)

  · parse_iso_utc     : RFC3339/ISO 문자열 → UTC datetime ('Z', '+09:00', 소수 초 자릿수 무관, 빈 값/쓰레기 → None)
  · time_from_filename: 파일명 패턴 → UTC datetime
        20260923_190207 · IMG_20260923_190207 · PXL_20260923_190207123 · VID_20260923_190207 ·
        Screenshot_2026-09-23-19-02-07 · 2026-09-23 19.02.07 · 13자리 epoch ms · 10자리 epoch s
  · stored_capture_key: Slide.exif JSON → UTC datetime
  · sort_by_capture   : 촬영 시각순 정렬 (시각 모르는 항목 처리 규칙은 아래)

시간대: 파일명·EXIF 의 시각에는 시간대가 없다 → 한국 시간 LOCAL_TZ(Asia/Seoul, UTC+9)로 본다.
  zoneinfo 대신 고정 오프셋 — 한국은 1988년 이후 서머타임이 없어 결과가 같고, tzdata 없는 환경에서도 동작한다.
  반대로 ffprobe creation_time 은 UTC 라서 media_import.ffprobe 가 UTC 기준 naive 로 저장한다
  (영상 슬라이드의 exif.captured_at = UTC, 사진 = 현지 시각).

시각 모르는 항목: 원래(선택) 순서에서 바로 앞의 시각 있는 항목에 붙어 함께 움직인다 (이웃 기준, 상대 순서 유지).
  앞에 시각 있는 항목이 없으면(맨 앞) 맨 앞에 그대로 둔다. 모두 모르면 원래 순서 그대로.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence, TypeVar

T = TypeVar("T")

LOCAL_TZ = timezone(timedelta(hours=9), "KST")   # 시간대 없는 시각(파일명·EXIF)의 기준 — Asia/Seoul

MIN_YEAR, MAX_YEAR = 1900, 2200                  # 이 밖은 쓰레기 값
_EPOCH_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_EPOCH_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)

_ISO_RE = re.compile(
    r"^(\d{4})[-:](\d{2})[-:](\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$")

# 날짜·시각 사이 구분자는 0~1개 (_ - . 공백 T), 초 뒤에 바로 붙은 1~6자리는 소수 초 (PXL_…190207123)
_NAME_DT_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"[ _T.\-]?([01]\d|2[0-3])[-_.:]?([0-5]\d)(?:[-_.:]?([0-5]\d)(\d{1,6})?)?(?!\d)")
_EPOCH_MS_RE = re.compile(r"(?<!\d)(1\d{12})(?!\d)")
_EPOCH_S_RE = re.compile(r"(?<!\d)(1\d{9})(?!\d)")


def _micro(frac: Optional[str]) -> int:
    """소수 초 문자열(자릿수 무관) → 마이크로초 (7자리 이상은 버림)."""
    return int((frac or "")[:6].ljust(6, "0")) if frac else 0


def _plausible(dt: datetime) -> Optional[datetime]:
    return dt if MIN_YEAR <= dt.year <= MAX_YEAR else None


def parse_iso_utc(value, naive_tz: timezone = timezone.utc) -> Optional[datetime]:
    """ISO/RFC3339 문자열(또는 datetime) → UTC aware datetime. 해석 못 하면 None.
    시간대 표기가 없으면 naive_tz 로 본다 (Picker creationTime 은 항상 'Z' 라 기본 UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        m = _ISO_RE.match(str(value or "").strip())
        if not m:
            return None
        y, mo, d, hh, mi, ss, frac, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), _micro(frac))
            if tz and tz not in ("Z", "z"):
                sign = -1 if tz[0] == "-" else 1
                digits = tz[1:].replace(":", "")
                off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
                dt = dt.replace(tzinfo=timezone(sign * off))
            elif tz:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    try:
        return _plausible(dt.astimezone(timezone.utc))
    except (OverflowError, ValueError):
        return None


def time_from_filename(name: str, local_tz: timezone = LOCAL_TZ, allow_epoch: bool = True) -> Optional[datetime]:
    """파일명 속 촬영 시각 → UTC aware datetime. 날짜+시각 패턴(현지 시각 = local_tz, 초 생략 가능
    예: '2026_09_23 18_12') 우선, 없으면 epoch ms/s (allow_epoch=False 면 epoch 은 보지 않음 —
    epoch 이름은 저장/다운로드 시각일 수 있어 EXIF 보다 약한 근거)."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    if not base:
        return None
    for m in _NAME_DT_RE.finditer(base):
        y, mo, d, hh, mi, ss, frac = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), _micro(frac), tzinfo=local_tz)
        except ValueError:                       # 2월 30일 등
            continue
        return dt.astimezone(timezone.utc)
    if not allow_epoch:
        return None
    for rx, scale in ((_EPOCH_MS_RE, 1000.0), (_EPOCH_S_RE, 1.0)):
        for m in rx.finditer(base):
            dt = datetime.fromtimestamp(int(m.group(1)) / scale, tz=timezone.utc)
            if _EPOCH_MIN <= dt < _EPOCH_MAX:
                return dt
    return None


def picked_item_key(item: dict) -> Optional[datetime]:
    """Picker 항목(photos_manager.parse_picked_item) → 촬영 시각: creationTime, 없으면 파일명."""
    return parse_iso_utc(item.get("creationTime")) or time_from_filename(item.get("filename") or "")


def stored_capture_key(exif: dict, is_video: bool = False) -> Optional[datetime]:
    """Slide.exif → 촬영 시각. captured_utc(가져올 때 저장한 UTC) 우선, 없으면 captured_at
    (시간대 없음: 영상 = ffprobe 의 UTC, 사진 = EXIF 현지 시각 LOCAL_TZ)."""
    if not isinstance(exif, dict):
        return None
    return parse_iso_utc(exif.get("captured_utc")) or \
        parse_iso_utc(exif.get("captured_at"), timezone.utc if is_video else LOCAL_TZ)


_IMPORT_STAMP_RE = re.compile(r"_\d{12}(?=(?:_h264)?\.[A-Za-z0-9]+$)")


def strip_import_stamp(saved_name: str) -> str:
    """저장 파일명 '<원본 stem>_<HHMMSSffffff>[_h264].ext' 에서 가져온 시각(촬영 시각 아님)을 뺀다."""
    return _IMPORT_STAMP_RE.sub("", str(saved_name or ""))


def slide_capture_key(exif: dict, is_video: bool, names: Sequence[str] = (),
                      read_file: Optional[Callable[[], Optional[datetime]]] = None) -> Optional[datetime]:
    """슬라이드 촬영 시각: 저장된 값 → 파일명 날짜 패턴(원본 이름, 저장 이름 순) → read_file()(EXIF/ffprobe)
    → 파일명 epoch ms/s (저장 시각일 수 있어 마지막)."""
    k = stored_capture_key(exif, is_video)
    for n in names:
        if k is not None:
            return k
        k = time_from_filename(n, allow_epoch=False)
    if k is None and read_file is not None:
        k = read_file()
    for n in names:
        if k is not None:
            break
        k = time_from_filename(n)
    return k


def to_utc_iso(dt: datetime) -> str:
    """UTC datetime → '2026-09-23T10:02:07Z' (소수 초는 있을 때만)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def sort_by_capture(items: Sequence[T], key: Callable[[T], Optional[datetime]],
                    reverse: bool = False) -> list[T]:
    """촬영 시각순(기본 오래된 순) 안정 정렬. 같은 시각은 원래 순서.
    시각 모르는 항목은 원래 순서상 바로 앞의 시각 있는 항목 뒤에 붙어 함께 움직이고, 맨 앞이면 맨 앞에 남는다."""
    lead: list[T] = []
    blocks: list[tuple[datetime, list[T]]] = []
    for it in items:
        k = key(it)
        if k is None:
            (blocks[-1][1] if blocks else lead).append(it)
        else:
            blocks.append((k, [it]))
    out = list(lead)
    for _, group in sorted(blocks, key=lambda b: b[0], reverse=reverse):   # sorted 는 reverse 여도 안정
        out.extend(group)
    return out
