"""
DocuMotion - 지도 서비스 (Route/Place 슬라이드용)

장소 검색은 카카오 Local API 를 1순위로, OSM 계열 API 를 폴백으로 사용하고,
staticmap 라이브러리로 OSM 타일 위에 경로/마커를 그려 PNG 로 출력한다.

- Geocoding 1순위: 카카오 Local API (https://dapi.kakao.com/v2/local) — 한국 POI 강점
- Geocoding 폴백 : Nominatim (https://nominatim.openstreetmap.org) — 해외/키 미입력 시
- Routing    : OSRM      (https://router.project-osrm.org)
- POI 상세   : 카카오 키워드 결과 우선, 폴백 시 Overpass API
- 지도 타일  : OpenStreetMap standard tiles (https://tile.openstreetmap.org)

참고:
  - 카카오: REST API 키 필요(KAKAO_REST_API_KEY). 키 없으면 자동으로 Nominatim 폴백.
  - Nominatim 정책상 User-Agent 필수, 1 req/s 권장.
  - OSRM 퍼블릭 서버는 rate-limit 이 빡빡함 → OSRM_BASE 환경변수로 self-hosted 전환 가능.
"""
from __future__ import annotations

import math
import re
import tempfile
import time
from pathlib import Path
from typing import List, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont
from staticmap import StaticMap, Line, CircleMarker, IconMarker

