"""
DocuMotion - OSM 지도 서비스 (Route/Place 슬라이드용)

무료 OSM 계열 API 로 경로·장소 데이터를 가져오고,
staticmap 라이브러리로 OSM 타일 위에 경로/마커를 그려 PNG 로 출력한다.

- Geocoding  : Nominatim (https://nominatim.openstreetmap.org)
- Routing    : OSRM      (https://router.project-osrm.org)
- POI 상세   : Overpass API (https://overpass-api.de/api/interpreter)
- 지도 타일  : OpenStreetMap standard tiles (https://tile.openstreetmap.org)

참고:
  - Nominatim 정책상 User-Agent 필수, 1 req/s 권장.
  - OSRM 퍼블릭 서버는 rate-limit 이 빡빡함 → OSRM_BASE 환경변수로 self-hosted 전환 가능.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import List, Tuple

import httpx
from staticmap import StaticMap, Line, CircleMarker

from backend.core.config import (
    NOMINATIM_BASE, OSRM_BASE, OVERPASS_BASE, MAP_USER_AGENT,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

# OSM standard raster tiles. staticmap 이 {z}/{x}/{y} 로 치환.
_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Nominatim 호출 간 최소 간격(초) — 사용량 정책 준수
_NOMINATIM_MIN_INTERVAL = 1.0
_last_nominatim_ts = 0.0

# 경로 프레임 색상
_ROUTE_BG_COLOR   = "#7aa6d6"   # 전체 경로(옅은 파랑)
_ROUTE_FG_COLOR   = "#e8473c"   # 이동한 구간(진한 빨강)
_ORIGIN_COLOR     = "#22b14c"   # 출발(초록)
_CURRENT_COLOR    = "#e8473c"   # 현재 위치(빨강)
_DEST_COLOR       = "#1f49b8"   # 도착(파랑)


# ────────────────────────────────────────────────────────────────────────── #
# Internal helpers
# ────────────────────────────────────────────────────────────────────────── #
def _nominatim_sleep():
    """Nominatim 1 req/s 정책 준수용 대기."""
    global _last_nominatim_ts
    dt = time.monotonic() - _last_nominatim_ts
    if dt < _NOMINATIM_MIN_INTERVAL:
        time.sleep(_NOMINATIM_MIN_INTERVAL - dt)
    _last_nominatim_ts = time.monotonic()


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """두 (lat,lng) 지점 간 거리(m)."""
    R = 6371000.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _interp_along(coords_latlng: List[Tuple[float, float]], frac: float):
    """
    경로(위도/경도 리스트) 위에서 누적 거리 기준 frac(0~1) 지점의
    (lat, lng) 와 그 지점까지의 부분 경로 리스트를 반환.
    coords_latlng: [(lat, lng), ...]  (OSRM geometry 는 [lng,lat] 이므로 호출 전 변환)
    """
    if not coords_latlng:
        return None, []
    if len(coords_latlng) == 1 or frac <= 0:
        return coords_latlng[0], [coords_latlng[0]]
    if frac >= 1:
        return coords_latlng[-1], list(coords_latlng)

    # 누적 거리
    seg = []
    total = 0.0
    for i in range(len(coords_latlng) - 1):
        d = _haversine_m(coords_latlng[i], coords_latlng[i + 1])
        seg.append(d)
        total += d
    if total <= 0:
        return coords_latlng[0], [coords_latlng[0]]

    target = total * frac
    acc = 0.0
    travelled = [coords_latlng[0]]
    for i, d in enumerate(seg):
        if acc + d >= target:
            # 이 세그먼트 안에 정답이 있음
            remain = target - acc
            ratio = remain / d if d > 0 else 0.0
            a = coords_latlng[i]
            b = coords_latlng[i + 1]
            pt = (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)
            travelled.append(pt)
            return pt, travelled
        acc += d
        travelled.append(coords_latlng[i + 1])
    return coords_latlng[-1], travelled


# ────────────────────────────────────────────────────────────────────────── #
# Public API
# ────────────────────────────────────────────────────────────────────────── #
def geocode(query: str) -> dict:
    """자유 텍스트 → {lat, lng, name, display_name}. 실패 시 ValueError."""
    query = (query or "").strip()
    if not query:
        raise ValueError("빈 검색어")

    _nominatim_sleep()
    url = f"{NOMINATIM_BASE}/search"
    params = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    headers = {"User-Agent": MAP_USER_AGENT, "Accept-Language": "ko,en"}

    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Geocode 네트워크 오류 ({query!r}): {e}")
        raise ValueError(f"지오코딩 실패: {e}")

    if not data:
        raise ValueError(f"장소를 찾을 수 없습니다: {query}")

    hit = data[0]
    return {
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "name": (hit.get("name") or hit.get("display_name") or query).strip(),
        "display_name": hit.get("display_name", query),
    }


def get_route(origin: dict, destination: dict, profile: str = "driving") -> dict:
    """
    OSRM 경로 요청.
    origin/destination: geocode() 결과 ({lat, lng, ...}).
    반환: {geometry_lnglat:[[lng,lat],...], distance_m, duration_s, profile}
    실패 시 ValueError.
    """
    profile = profile if profile in ("driving", "foot", "walking", "bike", "bicycle") else "driving"
    coords = f"{origin['lng']},{origin['lat']};{destination['lng']},{destination['lat']}"
    url = f"{OSRM_BASE}/route/v1/{profile}/{coords}"
    params = {"overview": "full", "geometries": "geojson"}

    try:
        r = httpx.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"OSRM 네트워크 오류: {e}")
        raise ValueError(f"경로 요청 실패: {e}")

    code = data.get("code")
    if code != "Ok" or not data.get("routes"):
        msg = data.get("message", "unknown")
        raise ValueError(f"경로를 찾을 수 없습니다 (OSRM: {code} {msg})")

    route = data["routes"][0]
    geom = route.get("geometry", {}).get("coordinates", [])  # [[lng, lat], ...]
    if not geom:
        raise ValueError("경로 geometry 가 비어 있습니다")

    return {
        "geometry_lnglat": geom,
        "distance_m": float(route.get("distance", 0)),
        "duration_s": float(route.get("duration", 0)),
        "profile": profile,
    }


def get_place_details(query: str) -> dict:
    """
    Nominatim 으로 좌표/주소 확보 후, Overpass 로 주변 POI 상세 조회.
    반환: {name, address, lat, lng, category, opening_hours}
    Overpass 실패 시 Nominatim 결과만 반환(gra d eful degrade).
    """
    geo = geocode(query)
    result = {
        "name": geo["name"],
        "address": geo["display_name"],
        "lat": geo["lat"],
        "lng": geo["lng"],
        "category": "",
        "opening_hours": "",
    }

    # Overpass 로 반경 30m POI 보강
    q = (
        "[out:json][timeout:15];"
        f"node(around:30,{geo['lat']},{geo['lng']})[\"name\"];"
        f"way(around:30,{geo['lat']},{geo['lng']})[\"name\"];"
        "out tags 5;"
    )
    try:
        r = httpx.post(OVERPASS_BASE, data={"data": q},
                       headers={"User-Agent": MAP_USER_AGENT}, timeout=20.0)
        r.raise_for_status()
        elements = r.json().get("elements", [])
        # 가장 태그가 풍부한 요소 1개 선택
        best = max(elements, key=lambda e: len(e.get("tags", {})), default=None)
        if best and best.get("tags"):
            tags = best["tags"]
            if tags.get("name") and not result["name"]:
                result["name"] = tags["name"]
            cat = tags.get("amenity") or tags.get("shop") or tags.get("tourism") \
                or tags.get("leisure") or tags.get("office") or tags.get("public_transport") \
                or tags.get("railway") or tags.get("highway") or tags.get("building") \
                or tags.get("historic") or tags.get("natural")
            if cat:
                result["category"] = cat
            if tags.get("opening_hours"):
                result["opening_hours"] = tags["opening_hours"]
    except Exception as e:
        logger.warning(f"Overpass 조회 실패(기본 정보로 진행): {e}")

    return result


# ────────────────────────────────────────────────────────────────────────── #
# Rendering
# ────────────────────────────────────────────────────────────────────────── #
def render_place_map(lat: float, lng: float, canvas: Tuple[int, int],
                     out_path: Path) -> str:
    """
    단일 정적 지도(줌 16) + 중앙 마커를 PNG 로 저장. out_path 의 파일명 반환.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    m = StaticMap(canvas[0], canvas[1], url_template=_TILE_URL,
                  tile_request_timeout=20, headers={"User-Agent": MAP_USER_AGENT})
    m.add_marker(CircleMarker((lng, lat), _CURRENT_COLOR, 14))
    img = m.render(zoom=16)
    img.save(str(out_path))
    return out_path.name


