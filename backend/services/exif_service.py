"""
DocuMotion - EXIF 메타데이터 추출/분석 (Photo-Vlog F3)

사진 vlog 자동 구성의 기반: 업로드 시각에 촬영시각(DateTimeOriginal)과 GPS를
Slide.exif 컬럼에 저장하고, 이를 기반으로 (a) 시간순 정렬 제안, (b) GPS 이동
구간 감지 → 경로 슬라이드 삽입 제안을 제공한다.

모든 EXIF 파싱은 best-effort — 스크린샷/메타데이터 제거 파일은 {} 를 반환하고
기능은 graceful 하게 숨겨진다 (렌더 영향 無).

의존성: Pillow 만 사용 (PIL.Image.Exif / TAGS / GPSTAGS).
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

from backend.core.logger import get_logger

logger = get_logger(__name__)

# DateTimeOriginal(0x9003), DateTime(0x0132) 태그 번호
_DATETIME_TAGS = (36867, 306)
_EXIF_TIME_FMT = "%Y:%m:%d %H:%M:%S"

GROUP_RADIUS_KM = 2.0    # 이 거리 이내 = 같은 장소 그룹
DEFAULT_GAP_KM = 30.0    # 그룹 간 이 거리 이상 이동 = 경로 슬라이드 제안


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _gps_to_decimal(gps: dict) -> Optional[dict]:
    """GPSInfo dict → {"lat": f, "lng": f} 십진수. 불완전하면 None."""
    def dms(key):
        d = gps.get(key)
        if not isinstance(d, (tuple, list)) or len(d) < 3:
            return None
        deg, minutes, sec = (_to_float(x) for x in d[:3])
        if None in (deg, minutes, sec):
            return None
        return deg + minutes / 60.0 + sec / 3600.0

    lat = dms("GPSLatitude")   # 숫자 키(2) 폴백
    lng = dms("GPSLongitude")  # 숫자 키(4) 폴백
    if lat is None:
        lat = dms(2)
    if lng is None:
        lng = dms(4)
    if lat is None or lng is None:
        return None
    # N/E = +, S/W = -
    lat_ref = str(gps.get("GPSLatitudeRef", gps.get(1, "N")))
    lng_ref = str(gps.get("GPSLongitudeRef", gps.get(3, "E")))
    if lat_ref.upper().startswith("S"):
        lat = -lat
    if lng_ref.upper().startswith("W"):
        lng = -lng
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {"lat": round(lat, 6), "lng": round(lng, 6)}


def extract_exif(image_path) -> dict:
    """
    이미지 파일 → {"captured_at": "YYYY-MM-DDTHH:MM:SS"|None, "gps": {lat,lng}|None}.
    EXIF 없음/오류 → 빈 값들이 None 인 dict (호출자는 {} 로 저장해도 무방).
    """
    result: dict = {"captured_at": None, "gps": None}
    try:
        p = Path(image_path)
        if not p.exists() or p.suffix.lower() not in (".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".webp"):
            return result
        with Image.open(p) as img:
            try:
                raw = img.getexif()
                if not raw:
                    return result
            except Exception:
                return result

        # IFD0 태그명 매핑 (숫자 키 → 이름)
        named = {TAGS.get(k, k): v for k, v in raw.items()}

        # 촬영시각: DateTimeOriginal 우선 → DateTime 폴백
        for tag in _DATETIME_TAGS:
            v = raw.get(tag) or named.get("DateTimeOriginal") or named.get("DateTime")
            if isinstance(v, str) and v.strip():
                try:
                    result["captured_at"] = datetime.strptime(v.strip(), _EXIF_TIME_FMT).isoformat()
                    break
                except ValueError:
                    continue

        # GPS: Exif IFD 내부의 GPSInfo — Pillow 버전에 따라 최상위에서 직접
        # 접근이 안 되는 경우가 있어 Exif IFD(0x8769) 경유 폴백을 둔다.
        gps_ifd = {}
        if hasattr(raw, "get_ifd"):
            gps_ifd = raw.get_ifd(0x8825) or raw.get_ifd(0x8769).get_ifd(0x8825)
        if gps_ifd:
            gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
            result["gps"] = _gps_to_decimal(gps)
        return result
    except Exception as e:
        logger.debug(f"EXIF 추출 실패({image_path}): {e}")
        return result


def haversine_km(a: tuple, b: tuple) -> float:
    """(lat,lng) 두 점의 거리 (km)."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def analyze_timeline(photos: list[dict], gap_km: float = DEFAULT_GAP_KM) -> dict:
    """
    사진 타임라인 분석 → 정렬 필요 여부 + 경로 슬라이드 삽입 제안.

    photos: [{slide_id, order_index, captured_at(ISO|None), gps({lat,lng}|None)}, ...]
             order_index 순으로 정렬되어 있다고 가정.
    반환:
      {
        "needs_sort": bool,              # 현재 순서 ≠ 시간순 (시각 있는 사진 2장 이상일 때만)
        "has_time": bool, "has_gps": bool,
        "routes": [                       # 시간순 기준 이동 구간
          {"after_slide_id": str,        # 이 슬라이드 뒤에 삽입 (그룹 마지막 슬라이드)
           "from": {lat,lng}, "to": {lat,lng}, "distance_km": f}
        ]
      }
    """
    has_time = sum(1 for p in photos if p.get("captured_at")) >= 2
    has_gps = any(p.get("gps") for p in photos)

    # (a) 정렬 필요 여부: 시각 있는 사진들의 현재 등장 순이 시간순과 다른가
    needs_sort = False
    if has_time:
        timed = [(idx, p["captured_at"]) for idx, p in enumerate(photos) if p.get("captured_at")]
        times = [t for _, t in timed]
        needs_sort = times != sorted(times)

    # (b) 경로 제안: 시간순(시각 없으면 현재 순서)으로 GPS 인접 그룹핑 → 그룹 간 거리
    routes: list[dict] = []
    if has_gps:
        ordered = sorted(photos, key=lambda p: p.get("captured_at") or "") if has_time else list(photos)
        groups: list[list[dict]] = []
        for p in ordered:
            gps = p.get("gps")
            if not gps:
                continue
            if groups:
                anchor = groups[-1][-1]["gps"]  # 그룹 마지막 사진 위치가 기준
                if haversine_km((anchor["lat"], anchor["lng"]), (gps["lat"], gps["lng"])) < GROUP_RADIUS_KM:
                    groups[-1].append(p)
                    continue
            groups.append([p])
        for g, g_next in zip(groups, groups[1:]):
            a, b = g[-1]["gps"], g_next[0]["gps"]
            dist = haversine_km((a["lat"], a["lng"]), (b["lat"], b["lng"]))
            if dist >= gap_km:
                routes.append({
                    "after_slide_id": g[-1]["slide_id"],
                    "from": a, "to": b,
                    "distance_km": round(dist, 1),
                })

    return {"needs_sort": needs_sort, "has_time": has_time, "has_gps": has_gps, "routes": routes}
