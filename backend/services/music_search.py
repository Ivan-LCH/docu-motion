"""
DocuMotion - Jamendo 음악 검색 공용 헬퍼 (BGM 검색 / AI 추천 / 간편 자동 후보)

  · jamendo_search: 연주곡(vocalinstrumental) · 길이(durationbetween) · musicinfo+licenses
  · 오류 구분: 키 없음 → JamendoError(503, jamendo_key_missing), 서버 오류/타임아웃 → (502, jamendo_error)
    결과 0건은 오류가 아니다 (호출 측에서 empty=true)
  · 한국어 검색어 → 영어 키워드: Gemini → 내장 사전 → 원문
  · AI 키워드: 장소·느낌·나레이션·제목 → 영어 음악 검색 키워드 1~3개 (Gemini → 사전 → 기본값)
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from backend.core import config
from backend.core.logger import get_logger

logger = get_logger(__name__)

JAMENDO_TRACKS_URL = "https://api.jamendo.com/v3.0/tracks/"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_KEYWORDS = ["calm acoustic"]
_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")

# 한국어 → 영어 음악 검색어 (Gemini 실패 시). 긴 표현을 먼저 검사한다.
KO_EN_DICT: list[tuple[str, str]] = [
    ("크리스마스", "christmas"), ("겨울", "winter"), ("가을", "autumn"), ("단풍", "autumn"),
    ("봄", "spring"), ("벚꽃", "spring"), ("여름", "summer"), ("바다", "ocean"), ("해변", "beach"),
    ("산책", "walk"), ("잔잔", "calm"), ("차분", "calm"), ("평화", "peaceful"), ("신나는", "upbeat"),
    ("신나", "upbeat"), ("경쾌", "upbeat"), ("따뜻한", "warm"), ("따뜻", "warm"), ("아늑", "cozy"),
    ("피아노", "piano"), ("기타", "guitar"), ("어쿠스틱", "acoustic"), ("우쿨렐레", "ukulele"),
    ("여행", "travel"), ("숲", "forest"), ("수목원", "forest"), ("공원", "park"), ("정원", "garden"),
    ("꽃", "flowers"), ("비", "rain"), ("눈", "snow"), ("밤", "night"), ("아침", "morning"),
    ("노을", "sunset"), ("카페", "cafe"), ("감성", "emotional"), ("웅장", "epic"), ("행복", "happy"),
    ("즐거", "happy"), ("슬픈", "sad"), ("슬픔", "sad"), ("몽환", "dreamy"), ("로맨틱", "romantic"),
    ("사랑", "love"), ("가족", "family"), ("아이", "kids"), ("도시", "city"), ("재즈", "jazz"),
    ("시네마틱", "cinematic"), ("영화", "cinematic"), ("산", "mountain"), ("호수", "lake"),
]


class JamendoError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text or ""))


def dict_translate(text: str, limit: int = 4) -> list[str]:
    """사전 기반 번역: 원문에 나타난 순서대로 영어 단어 (중복 제거). 매칭 없으면 []."""
    text = text or ""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for ko, en in KO_EN_DICT:
        for m in re.finditer(re.escape(ko), text):
            a, b = m.start(), m.end()
            if any(a < y and x < b for x, y in taken):      # 더 긴 표현과 겹치면 건너뜀
                continue
            taken.append((a, b))
            found.append((a, en))
    out: list[str] = []
    for _, en in sorted(found):
        if en not in out:
            out.append(en)
    return out[:limit]


def _clean_keyword(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9 \-]", " ", str(s or "")).lower()
    return " ".join(s.split())[:40]


async def _gemini(prompt: str, timeout: float = 15) -> Optional[str]:
    if not config.GOOGLE_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                GEMINI_URL.format(model=config.GEMINI_MODEL),
                params={"key": config.GOOGLE_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.3}})
        if r.status_code != 200:
            logger.warning(f"Gemini HTTP {r.status_code}")
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning(f"Gemini 호출 실패: {e}")
        return None


def _parse_keywords(raw: Optional[str], k: int = 3) -> list[str]:
    if not raw:
        return []
    items: list = []
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            items = json.loads(m.group(0))
        except Exception:
            items = []
    if not items:
        items = re.split(r"[,\n]", raw)
    out: list[str] = []
    for it in items:
        kw = _clean_keyword(it if isinstance(it, str) else "")
        if kw and kw not in out and not has_hangul(kw):
            out.append(kw)
    return out[:k]


async def translate_query(q: str) -> dict:
    """검색어 번역. 반환: {query_used, translated, translate_source: 'gemini'|'dict'|'none'}."""
    q = " ".join((q or "").split())
    if not q or not has_hangul(q):
        return {"query_used": q, "translated": False, "translate_source": "none"}
    raw = await _gemini(
        "다음 한국어 배경음악 검색어를 Jamendo 에서 검색할 영어 키워드 1~3개로 바꿔줘. "
        f"JSON 문자열 배열만 출력. 예: [\"christmas\", \"cozy\"]\n검색어: {q}")
    kws = _parse_keywords(raw)
    if kws:
        return {"query_used": " ".join(kws), "translated": True, "translate_source": "gemini"}
    words = dict_translate(q)
    if words:
        return {"query_used": " ".join(words), "translated": True, "translate_source": "dict"}
    return {"query_used": q, "translated": False, "translate_source": "none"}


async def ai_keywords(context: str) -> tuple[list[str], str]:
    """영상 맥락(장소·느낌·나레이션·제목) → 영어 음악 키워드 1~3개. 반환: (keywords, source)."""
    context = (context or "").strip()[:1500]
    if context:
        raw = await _gemini(
            "다음은 사진/영상 브이로그의 장소와 느낌 메모야:\n"
            f"{context}\n\n"
            "이 분위기에 맞는 연주곡 배경음악을 Jamendo 에서 찾을 영어 검색 키워드 1~3개를 추천해줘. "
            "각 키워드는 1~2 단어 (예: \"calm acoustic\", \"autumn walk\"). JSON 문자열 배열만 출력.")
        kws = _parse_keywords(raw)
        if kws:
            return kws, "gemini"
        words = dict_translate(context, limit=4)
        if words:
            # 두 단어씩 묶어 칩 1~2개 ('autumn walk', 'calm')
            chips = [" ".join(words[i:i + 2]) for i in range(0, min(len(words), 4), 2)]
            return chips[:3], "dict"
    return list(DEFAULT_KEYWORDS), "default"


def jamendo_hit(t: dict) -> dict:
    tags = (t.get("musicinfo") or {}).get("tags") or {}
    return {
        "id": t.get("id"),
        "title": t.get("name") or f"Track {t.get('id')}",
        "artist": t.get("artist_name", ""),
        "duration": int(t.get("duration") or 0),
        "preview_url": t.get("audio", ""),
        "page_url": t.get("shareurl", ""),
        "license_url": t.get("license_ccurl", ""),
        "audiodownload_allowed": bool(t.get("audiodownload_allowed")),
        "audiodownload": t.get("audiodownload", "") or "",
        "tags": ", ".join((tags.get("genres") or []) + (tags.get("instruments") or [])
                          + (tags.get("vartags") or [])),
    }


async def jamendo_search(query: str = "", *, limit: int = 15, offset: int = 0,
                         instrumental: bool = False, duration_range: Optional[tuple] = None,
                         fuzzytags: Optional[str] = None, track_id=None) -> tuple[list[dict], int]:
    """Jamendo tracks 검색. 반환: (hits, total). 키 없음/서버 오류 → JamendoError."""
    if not config.JAMENDO_CLIENT_ID:
        raise JamendoError("jamendo_key_missing",
                           "JAMENDO_CLIENT_ID 가 설정되지 않아 음악을 검색할 수 없습니다", 503)
    params = {"client_id": config.JAMENDO_CLIENT_ID, "format": "json", "limit": int(limit),
              "offset": int(offset), "include": "musicinfo licenses", "audioformat": "mp32"}
    if track_id is not None:
        params["id"] = str(track_id)
    if query:
        params["search"] = query
    if fuzzytags:
        params["fuzzytags"] = fuzzytags
    if instrumental:
        params["vocalinstrumental"] = "instrumental"
    if duration_range:
        lo, hi = int(max(0, duration_range[0])), int(max(duration_range[0], duration_range[1]))
        params["durationbetween"] = f"{lo}_{hi}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(JAMENDO_TRACKS_URL, params=params)
    except Exception as e:
        logger.warning(f"Jamendo 요청 실패: {e}")
        raise JamendoError("jamendo_error", "음악 서버에 연결하지 못했습니다", 502)
    if resp.status_code != 200:
        raise JamendoError("jamendo_error", f"음악 서버 오류 ({resp.status_code})", 502)
    try:
        data = resp.json()
    except Exception:
        raise JamendoError("jamendo_error", "음악 서버 응답을 읽지 못했습니다", 502)
    head = data.get("headers") or {}
    if head.get("status") == "failed":
        raise JamendoError("jamendo_error", f"음악 서버 오류: {head.get('error_message', '')}"[:200], 502)
    results = data.get("results") or []
    hits = [jamendo_hit(t) for t in results if t.get("audio")]
    return hits, int(head.get("results_fullcount") or len(hits))
