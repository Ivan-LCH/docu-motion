"""
DocuMotion - 자동 편집 1단계 (시칠리안 브이로그 감성 하이라이트)

사진/짧은 영상 프로젝트를 BGM 위 하이라이트로 자동 연출한다 (설계: auto-edit-stylize.md Stage 1).

  1. 음악 인트로 — 첫 intro_seconds 동안 나레이션·자막 없이 BGM 만. 첫 슬라이드에
     프로젝트 제목을 키네틱 타이포로 표시 (기존 중앙 인트로 타이틀은 생략), BGM 페이드 인.
  2. 위치 카드 — EXIF GPS 를 시간 순서대로 묶어(클러스터) 각 클러스터 첫 컷에 📍 카드.
     GPS 가 없으면 place 슬라이드 주소 → 프로젝트 제목 순으로 폴백.
  3. 중간 키네틱 문구 — 중간 구간의 2~3장(서로 인접하지 않게)에 짧은 한글 문구.
     기본은 EXIF 시각·계절·장소 기반 템플릿, 선택적으로 Gemini (opts.use_ai).
  4. 나머지 — 텍스트 없음 + 무나레이션 (음악 전용). opts.narration=True 일 때만 일부 나레이션.
  6. (2단계) 그림→실사 — opts.stylize_ratio > 0 이면 사진 일부를 그림 스틸
     (수채 애니풍/파스텔 채색 스케치/부드러운 스케치/만화/스케치)로 시작해
     brush/wipe/dissolve 마스크로 실사가 드러나게 (photo_effect + meta.effect_to_real).
     장소가 바뀌는 첫 컷(위치 카드)을 우선, 서로 인접하지 않게 고른다. 로컬 필터만 사용.
  5. 전환 — 인트로 직후 zoom_through, 장소(클러스터)가 바뀔 때 whip_left/right 교대,
     그 외 기존 AUTO_TRANSITION_CYCLE 순환. Ken Burns 유지.

모든 정보성 단계(역지오코딩, Gemini)는 실패해도 생략만 하고 계속 진행한다.
Stage 2: opts.stylize_ratio (0~0.5, 기본 0) /
         stylize_effect ('auto'|'sketch'|'cartoon'|'sketch_soft'|'watercolor'|'sketch_color').
         auto 는 watercolor → sketch_color → sketch_soft → cartoon → sketch 순환.

간편 자동 (opts.easy=True): 사용자가 적은 meta.easy_place/easy_feel 이 자막의 원본이다 (절대 지우지 않음).
  위치 카드는 resolve_easy_captions 규칙으로 만들고, 중간 키네틱 문구·나레이션은 끈다 (인트로 제목만).
세부 편집: 계획이 정한 필드는 meta.auto_fields 에 기록. 사용자가 그 필드를 고치면 meta.user_locked
  (slides API) → 이후 계획/다시 섞기는 그 슬라이드의 효과를 건드리지 않는다. opts.seed 로 순환 시작점을 바꾼다.
"""
from __future__ import annotations

import json
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.core import config
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.services import task_status
from backend.services.color_grade import VALID_STYLE_PRESETS
from backend.services.exif_service import GROUP_RADIUS_KM, haversine_km
from backend.services.geo_label import city_short, parse_address_parts, reverse_geocode
from backend.services.media_import import media_duration
from backend.services.presets import (AUTO_TRANSITION_CYCLE, EASY_VIDEO_VOLUME, PROJECT_META_KEYS,
                                      easy_clip_window, project_meta)

logger = get_logger(__name__)

TASK_KEY = "auto_edit:{project_id}"

DEFAULT_OPTS: dict = {
    "intro_seconds": 5.0,      # 음악 인트로 길이 (0 = 인트로 없음)
    "music_intro": True,       # False → 인트로/키네틱 제목 없이 기존 중앙 타이틀 유지
    "location_cards": True,
    "kinetic": True,
    "narration": False,        # 기본은 음악 전용 하이라이트
    "narration_ratio": 0.3,    # narration=True 일 때 나레이션 슬라이드 비율
    "tone": "vlog",
    "use_ai": False,           # True + GOOGLE_API_KEY → 키네틱 문구를 Gemini 로
    "style_preset": "cinematic",
    "slide_duration": 3.0,
    "resolution": None,        # "720p" | "1080p" | None(프로젝트 값 유지)
    "bgm_path": None,          # 프로젝트 BGM 대신 쓸 파일 (assets 로 복사)
    "bgm_fade_in": 1.0,
    "use_geocode": True,
    "cluster_km": GROUP_RADIUS_KM,
    "stylize_ratio": 0.0,      # 2단계: 그림→실사 전환을 넣을 사진 비율 (0 = 끔, 최대 0.5)
    "stylize_effect": "auto",  # 'auto'(_STYLIZE_CYCLE 순환) | 'sketch' | 'cartoon' | 'sketch_soft' | 'watercolor' | 'sketch_color'
    "stylize_reveal": 0.5,     # 전환 창 (슬라이드 길이 비율)
    "easy": False,             # 간편 자동: easy_place/easy_feel → 카드, 중간 키네틱·나레이션 끔
    "seed": 0,                 # 다시 섞기: 전환/그림 스타일/Ken Burns 순환 시작점 (0 = 기존과 동일)
    "require_bgm": True,       # False → BGM 확인 생략 (간편 자동 길이 추정용 dry-run)
}

KINETIC_MAX_CHARS = 8
_INTRO_MAX_SLIDES = 4
# 자동 편집이 관리하는 meta 키 (재실행 시 초기화)
_MANAGED_META_KEYS = ("location_card", "kinetic_text", "kinetic_sub", "kinetic_highlight",
                      "intro_kinetic", "bgm_fade_in", "effect_to_real", "auto_stylized", "easy_duration",
                      "video_fit")
