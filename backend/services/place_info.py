"""
DocuMotion - 장소 정보 보강 서비스 (Place 슬라이드용)

네이버 지역검색 API 로 **실제** 장소 데이터(이름/카테고리/한 줄 소개/주소)를 가져오고,
Gemini 로 **개요/특징/추천 포인트/팁** 을 생성한다.

- 네이버: X-Naver-Client-Id / X-Naver-Client-Secret 헤더. 키 없으면 스킵.
- Gemini: GOOGLE_API_KEY 없으면 스킵. JSON 스키마로 구조화 응답.
- 둘 다 실패해도 호출자가 진행 가능하도록 빈 값으로 graceful degrade.

★ 별점/후기는 네이버 무료 API에 없으므로 다루지 않는다 (가짜 데이터 생성 금지).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

import httpx
from PIL import Image

from backend.core.config import (
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_LOCAL_BASE,
    GOOGLE_API_KEY, GEMINI_MODEL,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)


def fetch_naver_local(query: str) -> Optional[dict]:
    """
    네이버 지역검색 → 첫 결과. 키 미설정/오류/결과 없음 → None.
    반환: {title, category, description, address, road_address, link}
    (좌표 mapx/mapy는 KATECH라 WGS84 변환 필요 — 지도는 기존 geocode 결과를 쓰므로 여기선 안 씀)
    """
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return None
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    try:
        r = httpx.get(NAVER_LOCAL_BASE, params={"query": query, "display": 1, "sort": "random"},
                      headers=headers, timeout=15.0)
        if r.status_code != 200:
            logger.warning(f"네이버 지역검색 응답 {r.status_code}: {r.text[:120]}")
            return None
        items = r.json().get("items", [])
        if not items:
            return None
        it = items[0]
        # title은 <b>태그가 섞여 올 수 있어 제거
        title = (it.get("title") or "").replace("<b>", "").replace("</b>", "").strip()
        return {
            "title": title,
            "category": (it.get("category") or "").strip(),
            "description": (it.get("description") or "").strip(),
            "address": (it.get("address") or "").strip(),
            "road_address": (it.get("roadAddress") or "").strip(),
            "link": (it.get("link") or "").strip(),
        }
    except Exception as e:
        logger.warning(f"네이버 지역검색 실패(스킵): {e}")
        return None


_GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string", "description": "장소 개요. 2~3문장."},
        "features": {
            "type": "array", "items": {"type": "string"},
            "description": "장소의 특징/볼거리. 짧은 항목 3~4개.",
        },
        "highlights": {
            "type": "array", "items": {"type": "string"},
            "description": "추천 포인트/대표 메뉴/체험. 짧은 항목 3~4개.",
        },
        "tip": {"type": "string", "description": "방문 팁 1문장."},
    },
    "required": ["overview", "features", "highlights", "tip"],
}


def generate_description(query: str, naver: Optional[dict], geo: dict) -> dict:
    """
    Gemini 로 장소 설명 생성 → {overview, features[], highlights[], tip}.
    API 키 없거나 실패 시 최소값 반환(네이버 한 줄 소개를 overview로).
    """
    if not GOOGLE_API_KEY:
        return _fallback_desc(naver)

    facts = {
        "이름": geo.get("name") or (naver or {}).get("title") or query,
        "주소": (naver or {}).get("road_address") or (naver or {}).get("address") or geo.get("display_name", ""),
        "카테고리": (naver or {}).get("category") or "",
        "네이버_한줄소개": (naver or {}).get("description") or "",
        "좌표": f"{geo.get('lat'):.5f}, {geo.get('lng'):.5f}",
    }
    prompt = (
        "아래 장소 정보를 바탕으로 영상용 장소 소개패널에 들어갈 내용을 한국어로 작성.\n"
        "사실 정보(이름/주소/카테고리/한줄소개)를 기반으로 하되, 모르는 구체적 사실(메뉴/가격/영업시간 등)은 지어내지 말 것. "
        "일반적이고 안전한 설명 위주로.\n\n"
        f"장소 정보:\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
        "overview는 2~3문장 개요. features/highlights는 각각 짧은 구(명사구) 3~4개. tip은 방문 팁 1문장."
    )
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": _GEMINI_SCHEMA,
                                     "temperature": 0.7},
            },
            timeout=25.0,
        )
        if r.status_code != 200:
            logger.warning(f"Gemini 장소 설명 응답 {r.status_code}: {r.text[:160]}")
            return _fallback_desc(naver)
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        return {
            "overview": str(data.get("overview", "")).strip(),
            "features": [str(x).strip() for x in data.get("features", []) if str(x).strip()][:5],
            "highlights": [str(x).strip() for x in data.get("highlights", []) if str(x).strip()][:5],
            "tip": str(data.get("tip", "")).strip(),
        }
    except Exception as e:
        logger.warning(f"Gemini 장소 설명 생성 실패(폴백): {e}")
        return _fallback_desc(naver)


def _fallback_desc(naver: Optional[dict]) -> dict:
    """Gemini 없을 때 최소 설명 (네이버 한 줄 소개 활용)."""
    overview = (naver or {}).get("description") or "이 장소에 대한 정보를 표시합니다."
    return {"overview": overview, "features": [], "highlights": [], "tip": ""}


# ──────────────────────────────────────────────────────────────────────────
# 장소 대표 사진
# ──────────────────────────────────────────────────────────────────────────
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # 15MB


def _download_image(url: str, referer: Optional[str] = None) -> Optional[str]:
    """URL 이미지를 임시 파일로 저장(유효성 검증). 실패 시 None."""
    headers = {"User-Agent": "Mozilla/5.0 DocuMotionStudio/1.0"}
    if referer:
        headers["Referer"] = referer
    try:
        r = httpx.get(url, headers=headers, timeout=20.0, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 1024:
            return None
        if len(r.content) > _MAX_PHOTO_BYTES:
            return None
        ct = r.headers.get("content-type", "").lower()
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        elif "gif" in ct:
            ext = ".gif"
        elif "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        else:
            ext = ".bin"
        tf = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tf.write(r.content)
        tf.close()
        # 유효한 이미지인지 검증
        try:
            with Image.open(tf.name) as im:
                im.verify()
        except Exception:
            os.unlink(tf.name)
            return None
        # webp/gif → jpg 로 통일(Pillow save 용이)
        if ext in (".webp", ".gif", ".bin"):
            try:
                with Image.open(tf.name) as im:
                    im.convert("RGB").save(tf.name + ".jpg", "JPEG")
                os.unlink(tf.name)
                return tf.name + ".jpg"
            except Exception:
                pass
        return tf.name
    except Exception as e:
        logger.debug(f"이미지 다운로드 실패({url[:60]}): {e}")
        return None


def _naver_image(query: str) -> Optional[str]:
    """네이버 이미지 검색 → 첫 결과 다운로드. 키 없으면 None."""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return None
    try:
        r = httpx.get(
            "https://openapi.naver.com/v1/search/image.json",
            params={"query": query, "display": 1, "sort": "sim"},
            headers={"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
        if not items:
            return None
        url = items[0].get("link") or items[0].get("thumbnail")
        if not url:
            return None
        return _download_image(url)
    except Exception as e:
        logger.debug(f"네이버 이미지 검색 실패: {e}")
        return None


def _wikimedia_image(query: str) -> Optional[str]:
    """Wikimedia Commons 검색 → 첫 결과 다운로드. 키 불필요."""
    try:
        r = httpx.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 3,
                "prop": "imageinfo", "iiprop": "url|mimetype", "iiurlwidth": 1280,
            },
            headers={"User-Agent": "DocuMotionStudio/1.0 (map place photo)"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        pages = (r.json().get("query") or {}).get("pages") or {}
        for p in pages.values():
            ii = (p.get("imageinfo") or [{}])[0]
            url = ii.get("thumburl") or ii.get("url")
            mime = ii.get("mime", "")
            if url and ("image" in mime or url.lower().endswith((".jpg", ".jpeg", ".png"))):
                path = _download_image(url, referer="https://commons.wikimedia.org/")
                if path:
                    return path
    except Exception as e:
        logger.debug(f"Wikimedia 이미지 검색 실패: {e}")
    return None


def fetch_place_photo(query: str, name: str = "") -> Optional[str]:
    """
    장소 대표 사진을 로컬 파일로 저장, 경로 반환.
    1순위: 네이버 이미지 검색 (상가/음식점 강점)
    2순위: Wikimedia Commons (랜드마크/명소, 키 불필요)
    실패 시 None (호출자가 지도 폴백).
    """
    q = (query or name or "").strip()
    if not q:
        return None
    # 이름이 더 구체적이면 이름 우선 (예: "경복궁")
    candidates = list(dict.fromkeys([q, name, name or q]))
    for cand in candidates:
        if not cand:
            continue
        path = _naver_image(cand)
        if path:
            logger.info(f"장소 사진(네이버): {cand!r}")
            return path
        path = _wikimedia_image(cand)
        if path:
            logger.info(f"장소 사진(wikimedia): {cand!r}")
            return path
    return None