from backend.core.config import (
    KAKAO_REST_API_KEY, KAKAO_LOCAL_BASE,
    NOMINATIM_BASE, OSRM_BASE, OVERPASS_BASE, MAP_USER_AGENT, FONT_PATH,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

# OSM standard raster tiles. staticmap 이 {z}/{x}/{y} 로 치환.
_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Nominatim 호출 간 최소 간격(초) — 사용량 정책 준수
_NOMINATIM_MIN_INTERVAL = 1.0
_last_nominatim_ts = 0.0

# 한국 영역 bbox (여유 있게) — 카카오 결과 vs Nominatim 결과 위치 불일치로
# 해외 의도를 감지할 때 사용. 제주/울릉도 포함.
_KOREA_BBOX = (33.0, 39.5, 124.0, 132.0)   # (lat_min, lat_max, lng_min, lng_max)

# 경로 프레임 색상
_ROUTE_BG_COLOR   = "#7aa6d6"   # 전체 경로(옅은 파랑)
_ROUTE_FG_COLOR   = "#e8473c"   # 이동한 구간(진한 빨강)
_ORIGIN_COLOR     = "#22b14c"   # 출발(초록)
_CURRENT_COLOR    = "#e8473c"   # 현재 위치(빨강)
_DEST_COLOR       = "#1f49b8"   # 도착(파랑)

# 핀 아이콘 PNG 캐시 디렉토리 — (color, glyph) 조합별 1회 생성 후 재사용
_PIN_CACHE_DIR = Path(tempfile.gettempdir()) / "documotion_pins"
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


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


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """font.ttf 를 캐싱하여 반환. 렌더링(라벨/핀 글자)용."""
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.truetype(FONT_PATH, size)
        except Exception:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def _pin_icon(color: str, glyph: str = "", size: int = 56) -> str:
    """
    tear-drop 핀 아이콘 PNG 생성(또는 캐시 조회). 파일 경로 반환.
    끝점(tip)이 bottom-center에 있어 IconMarker offset=(size//2, 0) 으로 좌표에 anchor.
    color: hex (#RRGGBB). glyph: 중앙에 그릴 짧은 문자/기호(옵션).
    """
    _PIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", color.lower())
    fp = _PIN_CACHE_DIR / f"pin_{safe}_{hash(glyph)}_{size}.png"
    if fp.exists():
        return str(fp)

    # 캔버스: 정사각 + 여백(안티앨리어싱/그림자용)
    pad = size // 6
    W = size + pad * 2
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = W // 2
    r = size // 2
    cy = r + pad  # 원 중심

    # 그림자
    shadow = (0, 0, 0, 70)
    d.ellipse([cx - r + 3, cy - r + 5, cx + r + 3, cy + r + 5], fill=shadow)
    d.polygon([(cx - r // 2 + 3, cy + r // 4 + 5), (cx + r // 2 + 3, cy + r // 4 + 5), (cx + 3, W - pad + 5)], fill=shadow)

    # 꼬리(삼각) + 원 → tear-drop
    d.polygon([(cx - r // 2, cy + r // 4), (cx + r // 2, cy + r // 4), (cx, W - pad)], fill=color)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    # 흰 내부원
    ir = int(r * 0.62)
    d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=(255, 255, 255, 255))

    # 중앙 글자/기호
    if glyph:
        fs = max(12, int(r * 0.95))
        f = _get_font(fs)
        try:
            bbox = d.textbbox((0, 0), glyph, font=f)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw = th = fs
        d.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), glyph, font=f, fill=color)

    img.save(str(fp))
    return str(fp)


def _draw_info_pill(img: Image.Image, profile: str, distance_m: float, duration_s: float) -> None:
    """
    이미지 상단 중앙에 소요시간/거리 알약 라벨을 그린다(픽셀 공간, 투영 불필요).
    예: "🚗 약 25분 · 10.4km"
    """
    icon = {"driving": "🚗", "foot": "🚶", "walking": "🚶", "bicycle": "🚲", "bike": "🚲"}.get(profile, "🧭")
    mins = max(1, round(duration_s / 60.0)) if duration_s > 0 else 0
    dist_km = distance_m / 1000.0
    text = f"{icon} 약 {mins}분 · {dist_km:.1f}km" if mins else f"{icon} {dist_km:.1f}km"

    fs = max(18, img.width // 30)
    f = _get_font(fs)
    d = ImageDraw.Draw(img, "RGBA")
    try:
        bbox = d.textbbox((0, 0), text, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = fs * len(text) // 2, fs
    pad_x, pad_y = fs // 2, fs // 3
    box_w, box_h = tw + pad_x * 2, th + pad_y * 2
    x = (img.width - box_w) // 2
    y = fs // 2

    # 알약 배경 (반투명 검정 + 둥근 모서리)
    d.rounded_rectangle([x, y, x + box_w, y + box_h], radius=box_h // 2, fill=(0, 0, 0, 160))
    d.text((x + pad_x - bbox[0], y + pad_y - bbox[1] - th // 6), text, font=f, fill=(255, 255, 255, 255))


def _kakao_search(query: str) -> dict | None:
    """
    카카오 Local 키워드 검색 → 첫 결과를 표준 형태로 반환.
    키 미설정/네트워크 오류/결과 없음 → None (호출자가 폴백).
    반환: {lat, lng, name, display_name, address, category, phone, place_url}
    """
    if not KAKAO_REST_API_KEY:
        return None

    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        r = httpx.get(
            f"{KAKAO_LOCAL_BASE}/search/keyword.json",
            params={"query": query, "size": 1},
            headers=headers, timeout=15.0,
        )
        # 401/403 등 인증 오류는 폴백으로(경고 로그)
        if r.status_code in (401, 403):
            logger.warning(f"카카오 API 인증 오류({r.status_code}) — Nominatim 폴백. KAKAO_REST_API_KEY 확인 필요.")
            return None
        r.raise_for_status()
        docs = r.json().get("documents", [])
    except Exception as e:
        logger.warning(f"카카오 키워드 검색 실패(폴백으로 진행): {e}")
        return None

    if not docs:
        return None

    d = docs[0]
    place = d.get("place_name") or query
    addr = d.get("road_address_name") or d.get("address_name") or ""
    cat = d.get("category_group_name") or d.get("category_name") or ""
    return {
        "lat": float(d["y"]),       # 카카오: y=위도
        "lng": float(d["x"]),       # 카카오: x=경도
        "name": place.strip(),
        "display_name": f"{place}, {addr}" if addr else place,
        "address": addr,
        "category": cat,
        "phone": d.get("phone", ""),
        "place_url": d.get("place_url", ""),
    }


def _nominatim_geocode(query: str) -> dict | None:
    """
    Nominatim 지오코딩 → 표준 형태. 실패/결과 없음 → None.
    반환: {lat, lng, name, display_name}
    """
    _nominatim_sleep()
    url = f"{NOMINATIM_BASE}/search"
    params = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    headers = {"User-Agent": MAP_USER_AGENT, "Accept-Language": "ko,en"}

    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Nominatim 지오코딩 네트워크 오류 ({query!r}): {e}")
        return None

    if not data:
        return None
    hit = data[0]
    return {
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "name": (hit.get("name") or hit.get("display_name") or query).strip(),
        "display_name": hit.get("display_name", query),
    }


def _norm(s: str) -> str:
    """비교용 정규화: 공백 제거 + 소문자. 한국어 exact 매칭 판정에 사용."""
    return re.sub(r"\s+", "", (s or "")).lower()


def _is_overseas(lat: float, lng: float) -> bool:
    """한국 bbox 바깥이면 True (해외)."""
    lat_min, lat_max, lng_min, lng_max = _KOREA_BBOX
    return not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max)


def _resolve_location(query: str) -> Tuple[str, dict | None]:
    """
    카카오 우선 지오코딩 + 해외 동명이인 감지.

    카카오는 한국 데이터만 가지므로, 해외 장소("도쿄" 등)를 넣으면 한국 내 동명의
    장소("도쿄쿠러미" 등)를 잘못 반환할 수 있다. 이를 보정:
      1. 카카오 검색. 결과가 없으면 Nominatim 폴백.
      2. 카카오 결과가 쿼리와 정확히 일치(이름)하면 신뢰 → 카카오.
      3. 일치하지 않으면(동명이인 의심) Nominatim 교차 검증.
         Nominatim이 한국 영역 밖(해외) 좌표를 주면 해외 의도로 판정 → Nominatim 채택.
         아니면 한국 장소이므로 카카오(POI 정보 풍부) 유지.

    반환: (provider, hit) — provider ∈ {'kakao','nominatim'}, hit=None 시 미발견.
    hit 은 lat/lng/name/display_name 을 포함(카카오 hit 은 address/category/phone/place_url 도 포함).
    """
    kakao = _kakao_search(query)

    if kakao is not None:
        # 정확히 일치 → 카카오 신뢰 (Nominatim 호출 생략: 속도 + 공용 서버 부하 절감)
        if _norm(kakao["name"]) == _norm(query):
            return "kakao", kakao
        # 동명이인 의심 → Nominatim 교차 검증
        nom = _nominatim_geocode(query)
        if nom is not None and _is_overseas(nom["lat"], nom["lng"]):
            logger.info(
                f"해외 장소 감지: {query!r} → Nominatim({nom['name']!r}, "
                f"{nom['lat']:.4f},{nom['lng']:.4f}) 가 카카오({kakao['name']!r}) 보다 우선"
            )
            return "nominatim", nom
        return "kakao", kakao

    # 카카오 미발견(키 없음/해외) → Nominatim
    nom = _nominatim_geocode(query)
    if nom is not None:
        return "nominatim", nom
    return None, None


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
    """
    자유 텍스트 → {lat, lng, name, display_name}. 실패 시 ValueError.

    _resolve_location() 으로 카카오 우선 + 해외 동명이인 보정 후 Nominatim 폴백.
    공개 계약은 lat/lng/name/display_name.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("빈 검색어")

    provider, hit = _resolve_location(query)
    if hit is None:
        raise ValueError(f"장소를 찾을 수 없습니다: {query}")

    logger.info(f"Geocode ({provider}): {query!r} -> ({hit['lat']:.5f}, {hit['lng']:.5f}) {hit['name']!r}")
    return {
        "lat": hit["lat"],
        "lng": hit["lng"],
        "name": hit["name"],
        "display_name": hit["display_name"],
    }


def geocode_verbose(query: str) -> dict:
    """
    사전 확인용 geocode — provider/overseas 포함해 상세 반환.
    반환: {provider, lat, lng, name, display_name, overseas}. 실패 시 ValueError.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("빈 검색어")
    provider, hit = _resolve_location(query)
    if hit is None:
        raise ValueError(f"장소를 찾을 수 없습니다: {query}")
    return {
        "provider": provider,
        "lat": hit["lat"],
        "lng": hit["lng"],
        "name": hit["name"],
        "display_name": hit["display_name"],
        "overseas": _is_overseas(hit["lat"], hit["lng"]),
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


def _overpass_enrich(lat: float, lng: float, result: dict) -> dict:
    """
    Overpass 로 (lat,lng) 반경 30m POI 태그를 보강 → result 의 category/opening_hours/name 갱신.
    실패 시 result 를 그대로 반환(graceful degrade).
    """
    q = (
        "[out:json][timeout:15];"
        f"node(around:30,{lat},{lng})[\"name\"];"
        f"way(around:30,{lat},{lng})[\"name\"];"
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


def get_place_details(query: str) -> dict:
    """
    장소 좌표 + POI 상세 조회.
    반환: {name, address, lat, lng, category, opening_hours}

    _resolve_location() 결과에 따라:
      - 카카오: POI 상세(주소/카테고리/전화)를 한 번에 반환.
      - Nominatim(해외/키 미입력/동명이인 보정): 좌표/주소 + Overpass POI 보강.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("빈 검색어")

    provider, hit = _resolve_location(query)
    if hit is None:
        raise ValueError(f"장소를 찾을 수 없습니다: {query}")

    if provider == "kakao":
        logger.info(f"Place details (kakao): {query!r} -> {hit['name']!r} [{hit.get('category')}]")
        return {
            "name": hit["name"],
            "address": hit.get("address") or hit["display_name"],
            "lat": hit["lat"],
            "lng": hit["lng"],
            "category": hit.get("category", ""),
            "opening_hours": "",   # 카카오는 영업시간 미제공
            "phone": hit.get("phone", ""),
        }

    # Nominatim (주로 해외) → Overpass POI 보강
    result = {
        "name": hit["name"],
        "address": hit["display_name"],
        "lat": hit["lat"],
        "lng": hit["lng"],
        "category": "",
        "opening_hours": "",
    }
    result = _overpass_enrich(hit["lat"], hit["lng"], result)
    logger.info(f"Place details (nominatim+overpass): {query!r} -> {result['name']!r}")
    return result


# ────────────────────────────────────────────────────────────────────────── #
# Rendering
# ────────────────────────────────────────────────────────────────────────── #
def render_place_map(lat: float, lng: float, canvas: Tuple[int, int],
                     out_path: Path) -> str:
    """
    단일 정적 지도(줌 16) + 중앙 핀 마커를 PNG 로 저장. out_path 의 파일명 반환.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    m = StaticMap(canvas[0], canvas[1], url_template=_TILE_URL,
                  tile_request_timeout=20, headers={"User-Agent": MAP_USER_AGENT})
    pin_size = max(40, min(canvas[0], canvas[1]) // 12)
    icon = _pin_icon(_CURRENT_COLOR, "★", pin_size)
    m.add_marker(IconMarker((lng, lat), icon, pin_size // 2, 0))
    img = m.render(zoom=16)
    img.save(str(out_path))
    return out_path.name


def render_route_frames(geometry_lnglat: List[Tuple[float, float]],
                        n_frames: int, canvas: Tuple[int, int],
                        out_dir: Path, slide_id: str,
                        distance_m: float = 0.0, duration_s: float = 0.0,
                        profile: str = "driving") -> List[str]:
    """
    경로 애니메이션의 N 개 프레임을 PNG 로 저장.

    각 프레임:
      - 전체 경로(옅은 파랑 폴리라인) — bounds 를 모든 프레임에서 동일하게 유지
      - 진행도까지의 이동 구간(진한 빨강)
      - 출발(초록 핀) / 현재(빨강 점) / 도착(파랑 핀) 마커
      - 상단에 소요시간/거리 알약 라벨

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

    # 핀 아이콘 (캐시됨)
    pin_size = max(40, min(canvas[0], canvas[1]) // 14)
    origin_icon = _pin_icon(_ORIGIN_COLOR, "출", pin_size)
    dest_icon = _pin_icon(_DEST_COLOR, "도", pin_size)

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
        # 마커: 출발(핀) / 도착(핀) — IconMarker, 끝점 anchor=(size//2, 0)
        o_lng, o_lat = full_line[0]
        d_lng, d_lat = full_line[-1]
        m.add_marker(IconMarker((o_lng, o_lat), origin_icon, pin_size // 2, 0))
        m.add_marker(IconMarker((d_lng, d_lat), dest_icon, pin_size // 2, 0))
        # 현재 위치 — 작은 강조 점(마지막 프레임에선 도착과 겹치므로 생략)
        if idx != len(frames_meta) - 1:
            m.add_marker(CircleMarker((cur_lng, cur_lat), _CURRENT_COLOR, 9))

        img = m.render()
        # 상단 소요시간/거리 라벨
        _draw_info_pill(img, profile, distance_m, duration_s)
        fname = f"{slide_id}_frame_{idx:03d}.png"
        img.save(str(out_dir / fname))
        filenames.append(fname)

    return filenames