# 계획이 정하는 슬라이드 필드 (meta.auto_fields 에 기록 → 에디터 「자동」 배지, 사용자 수정 시 user_locked)
AUTO_FIELD_KEYS = ("transition", "ken_burns", "photo_effect", "effect_reveal", "effect_to_real",
                   "location_card", "easy_duration")
_EFFECT_FIELDS = ("transition", "ken_burns", "photo_effect", "effect_reveal", "effect_to_real")
_TO_REAL_CYCLE = ("brush", "wipe", "brush", "dissolve")
_STYLIZE_CYCLE = ("watercolor", "sketch_color", "sketch_soft", "cartoon", "sketch")   # stylize_effect='auto' 순환 순서
_STYLIZE_MAX = 6
_NATURE_WORDS = ("수목원", "공원", "숲", "산", "둘레길", "정원", "식물원", "호수", "계곡", "올레")


class AutoEditError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 계획 단계 오류 (예: BGM 없음)."""


# ────────────────────────────────────────────────────────────────────────── #
# 슬라이드 헬퍼
# ────────────────────────────────────────────────────────────────────────── #
def _meta(s: Slide) -> dict:
    try:
        m = json.loads(s.meta or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _set_meta(s: Slide, m: dict) -> None:
    s.meta = json.dumps(m, ensure_ascii=False)


def _exif(s: Slide) -> dict:
    try:
        e = json.loads(s.exif or "{}")
        return e if isinstance(e, dict) else {}
    except Exception:
        return {}


def _gps(s: Slide) -> Optional[tuple]:
    g = _exif(s).get("gps")
    if isinstance(g, dict) and g.get("lat") is not None and g.get("lng") is not None:
        try:
            return float(g["lat"]), float(g["lng"])
        except (TypeError, ValueError):
            return None
    return None


def _captured(s: Slide) -> Optional[datetime]:
    v = _exif(s).get("captured_at")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def is_photo(s: Slide) -> bool:
    return (s.slide_type or "image") == "image" and bool(s.image_filename)


def is_media(s: Slide) -> bool:
    return is_photo(s) or (s.slide_type == "video" and bool(s.video_filename))


def _date_label(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y.%m.%d") if dt else ""


def _earliest(slides: list) -> Optional[datetime]:
    times = [t for t in (_captured(s) for s in slides) if t]
    return min(times) if times else None


# ────────────────────────────────────────────────────────────────────────── #
# 위치 카드
# ────────────────────────────────────────────────────────────────────────── #
def cluster_slides(slides: list, radius_km: float = GROUP_RADIUS_KM) -> list[dict]:
    """타임라인 순서의 이미지/영상 슬라이드를 GPS 근접으로 묶는다.
    GPS 없는 컷은 직전 클러스터에 붙고, 맨 앞의 GPS 없는 컷은 첫 클러스터에 붙는다.
    반환: [{"gps": (lat,lng), "slides": [Slide...]}] — GPS 가 하나도 없으면 []."""
    clusters: list[dict] = []
    pending: list = []
    for s in slides:
        if not is_media(s):
            continue
        g = _gps(s)
        if g is None:
            (clusters[-1]["slides"] if clusters else pending).append(s)
            continue
        if clusters and haversine_km(clusters[-1]["anchor"], g) < radius_km:
            clusters[-1]["slides"].append(s)
            clusters[-1]["anchor"] = g
            continue
        clusters.append({"gps": g, "anchor": g, "slides": pending + [s]})
        pending = []
    for c in clusters:
        c.pop("anchor", None)
    return clusters


def card_from_geo(geo: dict, date: str = "") -> Optional[dict]:
    """reverse_geocode 결과 → {"title", "sub"}. 제목: '수원 일월수목원' / 보조: '경기도 · 2026.10.25'."""
    if not geo:
        return None
    name = (geo.get("name") or "").strip()
    city = (geo.get("city") or "").strip()
    region = (geo.get("region") or "").strip()
    country = (geo.get("country") or "").strip()
    if name and city and city not in name:
        title = f"{city} {name}"
    else:
        title = name or city or region
    if not title:
        return None
    sub_parts = [region if region and region not in title else ""]
    if country and country not in ("대한민국", "한국", "South Korea", "Korea"):
        sub_parts.append(country)
    sub_parts.append(date)
    return {"title": title, "sub": " · ".join(p for p in sub_parts if p)}


def fallback_card(project: Project, slides: list) -> Optional[dict]:
    """GPS 없음 → place 슬라이드(이름+주소) → 프로젝트 제목/이름."""
    date = _date_label(_earliest(slides))
    place = next((s for s in slides if s.slide_type == "place"
                  or _meta(s).get("type") == "place"), None)
    if place is not None:
        m = _meta(place)
        name = (m.get("name") or place.label or "").strip()
        parts = parse_address_parts(m.get("address") or m.get("display_name") or "")
        city = city_short(parts["city"])
        if name and city and not name.startswith(city):
            title = f"{city} {name}"
        else:
            title = name or city
        if title:
            sub = " · ".join(p for p in (parts["region"], date) if p)
            return {"title": title, "sub": sub}
    title = (project.title_text or "").strip() or (project.name or "").strip()
    if not title:
        return None
    return {"title": title, "sub": date}


def _avoid_title_slide(candidates: list, slides: list):
    """위치 카드가 인트로 키네틱 제목(첫 사진)과 겹치지 않게, 후보가 첫 사진이면
    같은 그룹의 다음 사진으로 옮긴다. 다음 사진이 없으면 그대로 둔다."""
    first_photo = next((s for s in slides if is_photo(s)), None)
    if candidates and candidates[0] is first_photo:
        nxt = next((s for s in candidates[1:] if is_photo(s)), None)
        if nxt is not None:
            return nxt
    return candidates[0]


def plan_location_cards(project: Project, slides: list, use_geocode: bool = True,
                        radius_km: float = GROUP_RADIUS_KM) -> list[dict]:
    """각 GPS 클러스터 첫 컷 meta.location_card 설정. 반환: 카드 요약 리스트.
    GPS 가 없거나 역지오코딩이 전부 실패하면 첫 사진 슬라이드에 폴백 카드 1장."""
    cards: list[dict] = []
    clusters = cluster_slides(slides, radius_km) if use_geocode else []
    for c in clusters:
        lat, lng = c["gps"]
        card = card_from_geo(reverse_geocode(lat, lng), _date_label(_earliest(c["slides"])))
        if not card:
            continue
        first = _avoid_title_slide(c["slides"], slides)
        cards.append({"slide": first, **card, "source": "gps",
                      "cluster_start_id": c["slides"][0].id})

    if not cards:
        photos = [s for s in slides if is_photo(s)]
        first_photo = _avoid_title_slide(photos, slides) if photos else None
        card = fallback_card(project, slides) if first_photo is not None else None
        if card:
            cards.append({"slide": first_photo, **card, "source": "fallback"})

    out = []
    for c in cards:
        s = c.pop("slide")
        start_id = c.pop("cluster_start_id", s.id)
        m = _meta(s)
        m["location_card"] = dict(c)
        _set_meta(s, m)
        out.append({"slide_id": s.id, "order_index": s.order_index,
                    "cluster_start_id": start_id, **c})
    return out


# ────────────────────────────────────────────────────────────────────────── #
# 키네틱 문구
# ────────────────────────────────────────────────────────────────────────── #
def _season(month: int) -> str:
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(month, "autumn")


def _hour_phrase(dt: datetime) -> str:
    h = (dt.hour + (1 if dt.minute >= 30 else 0)) % 24
    if h == 0:
        return "자정"
    if h < 6:
        return f"새벽 {h}시"
    if h < 12:
        return f"오전 {h}시"
    if h == 12:
        return "낮 12시"
    return f"오후 {h - 12}시"


def _mood_phrase(dt: Optional[datetime]) -> str:
    if dt is None:
        return "햇살 가득"
    h = dt.hour
    if 5 <= h < 12:
        return "햇살 가득"
    if 12 <= h < 17:
        return "오후의 여유"
    if 17 <= h < 20:
        return "노을 무렵"
    return "밤 산책"


def template_phrases(k: int, times: list, context: str = "") -> list[str]:
    """결정적 템플릿 문구 k개 (≤8자). times: 각 키네틱 슬라이드의 EXIF 시각(None 가능).
    k=3: [계절, 시각, 마무리], k=2: [계절, 마무리], k=1: [계절]."""
    if k <= 0:
        return []
    nature = any(w in (context or "") for w in _NATURE_WORDS)
    known = [t for t in times if t]
    first = known[0] if known else None
    if first:
        season = _season(first.month)
        seasonal = ({"spring": "봄 꽃길", "summer": "초록 숲길", "autumn": "가을 숲길",
                     "winter": "겨울 숲길"} if nature else
                    {"spring": "봄 나들이", "summer": "여름 한낮", "autumn": "가을 나들이",
                     "winter": "겨울 나들이"})[season]
    else:
        seasonal = "오늘의 기록"
    closing = "천천히 걷기" if nature else _mood_phrase(known[-1] if known else None)
    mid_t = times[len(times) // 2] if times else None
    middle = _hour_phrase(mid_t) if mid_t else _mood_phrase(None)

    plan = {1: [seasonal], 2: [seasonal, closing], 3: [seasonal, middle, closing]}.get(
        k, [seasonal, middle, closing])
    spares = ["햇살 가득", "숨 고르기", "오늘의 기록", "천천히 걷기"]
    out: list[str] = []
    for p in plan + spares:
        if p not in out:
            out.append(p)
        if len(out) >= k:
            break
    return [p[:KINETIC_MAX_CHARS] for p in out[:k]]


def _ai_phrases(k: int, context: str) -> Optional[list[str]]:
    """Gemini 로 ≤8자 한글 문구 k개. 키 없음/실패/형식 오류 → None (템플릿 폴백)."""
    if k <= 0 or not config.GOOGLE_API_KEY:
        return None
    from backend.services import narration as narration_svc
    prompt = (
        "여행/산책 브이로그 영상 중간에 크게 띄울 짧은 한국어 문구를 만들어줘.\n"
        f"상황: {context}\n"
        f"조건: 정확히 {k}개, 각 문구는 공백 포함 {KINETIC_MAX_CHARS}자 이하, 이모지·따옴표 금지, "
        "담백하고 감성적인 톤. JSON 문자열 배열만 출력. 예: [\"가을 숲길\", \"천천히 걷기\"]"
    )
    try:
        raw = narration_svc._post_gemini([{"text": prompt}],
                                         {"temperature": 0.8, "responseMimeType": "application/json"})
        if not raw:
            return None
        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0) if m else raw)
        phrases = [str(x).strip().strip("\"'") for x in items if isinstance(x, str)]
        phrases = [p for p in phrases if 0 < len(p) <= KINETIC_MAX_CHARS]
        return phrases[:k] if len(phrases) >= k else None
    except Exception as e:
        logger.warning(f"[auto-edit] Gemini 문구 생성 실패 → 템플릿: {e}")
        return None


def pick_kinetic_slides(slides: list, exclude_ids: set, blocked_idx: set) -> list[int]:
    """중간 구간 키네틱 슬라이드 인덱스 2~3개 (사진만, 서로·blocked_idx 와 인접 금지, 마지막 컷 제외)."""
    n = len(slides)
    cand = [i for i, s in enumerate(slides)
            if is_photo(s) and s.id not in exclude_ids and i < n - 1 and i not in blocked_idx]
    if not cand:
        return []
    k = 3 if len(cand) >= 9 else (2 if len(cand) >= 3 else 1)
    chosen: list[int] = []
    taken = set(blocked_idx)
    for j in range(k):
        target = cand[0] + (cand[-1] - cand[0]) * (j + 1) / (k + 1)
        for i in sorted(cand, key=lambda c: (abs(c - target), c)):
            if all(abs(i - t) > 1 for t in taken):
                chosen.append(i)
                taken.add(i)
                break
    return sorted(chosen)


def pick_stylize_slides(slides: list, ratio: float, blocked_idx: set,
                        prefer_ids: set = frozenset(), phase: float = 0.0) -> list[int]:
    """그림→실사 전환을 넣을 사진 인덱스. 위치 카드 컷 우선, 서로 인접 금지, 마지막 컷 제외.
    phase(-0.5~0.5): 다시 섞기 때 고르게 뽑는 목표 위치를 칸 안에서 옮긴다 (0 = 기존)."""
    n = len(slides)
    cand = [i for i, s in enumerate(slides)
            if is_photo(s) and i < n - 1 and i not in blocked_idx]
    if not cand or ratio <= 0:
        return []
    k = max(1, min(_STYLIZE_MAX, round(len(cand) * min(ratio, 0.5))))
    chosen: list[int] = []
    for i in cand:                                  # 1) 장소 첫 컷
        if len(chosen) >= k:
            break
        if slides[i].id in prefer_ids and all(abs(i - c) > 1 for c in chosen):
            chosen.append(i)
    for j in range(k):                              # 2) 나머지는 고르게
        if len(chosen) >= k:
            break
        target = cand[0] + (cand[-1] - cand[0]) * min(1.0, max(0.0, (j + 0.5 + phase) / k))
        for i in sorted(cand, key=lambda c: (abs(c - target), c)):
            if i not in chosen and all(abs(i - c) > 1 for c in chosen):
                chosen.append(i)
                break
    return sorted(chosen)


# ────────────────────────────────────────────────────────────────────────── #
# 계획 (DB 반영)
# ────────────────────────────────────────────────────────────────────────── #
def _resolve_opts(opts: Optional[dict]) -> dict:
    o = dict(DEFAULT_OPTS)
    for k, v in (opts or {}).items():
        if v is not None:
            o[k] = v
    if o["style_preset"] not in VALID_STYLE_PRESETS:
        o["style_preset"] = "cinematic"
    o["slide_duration"] = max(1.5, min(8.0, float(o["slide_duration"] or 3.0)))
    o["intro_seconds"] = max(0.0, min(20.0, float(o["intro_seconds"] or 0.0)))
    return o


def ensure_bgm(db, project: Project, bgm_path: Optional[str], commit: bool = True) -> str:
    """BGM 확인/설치. bgm_path 가 있으면 assets 로 'bgm_<이름>' 복사 후 프로젝트에 지정.
    둘 다 없으면 AutoEditError('BGM 필요')."""
    assets = Path(config.OUTPUTS_DIR) / project.id / "assets"
    if bgm_path:
        src = Path(bgm_path)
        if not src.is_file():
            raise AutoEditError(f"BGM 필요: 파일을 찾을 수 없습니다 ({src.name})")
        assets.mkdir(parents=True, exist_ok=True)
        name = src.name if src.name.startswith("bgm_") else f"bgm_{src.name}"
        dst = assets / name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        project.bgm_filename = name
        if commit:
            db.commit()
        return name
    name = (project.bgm_filename or "").strip()
    if not name or not (assets / name).is_file():
        raise AutoEditError("BGM 필요: 자동 편집은 배경음악이 있어야 합니다. 먼저 BGM 을 추가하세요")
    return name


# ────────────────────────────────────────────────────────────────────────── #
# 간편 자동: 장소·느낌 → 위치 카드
# ────────────────────────────────────────────────────────────────────────── #
def resolve_easy_captions(entries: list[dict], project_title: str, cluster_card=None) -> list[dict]:
    """간편 자동 자막 해석 (순수 함수).

    entries: 타임라인 순서의 미디어 슬라이드 [{id, place, feel, cluster}] (cluster: GPS 묶음 번호|None)
    cluster_card(cluster) → {"title","sub"}|None : 그 GPS 묶음의 역지오코딩 카드 (묶음당 1회, 실패 시 None)

    실제 장소(effective place) 규칙: 자기 easy_place → (자막 없는 새 GPS 묶음의 첫 컷이면) GPS 지명
      → 앞 장소 이어받기 → GPS 지명 → 프로젝트 제목.
    카드 표시: 자기 장소나 느낌을 적은 컷 (제목=실제 장소, 보조=느낌)
      + 사용자 자막이 하나도 없는 GPS 묶음의 첫 컷 (제목=지명, 보조=지역·날짜).
      자막이 있는 묶음은 사용자 카드가 이미 있으니 GPS 카드를 겹쳐 띄우지 않는다.
    반환: [{id, place, source('user'|'gps'|'inherit'|'title'), card|None, scene_start(bool)}]"""
    cluster_card = cluster_card or (lambda c: None)
    geo_cache: dict = {}

    def geo(c):
        if c not in geo_cache:
            try:
                geo_cache[c] = cluster_card(c)
            except Exception:
                geo_cache[c] = None
        return geo_cache[c]

    def txt(v) -> str:
        return " ".join(str(v or "").split())

    captioned = {e.get("cluster") for e in entries
                 if (txt(e.get("place")) or txt(e.get("feel"))) and e.get("cluster") is not None}
    out, prev, seen = [], None, set()
    for e in entries:
        place, feel, c = txt(e.get("place")), txt(e.get("feel")), e.get("cluster")
        new_cluster = c is not None and c not in seen
        if c is not None:
            seen.add(c)
        g = None
        if place:
            eff, source = place, "user"
        elif new_cluster and c not in captioned and (g := geo(c)):
            eff, source = g["title"], "gps"
        elif prev:
            eff, source = prev, "inherit"
        elif c is not None and (g := geo(c)):
            eff, source = g["title"], "gps"
        else:
            eff, source = txt(project_title), "title"
        card = None
        if place or feel:
            if eff:
                card = {"title": eff, "sub": feel}
            elif feel:
                card = {"title": feel, "sub": ""}
        elif source == "gps" and g and new_cluster:
            card = {"title": eff, "sub": str(g.get("sub") or "")}
        out.append({"id": e.get("id"), "place": eff, "source": source, "card": card,
                    "scene_start": bool(eff) and eff != prev and source in ("user", "gps")
                    and prev is not None})
        prev = eff or prev
    return out


def plan_easy_cards(project: Project, slides: list, use_geocode: bool = True,
                    radius_km: float = GROUP_RADIUS_KM) -> tuple[list[dict], set]:
    """meta.easy_place/easy_feel → meta.location_card. 반환: (카드 요약, 장면 전환 슬라이드 id 집합)."""
    media = [s for s in slides if is_media(s)]
    clusters = cluster_slides(media, radius_km) if use_geocode else []
    member = {}
    for ci, c in enumerate(clusters):
        for s in c["slides"]:
            member[s.id] = ci

    def cluster_card(ci):
        lat, lng = clusters[ci]["gps"]
        return card_from_geo(reverse_geocode(lat, lng), _date_label(_earliest(clusters[ci]["slides"])))

    entries = []
    for s in media:
        m = _meta(s)
        entries.append({"id": s.id, "place": m.get("easy_place"), "feel": m.get("easy_feel"),
                        "cluster": member.get(s.id)})
    title = (project.title_text or "").strip() or (project.name or "").strip()
    resolved = resolve_easy_captions(entries, title, cluster_card)
    by_id = {s.id: s for s in media}
    cards, scenes = [], set()
    for r in resolved:
        s = by_id[r["id"]]
        if r["scene_start"]:
            scenes.add(s.id)
        if not r["card"]:
            continue
        src = "easy" if r["source"] != "gps" or _meta(s).get("easy_feel") else "gps"
        m = _meta(s)
        m["location_card"] = {**r["card"], "source": src}
        _set_meta(s, m)
        cards.append({"slide_id": s.id, "order_index": s.order_index, "cluster_start_id": s.id,
                      "title": r["card"]["title"], "sub": r["card"]["sub"], "source": src})
    return cards, scenes


# ────────────────────────────────────────────────────────────────────────── #
# 잠금 / 자동 필드
# ────────────────────────────────────────────────────────────────────────── #
def is_locked(m: dict) -> bool:
    return m.get("user_locked") is True


def _add_auto(m: dict, *fields) -> None:
    cur = [f for f in (m.get("auto_fields") or []) if f in AUTO_FIELD_KEYS]
    for f in fields:
        if f not in cur:
            cur.append(f)
    m["auto_fields"] = cur


def consolidate_project_meta(slides: list) -> dict:
    """프로젝트 단위 meta(PROJECT_META_KEYS)를 slides[0] 으로 모은다 (순서 변경/삭제 대비)."""
    if not slides:
        return {}
    pm = project_meta(slides)
    for i, s in enumerate(slides):
        m = _meta(s)
        changed = False
        for k in PROJECT_META_KEYS:
            if i == 0:
                if k in pm and m.get(k) != pm[k]:
                    m[k] = pm[k]
                    changed = True
            elif k in m:
                m.pop(k)
                changed = True
        if changed:
            _set_meta(s, m)
    return pm


def set_project_meta(slides: list, **kv) -> None:
    """slides[0].meta 에 프로젝트 단위 키 기록 (None → 삭제)."""
    if not slides:
        return
    m = _meta(slides[0])
    for k, v in kv.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    _set_meta(slides[0], m)


# ────────────────────────────────────────────────────────────────────────── #
# 효과 (전환 · 그림→실사 · Ken Burns) — 계획과 다시 섞기 공용
# ────────────────────────────────────────────────────────────────────────── #
def _seed_phase(seed: int) -> float:
    return 0.0 if not seed else ((seed * 0.618034) % 1.0) - 0.5


def apply_effects(slides: list, *, n_intro: int, card_ids: set, scene_ids: set, first_scene,
                  ratio: float, eff: str, reveal: float, seed: int = 0) -> tuple[list, list]:
    """잠기지 않은 슬라이드에 그림→실사 · 전환 · Ken Burns 를 정한다. 반환: (transitions, stylized).
    seed 는 각 순환의 시작점과 그림 슬라이드 위치를 옮긴다 (0 = 기존 결과와 동일)."""
    seed = int(seed or 0)
    locked = {s.id for s in slides if is_locked(_meta(s))}
    stylized = []
    if ratio > 0:
        blocked = set(range(n_intro)) | {i for i, s in enumerate(slides) if s.id in locked}
        idxs = pick_stylize_slides(slides, ratio, blocked, card_ids, phase=_seed_phase(seed))
        for j, i in enumerate(idxs):
            s = slides[i]
            fx = eff if eff in _STYLIZE_CYCLE else _STYLIZE_CYCLE[(j + seed) % len(_STYLIZE_CYCLE)]
            style = _TO_REAL_CYCLE[(j + seed) % len(_TO_REAL_CYCLE)]
            s.photo_effect = fx
            s.effect_reveal = reveal
            m = _meta(s)
            m["effect_to_real"] = style
            m["auto_stylized"] = True
            _add_auto(m, "photo_effect", "effect_reveal", "effect_to_real")
            _set_meta(s, m)
            stylized.append({"slide_id": s.id, "order_index": s.order_index,
                             "effect": fx, "to_real": style})

    transitions, whip, cyc = [], seed % 2, seed % len(AUTO_TRANSITION_CYCLE)
    for i, s in enumerate(slides):
        if s.id in locked:
            transitions.append(s.transition or "none")
            continue
        if i == 0 or i < n_intro:
            t = "crossfade"
        elif i == n_intro:
            t = "zoom_through"
        elif s.id in scene_ids and s.id != first_scene:
            t = ("whip_left", "whip_right")[whip % 2]
            whip += 1
        else:
            t = AUTO_TRANSITION_CYCLE[cyc % len(AUTO_TRANSITION_CYCLE)]
            cyc += 1
        s.transition = t
        transitions.append(t)
        m = _meta(s)
        _add_auto(m, "transition")
        if is_photo(s) and ((s.ken_burns or 0) < 20 or "ken_burns" in (m.get("auto_fields") or [])):
            s.ken_burns = 35 + ((i + seed * 7) * 17) % 40
            _add_auto(m, "ken_burns")
        _set_meta(s, m)
    return transitions, stylized


def _reset_slide(s: Slide, easy: bool) -> None:
    """재계획 전 초기화: 자동 편집 meta 제거 (잠긴 슬라이드의 효과·사용자가 고친 카드는 유지),
    이미지 슬라이드는 무자막·무나레이션 (이전 나레이션 텍스트는 meta.prev_text 에 백업).
    meta.easy_place/easy_feel(간편 자동 자막 원본)은 절대 건드리지 않는다."""
    m = _meta(s)
    locked = is_locked(m)
    user_fields = set(m.get("user_fields") or [])
    if m.get("auto_stylized") and not locked:      # 이전 자동 편집이 넣은 그림 효과만 되돌린다
        s.photo_effect = "none"
        s.effect_reveal = 0.45
    for k in _MANAGED_META_KEYS:
        if locked and k in ("effect_to_real", "auto_stylized"):
            continue
        if k == "location_card" and "location_card" in user_fields:
            continue
        m.pop(k, None)
    if locked:      # 잠긴 슬라이드: 아직 자동 값인 효과 필드 기록만 남긴다 (카드/길이는 다시 계산)
        m["auto_fields"] = [f for f in (m.get("auto_fields") or []) if f in _EFFECT_FIELDS]
    else:
        m.pop("auto_fields", None)
    if s.slide_type != "video":     # 영상 클립의 자막(JSON)·원본 오디오는 그대로 둔다
        if (s.text or "").strip():
            m["prev_text"] = s.text
        s.text = ""
        s.use_tts = 0
        s.text_style = "none"
    _set_meta(s, m)


def _close(a, b, eps: float = 1e-3) -> bool:
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return False


def apply_easy_clip(s: Slide, full: Optional[float]) -> Optional[dict]:
    """간편 자동 영상 클립: 트림 구간(presets.easy_clip_window) · 원본 오디오(EASY_VIDEO_VOLUME) · 화면 맞춤.
    사용자가 정한 값은 그대로 둔다 — 지난번 계획이 넣은 값(meta.easy_clip)이나 가져온 직후 기본값
    (트림 0/0, 볼륨 1.0)일 때만 자동으로 정한다. 잠긴 슬라이드는 건드리지 않는다. 반환: 요약 또는 None."""
    if s.slide_type != "video" or not s.video_filename:
        return None
    m = _meta(s)
    if is_locked(m):
        return None
    rec = m.get("easy_clip") if isinstance(m.get("easy_clip"), dict) else None
    ts, te = float(s.trim_start or 0.0), float(s.trim_end or 0.0)
    vol = 1.0 if s.volume is None else float(s.volume)
    trim_auto = (ts == 0 and te == 0) or (rec is not None and _close(ts, rec.get("trim_start"))
                                         and _close(te, rec.get("trim_end")))
    vol_auto = _close(vol, rec.get("volume")) if rec is not None and "volume" in rec else vol == 1.0
    if trim_auto and full:
        s.trim_start, s.trim_end = easy_clip_window(full)
    if vol_auto:
        s.volume = EASY_VIDEO_VOLUME
    m["easy_clip"] = {"trim_start": float(s.trim_start or 0.0), "trim_end": float(s.trim_end or 0.0),
                      "volume": float(s.volume if s.volume is not None else 1.0)}
    m["video_fit"] = "auto"                 # 렌더러: 비슷한 화면비 → 꽉 채움, 세로 영상 → 흐린 배경
    _set_meta(s, m)
    return {"slide_id": s.id, "order_index": s.order_index, "trim_start": s.trim_start,
            "trim_end": s.trim_end, "volume": s.volume, "user_trim": not trim_auto,
            "user_volume": not vol_auto}


# ────────────────────────────────────────────────────────────────────────── #
# 계획 (DB 반영)
# ────────────────────────────────────────────────────────────────────────── #
def plan_auto_edit(db, project_id: str, opts: Optional[dict] = None, commit: bool = True) -> dict:
    """자동 편집 계획을 슬라이드/프로젝트에 기록하고 요약 dict 반환 (렌더는 하지 않음).
    commit=False → flush 만 (호출 측이 rollback 하면 dry-run: 간편 자동 길이 추정)."""
    o = _resolve_opts(opts)
    easy = bool(o.get("easy"))
    seed = int(o.get("seed") or 0)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise AutoEditError("Project not found")
    slides = (db.query(Slide).filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())
    if not slides:
        raise AutoEditError("슬라이드가 없습니다")
    bgm = ensure_bgm(db, project, o.get("bgm_path"), commit=commit) if o.get("require_bgm", True) \
        else (project.bgm_filename or "")
    n = len(slides)
    consolidate_project_meta(slides)

    # 0) 초기화
    user_cards = {}
    for s in slides:
        m = _meta(s)
        if "location_card" in (m.get("user_fields") or []) and m.get("location_card"):
            user_cards[s.id] = m["location_card"]
        _reset_slide(s, easy)

    # 0.5) 간편 자동: 영상 클립 구간·오디오 (길이 추정·음악 맞춤이 이 트림 길이를 쓴다)
    clips = []
    if easy:
        assets = Path(config.OUTPUTS_DIR) / project_id / "assets"
        for s in slides:
            if s.slide_type == "video" and s.video_filename:
                c = apply_easy_clip(s, media_duration(assets / s.video_filename))
                if c:
                    clips.append(c)

    # 1) 음악 인트로
    sd = o["slide_duration"]
    n_intro = 0
    if o["music_intro"] and o["intro_seconds"] > 0 and n > 1:
        n_intro = max(1, min(math.ceil(o["intro_seconds"] / sd - 1e-6), _INTRO_MAX_SLIDES, n - 1))
    intro_title = (project.title_text or "").strip() or (project.name or "").strip()
    kinetic_intro = None
    if n_intro:
        m0 = _meta(slides[0])
        m0["bgm_fade_in"] = float(o["bgm_fade_in"])
        if (o["kinetic"] or easy) and intro_title and is_media(slides[0]):
            slides[0].text_style = "kinetic"
            m0["kinetic_text"] = intro_title
            m0["intro_kinetic"] = True
            first_dt = next((dt for dt in (_captured(s) for s in slides) if dt is not None), None)
            if first_dt is not None:
                m0["kinetic_sub"] = first_dt.strftime("%Y.%m.%d")
            kinetic_intro = {"order_index": slides[0].order_index, "text": intro_title,
                             **({"sub": m0["kinetic_sub"]} if m0.get("kinetic_sub") else {})}
        _set_meta(slides[0], m0)

    # 2) 위치 카드 (간편 자동: 장소·느낌 자막 → 카드)
    cards, easy_scenes = [], set()
    if easy:
        cards, easy_scenes = plan_easy_cards(project, slides, use_geocode=bool(o["use_geocode"]),
                                             radius_km=float(o["cluster_km"]))
    elif o["location_cards"]:
        cards = plan_location_cards(project, slides, use_geocode=bool(o["use_geocode"]),
                                    radius_km=float(o["cluster_km"]))
    for s in slides:                                 # 사용자가 직접 고친 카드는 그대로 되돌려 둔다
        m = _meta(s)
        if s.id in user_cards:
            m["location_card"] = user_cards[s.id]
        elif m.get("location_card"):
            _add_auto(m, "location_card")
        _set_meta(s, m)
    card_ids = {c["slide_id"] for c in cards}

    # 3) 중간 키네틱 문구 (간편 자동은 끔 — 자막은 장소 카드로만)
    kinetic = []
    if o["kinetic"] and not easy:
        blocked = set(range(n_intro)) | ({0} if kinetic_intro else set())
        idxs = pick_kinetic_slides(slides, card_ids, blocked)
        times = [_captured(slides[i]) for i in idxs]
        place = next((_meta(s).get("name") for s in slides if s.slide_type == "place"), "") or ""
        context = " ".join(p for p in (intro_title, place, *(c["title"] for c in cards)) if p)
        phrases = None
        if o["use_ai"]:
            when = ", ".join(t.strftime("%m월 %d일 %H시") for t in times if t)
            phrases = _ai_phrases(len(idxs), f"{context} / 촬영: {when or '미상'}")
        if not phrases:
            phrases = template_phrases(len(idxs), times, context)
        for i, text in zip(idxs, phrases):
            s = slides[i]
            m = _meta(s)
            m["kinetic_text"] = text
            _set_meta(s, m)
            s.text_style = "kinetic"
            kinetic.append({"slide_id": s.id, "order_index": s.order_index, "text": text,
                            "highlight": text.split()[-1]})

    # 4) 선택: 일부 나레이션 (기본 꺼짐, 간편 자동은 항상 끔)
    narrated = 0
    if o["narration"] and not easy:
        narrated = _narrate_some(project, slides, n_intro,
                                 {k["slide_id"] for k in kinetic} | ({slides[0].id} if kinetic_intro else set()),
                                 float(o["narration_ratio"]), o["tone"])

    # 4.5) 2단계 그림 → 실사 + 5) 전환 + Ken Burns (잠긴 슬라이드 제외)
    ratio = max(0.0, min(0.5, float(o.get("stylize_ratio") or 0.0)))
    eff = str(o.get("stylize_effect") or "auto").lower()
    reveal = max(0.2, min(0.7, float(o.get("stylize_reveal") or 0.5)))
    if easy:
        scene_ids = easy_scenes
        first_scene = None
    else:
        scene_ids = {c["cluster_start_id"] for c in cards if c["source"] == "gps"}
        first_scene = cards[0]["cluster_start_id"] if cards else None
    transitions, stylized = apply_effects(
        slides, n_intro=n_intro, card_ids=card_ids, scene_ids=scene_ids, first_scene=first_scene,
        ratio=ratio, eff=eff, reveal=reveal, seed=seed)

    set_project_meta(slides, auto_plan={
        "easy": easy, "seed": seed, "n_intro": n_intro, "stylize_ratio": ratio,
        "stylize_effect": eff, "stylize_reveal": reveal, "card_ids": sorted(card_ids),
        "scene_ids": sorted(scene_ids), "first_scene": first_scene, "slide_duration": sd})

    project.style_preset = o["style_preset"]
    project.default_slide_duration = sd
    if o.get("resolution") in ("720p", "1080p"):
        project.resolution = o["resolution"]
    project.stage = "scripted"
    project.updated_at = datetime.utcnow()
    if commit:
        db.commit()
    else:
        db.flush()

    summary = {
        "project_id": project_id,
        "slides": n,
        "bgm": bgm,
        "easy": easy,
        "seed": seed,
        "style_preset": o["style_preset"],
        "slide_duration": sd,
        "resolution": project.resolution,
        "intro_seconds": o["intro_seconds"] if n_intro else 0.0,
        "intro_slides": [slides[i].order_index for i in range(n_intro)],
        "kinetic_intro": kinetic_intro,
        "cards": cards,
        "kinetic": kinetic,
        "narrated": narrated,
        "transitions": transitions,
        "stylize_ratio": ratio,
        "stylized": stylized,
        "clips": clips,
        "locked": sum(1 for s in slides if is_locked(_meta(s))),
    }
    logger.info(f"[auto-edit] plan {project_id}: easy={easy} intro={n_intro} cards={len(cards)} "
                f"kinetic={len(kinetic)} narrated={narrated} seed={seed}")
    return summary


def reshuffle_effects(db, project_id: str, seed: Optional[int] = None) -> dict:
    """마지막 계획(meta.auto_plan)의 설정으로 전환·그림→실사·Ken Burns 만 다시 고른다.
    잠긴 슬라이드는 건너뛰고 자막/카드/나레이션은 건드리지 않는다. 렌더하지 않는다."""
    import random
    slides = (db.query(Slide).filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())
    if not slides:
        raise AutoEditError("슬라이드가 없습니다")
    pm = consolidate_project_meta(slides)
    plan = pm.get("auto_plan") if isinstance(pm.get("auto_plan"), dict) else None
    if not plan:
        raise AutoEditError("아직 자동으로 꾸민 적이 없어요. 먼저 간편 자동이나 자동 편집을 실행하세요")
    old_seed = int(plan.get("seed") or 0)
    if seed is None:
        seed = random.randint(1, 9999)
        if seed == old_seed:
            seed += 1
    before = [(s.transition, s.photo_effect, s.ken_burns) for s in slides]
    ids = {s.id for s in slides}
    for s in slides:
        m = _meta(s)
        if is_locked(m):
            continue
        if m.get("auto_stylized"):
            s.photo_effect = "none"
            s.effect_reveal = 0.45
            for k in ("effect_to_real", "auto_stylized"):
                m.pop(k, None)
            m["auto_fields"] = [f for f in (m.get("auto_fields") or [])
                                if f not in ("photo_effect", "effect_reveal", "effect_to_real")]
            _set_meta(s, m)
    n_intro = max(0, min(int(plan.get("n_intro") or 0), len(slides) - 1))
    apply_effects(slides, n_intro=n_intro,
                  card_ids={i for i in plan.get("card_ids") or [] if i in ids},
                  scene_ids={i for i in plan.get("scene_ids") or [] if i in ids},
                  first_scene=plan.get("first_scene"),
                  ratio=float(plan.get("stylize_ratio") or 0.0),
                  eff=str(plan.get("stylize_effect") or "auto"),
                  reveal=float(plan.get("stylize_reveal") or 0.5), seed=seed)
    set_project_meta(slides, auto_plan={**plan, "seed": seed})
    db.commit()
    after = [(s.transition, s.photo_effect, s.ken_burns) for s in slides]
    changed = sum(1 for a, b in zip(before, after) if a != b)
    logger.info(f"[auto-edit] reshuffle {project_id}: seed={seed} changed={changed}")
    return {"ok": True, "seed": seed, "changed": changed, "easy": bool(plan.get("easy"))}


def _narrate_some(project: Project, slides: list, n_intro: int, skip_ids: set,
                  ratio: float, tone: str) -> int:
    """opts.narration=True: 인트로·키네틱 제외 사진 중 일부에 caption 나레이션 (Gemini 필요)."""
    if not config.GOOGLE_API_KEY:
        logger.warning("[auto-edit] GOOGLE_API_KEY 없음 — 나레이션 생략")
        return 0
    from backend.services import narration as narration_svc
    pool = [s for i, s in enumerate(slides) if i >= n_intro and is_photo(s) and s.id not in skip_ids]
    if not pool or ratio <= 0:
        return 0
    count = max(1, math.ceil(len(pool) * min(ratio, 1.0)))
    targets = [pool[round(j * len(pool) / count)] for j in range(min(count, len(pool)))]
    assets = Path(config.OUTPUTS_DIR) / project.id / "assets"
    done = 0
    for s in targets:
        try:
            text = narration_svc.generate_narration_for_image(
                assets / s.image_filename, project_title=project.name or "", tone=tone)
        except Exception as e:
            logger.warning(f"[auto-edit] 나레이션 실패({s.id}): {e}")
            text = ""
        if text:
            s.text, s.use_tts, s.text_style = text, 1, "caption"
            done += 1
    return done


# ────────────────────────────────────────────────────────────────────────── #
# 백그라운드 실행 (API)
# ────────────────────────────────────────────────────────────────────────── #
def run_auto_edit(project_id: str, opts: dict):
    """BackgroundTasks.add_task(run_auto_edit, project_id, opts). 계획 → (opts.render) 렌더."""
    from backend.db.session import SessionLocal
    from backend.services import worker as render_worker

    task_id = TASK_KEY.format(project_id=project_id)
    db = SessionLocal()
    try:
        task_status.set_status(task_id, status="running", phase="planning",
                               progress=5.0, message="자동 편집 계획 중...")
        try:
            summary = plan_auto_edit(db, project_id, opts)
        except AutoEditError as e:
            task_status.set_status(task_id, status="error", phase="planning", message=str(e))
            return
        task_status.set_status(task_id, status="running", phase="planned", progress=20.0,
                               message=f"위치 카드 {len(summary['cards'])}개 · "
                                       f"키네틱 {len(summary['kinetic'])}개",
                               summary=summary)
        if not opts.get("render", True):
            task_status.set_status(task_id, status="done", phase="done", progress=100.0,
                                   message="자동 편집 계획 완료 (렌더 생략)")
            return
        project = db.query(Project).filter(Project.id == project_id).first()
        project.status, project.progress, project.message = "QUEUED", 0, "자동 편집 렌더 대기"
        db.commit()
        db.close()
        task_status.set_status(task_id, status="running", phase="rendering", progress=25.0,
                               message="렌더링 시작...")
        render_worker.run_render(project_id)
        db = SessionLocal()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project and project.status == "COMPLETED":
            task_status.set_status(task_id, status="done", phase="done", progress=100.0,
                                   message="자동 편집 영상 완성")
        else:
            task_status.set_status(task_id, status="error", phase="rendering",
                                   message=f"렌더 실패: {getattr(project, 'message', '')}")
    except Exception as e:
        logger.error(f"[auto-edit] 실패: {e}", exc_info=True)
        task_status.set_status(task_id, status="error", message=str(e)[:300])
    finally:
        db.close()
