"""
DocuMotion - 역지오코딩 + 지명 라벨 (자동 편집 위치 카드용)

EXIF GPS 좌표 → 사람이 읽는 지명(POI / 동네, 도시, 도, 나라).

- 1순위: 카카오 Local coord2address (+ coord2regioncode 폴백) — KAKAO_REST_API_KEY 있을 때
- 폴백 : Nominatim /reverse (map_service 와 같은 1 req/s 간격, User-Agent, accept-language=ko)
- 결과는 OUTPUTS_DIR/_geocache.json 에 좌표(소수점 4자리 ≈ 11m) 단위로 캐시
- 어떤 실패든 None — 위치 카드만 생략되고 파이프라인은 계속된다.

반환 형태:
  {"name": "일월수목원", "city": "수원", "region": "경기도",
   "country": "대한민국", "provider": "kakao"|"nominatim"}
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Optional

import httpx

from backend.core import config
from backend.core.logger import get_logger
from backend.services import map_service

logger = get_logger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE_NAME = "_geocache.json"
_TIMEOUT = 10.0

# 도시 접미사 (긴 것부터 매칭). "수원시"→"수원", "서울특별시"→"서울"
_CITY_SUFFIXES = ("특별자치시", "특별시", "광역시", "시", "군")
# 광역 단위 (도/특별시/광역시) — 주소 파트에서 region 판별
_REGION_RE = re.compile(r"(특별자치도|특별자치시|특별시|광역시|도)$")
_CITY_RE = re.compile(r"[가-힣]+(시|군)$")
_HANGUL_RE = re.compile(r"[가-힣]")
# Nominatim address 에서 POI 이름으로 쓸 키 (우선순위 순)
_POI_KEYS = ("tourism", "leisure", "amenity", "attraction", "historic", "park",
             "building", "shop", "natural")
_AREA_KEYS = ("neighbourhood", "quarter", "suburb", "village", "hamlet", "city_district")


# ────────────────────────────────────────────────────────────────────────── #
# 지명 헬퍼
# ────────────────────────────────────────────────────────────────────────── #
def city_short(name: str) -> str:
    """도시 이름 축약: '수원시'→'수원', '서울특별시'→'서울', '양평군'→'양평'.
    한글이 아니거나(예: 'Suwon-si') 축약하면 한 글자가 되는 경우는 그대로 둔다."""
    name = (name or "").strip()
    if not name or not _HANGUL_RE.search(name):
        return name
    # "수원시 장안구" 처럼 구까지 붙은 값은 첫 토큰만
    name = name.split()[0]
    for suf in _CITY_SUFFIXES:
        if name.endswith(suf) and len(name) - len(suf) >= 2:
            return name[: -len(suf)]
    return name


def parse_address_parts(address: str) -> dict:
    """주소 문자열에서 city / region / country 추출 (best-effort).
    - Nominatim 순서(작은→큰): '일월수목원, 장안구, 수원시, 경기도, 16416, 대한민국'
    - 카카오 순서(큰→작은): '경기 수원시 장안구 일월로 61'
    반환: {"city": "수원시"|"", "region": "경기도"|"", "country": "대한민국"|""}"""
    out = {"city": "", "region": "", "country": ""}
    if not address:
        return out
    parts = [p.strip() for p in re.split(r"[,\s]+", address) if p.strip()]
    for p in parts:
        if p.isdigit():
            continue
        if not out["country"] and p in ("대한민국", "한국", "South Korea", "Korea"):
            out["country"] = p
            continue
        if not out["city"] and _CITY_RE.fullmatch(p) and not p.endswith("구"):
            out["city"] = p
            continue
        if not out["region"] and _REGION_RE.search(p) and _HANGUL_RE.search(p) and len(p) >= 3:
            out["region"] = p
        elif not out["region"] and p in _KAKAO_REGION_FULL:
            out["region"] = _KAKAO_REGION_FULL[p]
    # 특별시/광역시는 도시이자 광역 단위
    if not out["city"] and re.search(r"(특별시|광역시|특별자치시)$", out["region"]):
        out["city"] = out["region"]
    return out


# ────────────────────────────────────────────────────────────────────────── #
# 캐시
# ────────────────────────────────────────────────────────────────────────── #
def _cache_path() -> Path:
    return Path(config.OUTPUTS_DIR) / _CACHE_NAME


def _cache_key(lat: float, lng: float) -> str:
    return f"{round(float(lat), 4):.4f},{round(float(lng), 4):.4f}"


def _cache_load() -> dict:
    p = _cache_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"geocache 읽기 실패(무시): {e}")
    return {}


def _cache_put(key: str, value: dict) -> None:
    with _CACHE_LOCK:
        data = _cache_load()
        data[key] = value
        p = _cache_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)
        except Exception as e:
            logger.warning(f"geocache 쓰기 실패(무시): {e}")


# ────────────────────────────────────────────────────────────────────────── #
# Provider 파서 (응답 JSON → 표준 dict). 테스트에서 직접 검증한다.
# ────────────────────────────────────────────────────────────────────────── #
def parse_kakao_coord2address(data: dict) -> Optional[dict]:
    docs = (data or {}).get("documents") or []
    if not docs:
        return None
    d = docs[0]
    road = d.get("road_address") or {}
    addr = d.get("address") or {}
    base = addr or road
    if not base:
        return None
    region2 = base.get("region_2depth_name") or ""
    name = (road.get("building_name") or "").strip() or (base.get("region_3depth_name") or "").strip()
    return {
        "name": name,
        "city": city_short(region2),
        "region": _expand_kakao_region(base.get("region_1depth_name") or ""),
        "country": "대한민국",
        "provider": "kakao",
    }


def parse_kakao_regioncode(data: dict) -> Optional[dict]:
    docs = (data or {}).get("documents") or []
    if not docs:
        return None
    # 행정동(H) 우선, 없으면 법정동(B)
    d = next((x for x in docs if x.get("region_type") == "H"), docs[0])
    return {
        "name": (d.get("region_3depth_name") or "").strip(),
        "city": city_short(d.get("region_2depth_name") or ""),
        "region": _expand_kakao_region(d.get("region_1depth_name") or ""),
        "country": "대한민국",
        "provider": "kakao",
    }


# 카카오 coord2address 는 '경기', '서울' 처럼 축약된 광역명을 준다
_KAKAO_REGION_FULL = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}


def _expand_kakao_region(name: str) -> str:
    name = (name or "").strip()
    return _KAKAO_REGION_FULL.get(name, name)


def parse_nominatim_reverse(data: dict) -> Optional[dict]:
    if not isinstance(data, dict) or data.get("error"):
        return None
    a = data.get("address") or {}
    if not a and not data.get("display_name"):
        return None
    name = (data.get("name") or "").strip()
    if not name:
        name = next((a[k] for k in _POI_KEYS if a.get(k)), "")
    if not name:
        name = next((a[k] for k in _AREA_KEYS if a.get(k)), "")
    city = a.get("city") or a.get("town") or a.get("county") or a.get("municipality") or ""
    region = a.get("province") or a.get("state") or ""
    if not city:
        city = parse_address_parts(data.get("display_name", "")).get("city", "")
    return {
        "name": name.strip(),
        "city": city_short(city),
        "region": region.strip(),
        "country": (a.get("country") or "").strip(),
        "provider": "nominatim",
    }


# ────────────────────────────────────────────────────────────────────────── #
# Provider 호출
# ────────────────────────────────────────────────────────────────────────── #
def _kakao_reverse(lat: float, lng: float) -> Optional[dict]:
    if not config.KAKAO_REST_API_KEY:
        return None
    headers = {"Authorization": f"KakaoAK {config.KAKAO_REST_API_KEY}"}
    params = {"x": lng, "y": lat}    # 카카오: x=경도, y=위도
    for endpoint, parser in (("geo/coord2address.json", parse_kakao_coord2address),
                             ("geo/coord2regioncode.json", parse_kakao_regioncode)):
        try:
            r = httpx.get(f"{config.KAKAO_LOCAL_BASE}/{endpoint}", params=params,
                          headers=headers, timeout=_TIMEOUT)
            if r.status_code in (401, 403):
                logger.warning(f"카카오 역지오코딩 인증 오류({r.status_code}) — Nominatim 폴백")
                return None
            r.raise_for_status()
            hit = parser(r.json())
            if hit and (hit.get("name") or hit.get("city")):
                return hit
        except Exception as e:
            logger.warning(f"카카오 역지오코딩 실패({endpoint}): {e}")
    return None


def _nominatim_reverse(lat: float, lng: float) -> Optional[dict]:
    map_service._nominatim_sleep()
    try:
        r = httpx.get(
            f"{config.NOMINATIM_BASE}/reverse",
            params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 17,
                    "addressdetails": 1, "accept-language": "ko"},
            headers={"User-Agent": config.MAP_USER_AGENT, "Accept-Language": "ko,en"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return parse_nominatim_reverse(r.json())
    except Exception as e:
        logger.warning(f"Nominatim 역지오코딩 실패: {e}")
        return None


def reverse_geocode(lat, lng, use_cache: bool = True) -> Optional[dict]:
    """좌표 → 지명 dict. 실패 시 None (예외를 던지지 않는다)."""
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        key = _cache_key(lat, lng)
        if use_cache:
            hit = _cache_load().get(key)
            if isinstance(hit, dict):
                return hit
        result = _kakao_reverse(lat, lng) or _nominatim_reverse(lat, lng)
        if result and use_cache:
            _cache_put(key, result)
        return result
    except Exception as e:
        logger.warning(f"reverse_geocode 실패({lat},{lng}): {e}")
        return None