def render_route_frames(geometry_lnglat: List[Tuple[float, float]],
                        n_frames: int, canvas: Tuple[int, int],
                        out_dir: Path, slide_id: str) -> List[str]:
    """
    경로 애니메이션의 N 개 프레임을 PNG 로 저장.

    각 프레임:
      - 전체 경로(옅은 파랑 폴리라인) — bounds 를 모든 프레임에서 동일하게 유지
      - 진행도까지의 이동 구간(진한 빨강)
      - 출발/현재/도착 마커

    geometry_lnglat: [[lng, lat], ...] (OSRM geojson 형식)
    반환: 저장된 파일명 리스트(assets_dir 기준 상대명).
    """
    if not geometry_lnglat:
        raise ValueError("경로 geometry 가 비어 있습니다")
    out_dir.mkdir(parents=True, exist_ok=True)

    # staticmap Line 은 (lng, lat) 튜플 리스트를 받는다
    full_line = [tuple(map(float, c)) for c in geometry_lnglat]
    # 위도/경도 순으로 변환 (내부 거리 계산용)
    latlng = [(c[1], c[0]) for c in full_line]

    # 진행도별 현재 위치 + 이동한 부분 경로
    fracs = [i / max(1, n_frames - 1) for i in range(n_frames)]
    frames_meta = [_interp_along(latlng, f) for f in fracs]

    # 전체 경로 bounds 로 전체 폴리라인을 항상 추가 → 카메라 고정
    filenames: List[str] = []
    for idx, ((cur_lat, cur_lng), travelled_latlng) in enumerate(frames_meta):
        m = StaticMap(canvas[0], canvas[1], url_template=_TILE_URL,
                      tile_request_timeout=20, headers={"User-Agent": MAP_USER_AGENT})
        # 전체 경로(옅은) — bounds 고정용
        m.add_line(Line(full_line, _ROUTE_BG_COLOR, 5))
        # 이동한 구간(진한) — (lng, lat) 로 변환
        if len(travelled_latlng) >= 2:
            travelled_lnglat = [(lng, lat) for (lat, lng) in travelled_latlng]
            m.add_line(Line(travelled_lnglat, _ROUTE_FG_COLOR, 7))
        # 마커: 출발 / 현재 / 도착
        o_lng, o_lat = full_line[0]
        d_lng, d_lat = full_line[-1]
        m.add_marker(CircleMarker((o_lng, o_lat), _ORIGIN_COLOR, 12))
        m.add_marker(CircleMarker((cur_lng, cur_lat), _CURRENT_COLOR, 12))
        if idx != len(frames_meta) - 1:  # 마지막 프레임에선 도착 마커가 현재 마커 겹침 방지
            m.add_marker(CircleMarker((d_lng, d_lat), _DEST_COLOR, 12))
        else:
            m.add_marker(CircleMarker((d_lng, d_lat), _DEST_COLOR, 12))

        img = m.render()
        fname = f"{slide_id}_frame_{idx:03d}.png"
        img.save(str(out_dir / fname))
        filenames.append(fname)

    return filenames
