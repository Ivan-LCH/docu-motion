"""
DocuMotion - YouTube 제목·설명·태그 자동 생성

입력: 프로젝트 이름, 슬라이드 meta.easy_place / easy_feel (간편 자동 자막), 슬라이드 text,
      프로젝트 meta.bgm_credit (Jamendo 크레딧 → 설명에 항상 포함)
  · Gemini (GOOGLE_API_KEY 있을 때, use_ai=True): JSON {title, summary, tags_ko, tags_en}
  · 실패/키 없음 → 템플릿: "{장소} 산책 · {느낌}", 2~3줄 요약, 📍 장소, 크레딧, 해시태그 3개
태그: 장소 · 지역(경기 수원시 → 수원, 수원시, 경기) · 분위기 단어, 한국어 + 영어, 500자 제한.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from backend.core import config
from backend.core.logger import get_logger
from backend.services.music_credit import credit_description
from backend.services.presets import project_meta
from backend.services.youtube_manager import clean_description, clean_tags, clean_title

logger = get_logger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = 12

# (원문에서 찾을 말, 한국어 태그, 영어 태그). 긴 표현을 먼저 검사한다.
KO_EN_TAGS: list[tuple[str, str, str]] = [
    ("브이로그", "브이로그", "vlog"), ("크리스마스", "크리스마스", "christmas"),
    ("수목원", "수목원", "arboretum"), ("식물원", "식물원", "botanical garden"),
    ("저수지", "저수지", "reservoir"), ("벚꽃", "벚꽃", "cherry blossom"), ("단풍", "단풍", "autumn leaves"),
    ("산책", "산책", "walk"), ("가을", "가을", "autumn"), ("겨울", "겨울", "winter"),
    ("여름", "여름", "summer"), ("호수", "호수", "lake"), ("바다", "바다", "sea"), ("해변", "해변", "beach"),
    ("공원", "공원", "park"), ("정원", "정원", "garden"), ("여행", "여행", "travel"),
    ("감성", "감성", "mood"), ("힐링", "힐링", "healing"), ("고요", "고요함", "calm"),
    ("잔잔", "잔잔함", "calm"), ("평화", "평화", "peaceful"), ("노을", "노을", "sunset"),
    ("아침", "아침", "morning"), ("오후", "오후", "afternoon"), ("카페", "카페", "cafe"),
    ("가족", "가족", "family"), ("숲", "숲", "forest"), ("꽃", "꽃", "flowers"), ("봄", "봄", "spring"),
]
DEFAULT_TAGS = [("산책", "walk"), ("브이로그", "vlog")]
_REGION_SUFFIX = ("특별시", "광역시", "특별자치시", "특별자치도", "시", "군", "구")


# ────────────────────────────────────────────────────────────────────────── #
# 입력 모으기
# ────────────────────────────────────────────────────────────────────────── #
def _meta(s) -> dict:
    raw = s.get("meta") if isinstance(s, dict) else getattr(s, "meta", None)
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _attr(s, k) -> str:
    v = s.get(k) if isinstance(s, dict) else getattr(s, k, "")
    return " ".join(str(v or "").split())


def _uniq(items) -> list[str]:
    out: list[str] = []
    for it in items:
        if it and it not in out:
            out.append(it)
    return out


def collect_inputs(project, slides: list) -> dict:
    places, feels, texts = [], [], []
    for s in slides or []:
        m = _meta(s)
        places.append(" ".join(str(m.get("easy_place") or "").split()))
        feels.append(" ".join(str(m.get("easy_feel") or "").split()))
        texts.append(_attr(s, "text"))
    credit = project_meta(slides or []).get("bgm_credit")
    name = " ".join(str(getattr(project, "name", "") or "").split())
    return {"name": name, "places": _uniq(places), "feels": _uniq(feels),
            "texts": _uniq(texts)[:6], "credit": credit if isinstance(credit, dict) else None}


def place_short(place: str) -> str:
    """'경기 수원시 일월수목원' → '일월수목원'."""
    parts = (place or "").split()
    return parts[-1] if parts else ""


def region_tokens(place: str) -> list[str]:
    """'경기 수원시 일월수목원' → ['일월수목원', '수원', '수원시', '경기']."""
    parts = (place or "").split()
    if not parts:
        return []
    out = [parts[-1]]
    for p in reversed(parts[:-1]):
        for suf in _REGION_SUFFIX:
            if p.endswith(suf) and len(p) - len(suf) >= 2:
                out.append(p[: -len(suf)])
                break
        out.append(p)
    return _uniq(out)


def mood_tags(text: str) -> list[tuple[str, str]]:
    """원문에서 찾은 (한국어, 영어) 태그 쌍 (등장 순서, 겹치는 짧은 표현은 건너뜀)."""
    found: list[tuple[int, str, str]] = []
    taken: list[tuple[int, int]] = []
    for ko, tag_ko, en in KO_EN_TAGS:
        for m in re.finditer(re.escape(ko), text or ""):
            a, b = m.start(), m.end()
            if any(a < y and x < b for x, y in taken):
                continue
            taken.append((a, b))
            found.append((a, tag_ko, en))
    out: list[tuple[str, str]] = []
    for _, k, e in sorted(found):
        if (k, e) not in out:
            out.append((k, e))
    return out


def _hashtag(t: str) -> str:
    return "#" + re.sub(r"[\s#,]", "", t)


# ────────────────────────────────────────────────────────────────────────── #
# 템플릿
# ────────────────────────────────────────────────────────────────────────── #
def template_title(inp: dict) -> str:
    place = place_short(inp["places"][0]) if inp["places"] else ""
    feel = inp["feels"][0] if inp["feels"] else ""
    if place:
        head = place if "산책" in place else f"{place} 산책"
        return f"{head} · {feel}" if feel else head
    return feel or inp["name"] or "DocuMotion"


def template_summary(inp: dict) -> str:
    place = place_short(inp["places"][0]) if inp["places"] else ""
    feel = inp["feels"][0] if inp["feels"] else ""
    lines = []
    if place and feel:
        lines.append(f"{place}에서 담은 {feel}.")
    elif place:
        lines.append(f"{place}에서 담은 순간들.")
    elif feel:
        lines.append(f"{feel}.")
    else:
        lines.append(f"{inp['name'] or '오늘'}의 기록.")
    others = [place_short(p) for p in inp["places"][1:4]]
    if others:
        lines.append("함께 걸은 곳: " + ", ".join(others))
    elif inp["texts"]:
        lines.append(inp["texts"][0][:120])
    lines.append("사진과 영상으로 엮은 짧은 기록이에요.")
    return "\n".join(lines[:3])


def build_tags(inp: dict, extra: Optional[list[str]] = None) -> list[str]:
    ko: list[str] = []
    en: list[str] = []
    for p in inp["places"][:3]:
        ko += region_tokens(p)
    text = " ".join(inp["places"] + inp["feels"] + [inp["name"]])
    for k, e in mood_tags(text) + DEFAULT_TAGS:
        ko.append(k)
        en.append(e)
    tags = ko[:8] + list(extra or []) + ko[8:] + en
    return clean_tags(tags)


def pick_hashtags(inp: dict, tags: list[str], k: int = 3) -> list[str]:
    """장소 · 지역 · 분위기 순으로 3개 (영어 태그 제외)."""
    first = region_tokens(inp["places"][0])[:2] if inp["places"] else []
    moods = [ko for ko, _ in mood_tags(" ".join(inp["feels"] + inp["places"])) + DEFAULT_TAGS]
    out: list[str] = []
    for t in first + moods + tags:
        if re.search(r"[A-Za-z]", t) or any(t in o or o in t for o in out):
            continue
        out.append(t)
    return [_hashtag(t) for t in out[:k]]


def compose_description(summary: str, inp: dict, tags: list[str]) -> str:
    parts = [summary.strip()]
    if inp["places"]:
        parts.append(f"📍 {inp['places'][0]}")
    credit = credit_description(inp["credit"])
    if credit:
        parts.append(credit)
    hashtags = pick_hashtags(inp, tags)
    if hashtags:
        parts.append(" ".join(hashtags))
    return clean_description("\n\n".join(p for p in parts if p))


# ────────────────────────────────────────────────────────────────────────── #
# Gemini (선택)
# ────────────────────────────────────────────────────────────────────────── #
def _gemini(prompt: str, timeout: float = GEMINI_TIMEOUT) -> Optional[str]:
    if not config.GOOGLE_API_KEY:
        return None
    try:
        r = httpx.post(GEMINI_URL.format(model=config.GEMINI_MODEL),
                       params={"key": config.GOOGLE_API_KEY}, timeout=timeout,
                       json={"contents": [{"parts": [{"text": prompt}]}],
                             "generationConfig": {"temperature": 0.5,
                                                  "responseMimeType": "application/json"}})
        if r.status_code != 200:
            logger.warning(f"Gemini HTTP {r.status_code}")
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning(f"Gemini 호출 실패: {e}")
        return None


def _parse_ai(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    title = " ".join(str(d.get("title") or "").split())
    summary = str(d.get("summary") or "").strip()
    if not title or not summary:
        return None

    def _list(v):
        return [str(x).strip() for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []
    return {"title": title, "summary": summary,
            "tags_ko": _list(d.get("tags_ko")), "tags_en": _list(d.get("tags_en"))}


def _ai_prompt(inp: dict) -> str:
    lines = [f"프로젝트: {inp['name']}"]
    if inp["places"]:
        lines.append("장소: " + ", ".join(inp["places"][:5]))
    if inp["feels"]:
        lines.append("느낌: " + ", ".join(inp["feels"][:5]))
    if inp["texts"]:
        lines.append("자막/나레이션: " + " / ".join(t[:100] for t in inp["texts"][:4]))
    return ("다음은 사진·영상으로 만든 짧은 산책/여행 브이로그 정보야:\n" + "\n".join(lines) + "\n\n"
            "유튜브 업로드용 메타데이터를 만들어줘. 과장 없이 담백하게, 한국어로.\n"
            "- title: 60자 이내, 장소와 느낌이 드러나게\n"
            "- summary: 2~3줄 설명 (줄바꿈 \\n)\n"
            "- tags_ko: 한국어 태그 5~8개, tags_en: 영어 태그 3~6개\n"
            'JSON 만 출력: {"title": "", "summary": "", "tags_ko": [], "tags_en": []}')


# ────────────────────────────────────────────────────────────────────────── #
# 진입점
# ────────────────────────────────────────────────────────────────────────── #
def build_metadata(project, slides: list, use_ai: bool = True) -> dict:
    """→ {title, description, tags, privacy: 'private', source: 'ai'|'template', credit}."""
    inp = collect_inputs(project, slides)
    ai = _parse_ai(_gemini(_ai_prompt(inp))) if use_ai else None
    if ai:
        title = clean_title(ai["title"], inp["name"])
        tags = build_tags(inp, extra=ai["tags_ko"] + ai["tags_en"])
        summary, source = ai["summary"], "ai"
    else:
        title = clean_title(template_title(inp), inp["name"])
        tags = build_tags(inp)
        summary, source = template_summary(inp), "template"
    return {
        "title": title,
        "description": compose_description(summary, inp, tags),
        "tags": tags,
        "privacy": "private",
        "source": source,
        "credit": credit_description(inp["credit"]),
    }
