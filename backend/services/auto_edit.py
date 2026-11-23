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
  5. 전환 — 인트로 직후 zoom_through, 장소(클러스터)가 바뀔 때 whip_left/right 교대,
     그 외 기존 AUTO_TRANSITION_CYCLE 순환. Ken Burns 유지.

모든 정보성 단계(역지오코딩, Gemini)는 실패해도 생략만 하고 계속 진행한다.
Stage 2(그림화 전환) 훅: opts.stylize_ratio 는 받기만 하고 1단계에서는 사용하지 않는다.
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
from backend.services.presets import AUTO_TRANSITION_CYCLE

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
    "stylize_ratio": 0.0,      # Stage 2 훅 — 1단계에서는 무시
}

KINETIC_MAX_CHARS = 8
_INTRO_MAX_SLIDES = 4
# 자동 편집이 관리하는 meta 키 (재실행 시 초기화)
_MANAGED_META_KEYS = ("location_card", "kinetic_text", "kinetic_sub", "kinetic_highlight",
                      "intro_kinetic", "bgm_fade_in")
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


def ensure_bgm(db, project: Project, bgm_path: Optional[str]) -> str:
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
        db.commit()
        return name
    name = (project.bgm_filename or "").strip()
    if not name or not (assets / name).is_file():
        raise AutoEditError("BGM 필요: 자동 편집은 배경음악이 있어야 합니다. 먼저 BGM 을 추가하세요")
    return name


def plan_auto_edit(db, project_id: str, opts: Optional[dict] = None) -> dict:
    """자동 편집 계획을 슬라이드/프로젝트에 기록하고 요약 dict 반환 (렌더는 하지 않음)."""
    o = _resolve_opts(opts)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise AutoEditError("Project not found")
    slides = (db.query(Slide).filter(Slide.project_id == project_id)
              .order_by(Slide.order_index).all())
    if not slides:
        raise AutoEditError("슬라이드가 없습니다")
    bgm = ensure_bgm(db, project, o.get("bgm_path"))
    n = len(slides)

    # 0) 초기화: 자동 편집 meta 제거, 전부 무자막·무나레이션 (이전 나레이션 텍스트는 meta 에 백업)
    for s in slides:
        m = _meta(s)
        for k in _MANAGED_META_KEYS:
            m.pop(k, None)
        if s.slide_type != "video":     # 영상 클립의 자막(JSON)·원본 오디오는 그대로 둔다
            if (s.text or "").strip():
                m["prev_text"] = s.text
            s.text = ""
            s.use_tts = 0
            s.text_style = "none"
        _set_meta(s, m)

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
        if o["kinetic"] and intro_title and is_media(slides[0]):
            slides[0].text_style = "kinetic"
            m0["kinetic_text"] = intro_title
            m0["intro_kinetic"] = True
            first_dt = next((dt for dt in (_captured(s) for s in slides) if dt is not None), None)
            if first_dt is not None:
                m0["kinetic_sub"] = first_dt.strftime("%Y.%m.%d")
            kinetic_intro = {"order_index": slides[0].order_index, "text": intro_title,
                             **({"sub": m0["kinetic_sub"]} if m0.get("kinetic_sub") else {})}
        _set_meta(slides[0], m0)

    # 2) 위치 카드
    cards = []
    if o["location_cards"]:
        cards = plan_location_cards(project, slides, use_geocode=bool(o["use_geocode"]),
                                    radius_km=float(o["cluster_km"]))
    card_ids = {c["slide_id"] for c in cards}

    # 3) 중간 키네틱 문구
    kinetic = []
    if o["kinetic"]:
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

    # 4) 선택: 일부 나레이션 (기본 꺼짐)
    narrated = 0
    if o["narration"]:
        narrated = _narrate_some(project, slides, n_intro,
                                 {k["slide_id"] for k in kinetic} | ({slides[0].id} if kinetic_intro else set()),
                                 float(o["narration_ratio"]), o["tone"])

    # 5) 전환 + Ken Burns
    cluster_start_ids = {c["cluster_start_id"] for c in cards if c["source"] == "gps"}
    first_card = cards[0]["cluster_start_id"] if cards else None
    transitions, whip, cyc = [], 0, 0
    for i, s in enumerate(slides):
        if i == 0 or i < n_intro:
            t = "crossfade"
        elif i == n_intro:
            t = "zoom_through"
        elif s.id in cluster_start_ids and s.id != first_card:
            t = ("whip_left", "whip_right")[whip % 2]
            whip += 1
        else:
            t = AUTO_TRANSITION_CYCLE[cyc % len(AUTO_TRANSITION_CYCLE)]
            cyc += 1
        s.transition = t
        transitions.append(t)
        if is_photo(s) and (s.ken_burns or 0) < 20:
            s.ken_burns = 35 + (i * 17) % 40

    project.style_preset = o["style_preset"]
    project.default_slide_duration = sd
    if o.get("resolution") in ("720p", "1080p"):
        project.resolution = o["resolution"]
    project.stage = "scripted"
    project.updated_at = datetime.utcnow()
    db.commit()

    summary = {
        "project_id": project_id,
        "slides": n,
        "bgm": bgm,
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
        "stylize_ratio": float(o.get("stylize_ratio") or 0.0),   # Stage 2 훅 (미사용)
    }
    logger.info(f"[auto-edit] plan {project_id}: intro={n_intro} cards={len(cards)} "
                f"kinetic={len(kinetic)} narrated={narrated}")
    return summary


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
