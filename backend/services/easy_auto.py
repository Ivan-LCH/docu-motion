"""
DocuMotion - 간편 자동 모드 (가져오기 → 장소·느낌 자막 → 음악 → 자동 렌더)

순수 함수 (테스트 대상):
  decide_fit / rank_candidates / candidate_duration_range — 곡 길이 T 와 영상 길이 L 맞추기
    |T−L|/L ≤ 15% → adjust : 사진 슬라이드 길이를 늘리거나 줄여 영상이 곡과 함께 끝남
    T > L          → trim   : 앞에서 L 만큼 쓰고 끝 3초 페이드 아웃
    T < L          → loop   : 1.5초 크로스페이드로 반복, 끝 3초 페이드 아웃
  solve_photo_scale — 사진 길이 배율 (렌더러와 같은 presets.plan_timeline 으로 이분 탐색)
  easy_plan_opts — 간편 자동 기본값 (그림→실사 30%, 3.5초, 1080p, 시네마틱, 인트로)

DB/네트워크:
  estimate_length — 계획을 dry-run(rollback) 해 실제 전환·홀드가 반영된 L 추정
  fit_music       — 계획 뒤 meta.easy_duration / bgm_fit / bgm_fade_out / bgm_loop_xfade 기록
  choose_track    — Jamendo 곡 다운로드(허용 곡만) → 프로젝트 BGM + 크레딧(meta.bgm_credit, music_credit.txt)
  run_easy_render — 백그라운드: (선별) → 계획 → 음악 맞춤 → worker.run_render
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from backend.core import config
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.services import auto_edit, music_search, task_status
from backend.services.media_import import media_duration, video_duration_fn
from backend.services.music_credit import credit_description
from backend.services.presets import is_free_length, plan_timeline, project_meta

logger = get_logger(__name__)

TASK_KEY = "easy_auto:{project_id}"
FIT_TOLERANCE = 0.15
EASY_FADE_OUT = 3.0
EASY_LOOP_XFADE = 1.5
MAX_TRACK_BYTES = 60 * 1024 ** 2
EASY_DEFAULTS: dict = {
    "resolution": "1080p",
    "stylize_ratio": 0.3,
    "slide_duration": 3.5,
    "auto_curate": True,
}


class EasyAutoError(ValueError):
    """사용자에게 그대로 보여줄 오류 (status: HTTP 코드)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ────────────────────────────────────────────────────────────────────────── #
# 순수 함수
# ────────────────────────────────────────────────────────────────────────── #
def fmt_mmss(sec: float) -> str:
    sec = max(0, int(round(float(sec or 0))))
    return f"{sec // 60}:{sec % 60:02d}"


def decide_fit(track: float, length: float, tolerance: float = FIT_TOLERANCE) -> dict:
    """곡 길이 T, 영상 길이 L → {mode, scale, label}. scale 은 adjust 일 때 사진 길이 배율 (T/L 근사)."""
    T, L = float(track or 0), float(length or 0)
    if T <= 0 or L <= 0:
        return {"mode": "trim", "scale": 1.0, "label": "길이 정보 없음 · 끝 3초 페이드"}
    diff = (T - L) / L
    if abs(diff) <= tolerance:
        pct = round(diff * 100)
        label = "딱 맞음" if pct == 0 else f"딱 맞음 · 슬라이드 시간 {pct:+d}%"
        return {"mode": "adjust", "scale": round(T / L, 4), "label": label}
    if T > L:
        return {"mode": "trim", "scale": 1.0, "label": f"길음 · 앞 {fmt_mmss(L)}만 사용 + 3초 페이드"}
    repeats = max(1, math.ceil(L / T) - 1)
    return {"mode": "loop", "scale": 1.0, "label": f"짧음 · 크로스페이드 루프 {repeats}회 + 3초 페이드"}


def candidate_duration_range(length: float) -> tuple[int, int]:
    """Jamendo durationbetween: max(30, L×0.6) ~ L×2.5 (정수 초)."""
    lo = int(max(30.0, float(length) * 0.6))
    hi = int(max(lo + 30, math.ceil(float(length) * 2.5)))
    return lo, hi


def rank_candidates(hits: list[dict], length: float, k: int = 4) -> list[dict]:
    """다운로드 허용 곡만, 길이 적합도 순: adjust(|T−L| 작은 순) → trim(짧게 남는 순) → loop(긴 순).
    각 후보에 fit {mode, scale, label} 을 붙인다. 같은 id 는 한 번만."""
    order = {"adjust": 0, "trim": 1, "loop": 2}
    seen, scored = set(), []
    for h in hits:
        if not h.get("audiodownload_allowed") or not h.get("audiodownload"):
            continue
        hid = str(h.get("id"))
        if hid in seen:
            continue
        seen.add(hid)
        fit = decide_fit(h.get("duration") or 0, length)
        scored.append((order[fit["mode"]], abs(float(h.get("duration") or 0) - length), {**h, "fit": fit}))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in scored[:k]]


def easy_plan_opts(settings: Optional[dict] = None, seed: int = 0) -> dict:
    st = {**EASY_DEFAULTS, **{k: v for k, v in (settings or {}).items() if v is not None}}
    return {
        "easy": True, "seed": int(seed or 0),
        "music_intro": True, "intro_seconds": 5.0, "kinetic": False, "location_cards": True,
        "narration": False, "use_ai": False, "style_preset": "cinematic",
        "slide_duration": float(st["slide_duration"]),
        "resolution": st["resolution"] if st["resolution"] in ("720p", "1080p") else None,
        "stylize_ratio": max(0.0, min(0.5, float(st["stylize_ratio"]))),
        "stylize_effect": "auto",
    }


def is_photo_item(item: dict) -> bool:
    return (item.get("slide_type") or "image") == "image" and bool(item.get("image_filename")) \
        and is_free_length(item)


def solve_photo_scale(items: list[dict], default: float, project_duration: float, target: float,
                      video_duration=None, lo: float = 0.6, hi: float = 1.6) -> tuple[float, float]:
    """사진(자유 길이 이미지) 슬라이드 길이 × scale 로 타임라인 총 길이가 target 에 가장 가깝게.
    홀드·전환 overlap 까지 plan_timeline 으로 계산 (렌더러와 같은 규칙). 반환: (scale, 총 길이)."""
    photos = [i for i, it in enumerate(items) if is_photo_item(it)]

    def total(scale: float) -> float:
        its = []
        for i, it in enumerate(items):
            if i in photos:
                m = _meta_of(it)
                m["easy_duration"] = round(default * scale, 4)
                it = {**it, "meta": json.dumps(m)}
            its.append(it)
        return plan_timeline(its, default, project_duration, video_duration)["total"]

    if not photos:
        return 1.0, total(1.0)
    a, b = lo, hi
    if total(a) >= target:
        return a, total(a)
    if total(b) <= target:
        return b, total(b)
    for _ in range(40):
        mid = (a + b) / 2
        if total(mid) < target:
            a = mid
        else:
            b = mid
    return b, total(b)


def _meta_of(item) -> dict:
    raw = item.get("meta") if isinstance(item, dict) else getattr(item, "meta", None)
    try:
        m = json.loads(raw or "{}")
        return dict(m) if isinstance(m, dict) else {}
    except Exception:
        return {}


# ────────────────────────────────────────────────────────────────────────── #
# DB 헬퍼
# ────────────────────────────────────────────────────────────────────────── #
def _slides(db, project_id: str) -> list:
    return db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()


def _project(db, project_id: str) -> Project:
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise EasyAutoError("Project not found", 404)
    return p


def _items(slides: list) -> list[dict]:
    from backend.services.preview import slide_to_item
    return [slide_to_item(s) for s in slides]


def easy_settings(slides: list) -> dict:
    st = project_meta(slides).get("easy_settings")
    return {**EASY_DEFAULTS, **(st if isinstance(st, dict) else {})}


def current_seed(slides: list) -> int:
    plan = project_meta(slides).get("auto_plan")
    return int(plan.get("seed") or 0) if isinstance(plan, dict) else 0


def timeline_of(db, project: Project, slides: list) -> dict:
    return plan_timeline(_items(slides), project.default_slide_duration or 3.0,
                         project.transition_duration or 0.7,
                         video_duration_fn(Path(config.OUTPUTS_DIR) / project.id / "assets"))


def estimate_length(db, project_id: str, slide_duration: Optional[float] = None,
                    settings: Optional[dict] = None) -> dict:
    """간편 자동 계획을 dry-run(rollback) 해서 영상 길이 L 추정 (음악 맞춤 전 자연 길이)."""
    project = _project(db, project_id)
    slides = _slides(db, project_id)
    media = [s for s in slides if auto_edit.is_media(s)]
    if not media:
        return {"length": 0.0, "photos": 0, "videos": 0, "video_seconds": 0.0,
                "slide_duration": float(slide_duration or EASY_DEFAULTS["slide_duration"])}
    st = {**easy_settings(slides), **(settings or {})}
    if slide_duration:
        st["slide_duration"] = float(slide_duration)
    opts = {**easy_plan_opts(st, current_seed(slides)), "require_bgm": False}
    try:
        auto_edit.plan_auto_edit(db, project_id, opts, commit=False)
        slides = _slides(db, project_id)
        project = _project(db, project_id)
        tl = timeline_of(db, project, slides)
        items = _items(slides)
    finally:
        db.rollback()
    vids = [d for d, it in zip(tl["durations"], items) if it.get("slide_type") == "video"]
    return {"length": round(tl["total"], 2), "photos": sum(1 for it in items if is_photo_item(it)),
            "videos": len(vids), "video_seconds": round(sum(vids), 2),
            "slide_duration": float(st["slide_duration"])}


def fit_music(db, project_id: str) -> dict:
    """계획 후 호출: BGM 길이 T 와 영상 길이 L 비교 → adjust 면 사진 meta.easy_duration 기록.
    slides[0].meta 에 bgm_fit / bgm_fade_out(3.0) / bgm_loop_xfade(1.5) 기록. 커밋한다."""
    project = _project(db, project_id)
    slides = _slides(db, project_id)
    if not slides:
        raise EasyAutoError("슬라이드가 없습니다")
    for s in slides:                                  # 이전 맞춤 제거 → 자연 길이 L 부터
        m = _meta_of(s)
        if "easy_duration" in m or "easy_duration" in (m.get("auto_fields") or []):
            m.pop("easy_duration", None)
            m["auto_fields"] = [f for f in (m.get("auto_fields") or []) if f != "easy_duration"]
            s.meta = json.dumps(m, ensure_ascii=False)
    db.flush()
    assets = Path(config.OUTPUTS_DIR) / project_id / "assets"
    bgm = (project.bgm_filename or "").strip()
    T = media_duration(assets / bgm) if bgm and (assets / bgm).is_file() else None
    pm = project_meta(slides)
    if not T and isinstance(pm.get("bgm_credit"), dict):
        T = float(pm["bgm_credit"].get("duration") or 0) or None
    default = project.default_slide_duration or 3.0
    td = project.transition_duration or 0.7
    vfn = video_duration_fn(assets)
    items = _items(slides)
    L = plan_timeline(items, default, td, vfn)["total"]
    fit = decide_fit(T or 0, L)
    final_len = L
    scale = 1.0
    if fit["mode"] == "adjust" and T:
        scale, final_len = solve_photo_scale(items, default, td, T, vfn)
        if abs(final_len - T) > 0.6:                 # 사진이 적어 다 못 맞추면 남는 쪽 방식(trim/loop)으로
            fit = decide_fit(T, final_len, tolerance=0.0)
        for s, it in zip(slides, items):
            if is_photo_item(it):
                m = _meta_of(s)
                m["easy_duration"] = round(default * scale, 4)
                auto_edit._add_auto(m, "easy_duration")
                s.meta = json.dumps(m, ensure_ascii=False)
    result = {"mode": fit["mode"], "scale": round(scale, 4), "label": fit["label"],
              "track_duration": round(T or 0, 2), "video_length": round(final_len, 2),
              "natural_length": round(L, 2)}
    auto_edit.set_project_meta(slides, bgm_fit=result, bgm_fade_out=EASY_FADE_OUT,
                               bgm_loop_xfade=EASY_LOOP_XFADE,
                               show_bgm_credit=pm.get("show_bgm_credit", True))
    db.commit()
    logger.info(f"[easy] fit {project_id}: T={T} L={L:.2f} → {result}")
    return result


def credit_text(db, project_id: str) -> dict:
    slides = _slides(db, project_id)
    credit = project_meta(slides).get("bgm_credit")
    credit = credit if isinstance(credit, dict) else None
    return {"text": credit_description(credit), "credit": credit}


# ────────────────────────────────────────────────────────────────────────── #
# 음악 후보 / 선택 (async — httpx)
# ────────────────────────────────────────────────────────────────────────── #
def context_text(project: Project, slides: list) -> str:
    from backend.api.v1.projects import bgm_context_text
    return bgm_context_text(project, slides)


async def music_candidates(db, project_id: str, keywords: Optional[list] = None,
                           query: Optional[str] = None, slide_duration: Optional[float] = None) -> dict:
    project = _project(db, project_id)
    est = estimate_length(db, project_id, slide_duration)
    L = float(est["length"]) or 60.0
    slides = _slides(db, project_id)
    kws = [music_search._clean_keyword(k) for k in (keywords or []) if str(k).strip()]
    kws = [k for k in kws if k]
    source = "user" if kws else ""
    if not kws:
        kws, source = await music_search.ai_keywords(context_text(project, slides))
    tr = {"query_used": " ".join(kws), "translated": False, "translate_source": "none"}
    if query and query.strip():
        tr = await music_search.translate_query(query)
    q = tr["query_used"]
    rng = candidate_duration_range(L)
    hits, _ = await music_search.jamendo_search(q, limit=30, instrumental=True, duration_range=rng)
    ranked = rank_candidates(hits, L)
    broadened = False
    if len(ranked) < 3 and not (query and query.strip()) and len(kws) > 1:
        more, _ = await music_search.jamendo_search(
            "", limit=30, instrumental=True, duration_range=rng,
            fuzzytags="+".join(w for k in kws for w in k.split())[:100])
        ranked = rank_candidates(hits + more, L)
        hits = hits + more
        broadened = True
    if len(ranked) < 3:                              # 길이 조건을 빼고 넓게 다시
        more, _ = await music_search.jamendo_search(q, limit=30, instrumental=True)
        ranked = rank_candidates(hits + more, L)
        broadened = True
    return {"length": round(L, 2), "estimate": est, "keywords": kws, "keywords_source": source or "user",
            **tr, "duration_range": list(rng), "broadened": broadened, "empty": not ranked,
            "candidates": ranked}


def _bgm_filename(track_id) -> str:
    safe = "".join(c for c in str(track_id) if c.isalnum())[:20] or "track"
    return f"bgm_jamendo_{safe}.mp3"


async def _download(url: str, dest: Path, max_bytes: int = MAX_TRACK_BYTES) -> int:
    n = 0
    tmp = dest.with_suffix(".part")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise EasyAutoError(f"음악 파일 다운로드 실패 ({resp.status_code})", 502)
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        n += len(chunk)
                        if n > max_bytes:
                            raise EasyAutoError("음악 파일이 너무 큽니다", 413)
                        f.write(chunk)
        if n < 1000:
            raise EasyAutoError("음악 파일이 비어 있습니다", 502)
        tmp.replace(dest)
        return n
    finally:
        if tmp.exists():
            tmp.unlink()


async def choose_track(db, project_id: str, track: dict) -> dict:
    """곡 선택: Jamendo 에서 id 로 다시 조회(다운로드 허용·URL 확인) → 다운로드 → 프로젝트 BGM.
    크레딧은 slides[0].meta.bgm_credit + outputs/<id>/music_credit.txt."""
    project = _project(db, project_id)
    tid = (track or {}).get("id")
    if tid in (None, ""):
        raise EasyAutoError("곡 정보가 없습니다")
    hits, _ = await music_search.jamendo_search(track_id=tid, limit=1)
    hit = next((h for h in hits if str(h.get("id")) == str(tid)), None)
    if hit is None:
        raise EasyAutoError("Jamendo 에서 곡을 찾을 수 없습니다", 404)
    if not hit.get("audiodownload_allowed") or not hit.get("audiodownload"):
        raise EasyAutoError("이 곡은 다운로드가 허용되지 않아 쓸 수 없어요. 다른 곡을 골라주세요")
    assets = Path(config.OUTPUTS_DIR) / project_id / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    name = _bgm_filename(tid)
    await _download(hit["audiodownload"], assets / name)
    old = (project.bgm_filename or "").strip()
    if old and old != name and (assets / old).is_file():
        try:
            (assets / old).unlink()
        except OSError:
            pass
    project.bgm_filename = name
    project.updated_at = datetime.utcnow()
    credit = {"title": hit.get("title", ""), "artist": hit.get("artist", ""),
              "license_url": hit.get("license_url", ""), "page_url": hit.get("page_url", ""),
              "jamendo_id": tid, "duration": hit.get("duration") or 0}
    slides = _slides(db, project_id)
    if slides:
        auto_edit.consolidate_project_meta(slides)
        auto_edit.set_project_meta(slides, bgm_credit=credit, bgm_fit=None)
    db.commit()
    text = credit_description(credit)
    (Path(config.OUTPUTS_DIR) / project_id / "music_credit.txt").write_text(text + "\n", encoding="utf-8")
    logger.info(f"[easy] track chosen {project_id}: {credit['title']} ({tid})")
    return {"ok": True, "bgm_filename": name, "credit": credit, "credit_text": text}


# ────────────────────────────────────────────────────────────────────────── #
# 파이프라인 (백그라운드)
# ────────────────────────────────────────────────────────────────────────── #
def has_bgm(project: Project) -> bool:
    name = (project.bgm_filename or "").strip()
    return bool(name) and (Path(config.OUTPUTS_DIR) / project.id / "assets" / name).is_file()


def prepare(db, project_id: str, settings: Optional[dict] = None, seed: Optional[int] = None,
            curate: bool = False, task_id: Optional[str] = None) -> dict:
    """(선별) → 간편 자동 계획 → 음악 맞춤. 렌더 직전 상태까지 DB 반영. 반환: {plan, fit, curation}."""
    project = _project(db, project_id)
    if not has_bgm(project):
        raise EasyAutoError("음악을 먼저 골라주세요 (BGM 필요)")
    slides = _slides(db, project_id)
    pm = auto_edit.consolidate_project_meta(slides)
    st = {**easy_settings(slides), **{k: v for k, v in (settings or {}).items() if v is not None}}
    curation = None
    if curate:
        if task_id:
            task_status.set_status(task_id, status="running", phase="curating", progress=5.0,
                                   message="흐림·중복 컷 고르는 중...")
        from backend.services.auto_video import _auto_curate
        protect = {s.id for s in slides
                   if any(str(_meta_of(s).get(k) or "").strip() for k in ("easy_place", "easy_feel"))
                   or auto_edit.is_locked(_meta_of(s))}
        curation = _auto_curate(db, project_id, Path(config.OUTPUTS_DIR) / project_id / "assets",
                                task_id or "", protect_ids=protect)
        slides = _slides(db, project_id)
        if slides:                                     # 첫 슬라이드가 지워졌어도 프로젝트 meta 유지
            auto_edit.set_project_meta(slides, **{k: v for k, v in pm.items()})
            db.commit()
    if not slides:
        raise EasyAutoError("슬라이드가 없습니다")
    if seed is None:
        seed = current_seed(slides)
    if task_id:
        task_status.set_status(task_id, status="running", phase="planning", progress=12.0,
                               message="장소 카드·전환·그림 효과 정하는 중...")
    plan = auto_edit.plan_auto_edit(db, project_id, easy_plan_opts(st, seed))
    slides = _slides(db, project_id)
    auto_edit.set_project_meta(slides, easy_settings={
        "resolution": st["resolution"], "stylize_ratio": float(st["stylize_ratio"]),
        "slide_duration": float(st["slide_duration"]), "auto_curate": bool(st.get("auto_curate"))})
    db.commit()
    if task_id:
        task_status.set_status(task_id, status="running", phase="fitting", progress=18.0,
                               message="음악 길이에 맞추는 중...", summary=plan)
    fit = fit_music(db, project_id)
    return {"plan": plan, "fit": fit, "curation": curation}


def run_easy_render(project_id: str, opts: dict):
    """BackgroundTasks.add_task(run_easy_render, project_id, opts)."""
    from backend.db.session import SessionLocal
    from backend.services import worker as render_worker

    task_id = TASK_KEY.format(project_id=project_id)
    db = SessionLocal()
    try:
        settings = {k: opts.get(k) for k in ("resolution", "stylize_ratio", "slide_duration", "auto_curate")}
        try:
            out = prepare(db, project_id, settings, seed=opts.get("seed"),
                          curate=bool(opts.get("auto_curate", EASY_DEFAULTS["auto_curate"])),
                          task_id=task_id)
        except (EasyAutoError, auto_edit.AutoEditError) as e:
            task_status.set_status(task_id, status="error", phase="planning", message=str(e))
            return
        summary = {**out["plan"], "fit": out["fit"], "curation": out["curation"]}
        project = db.query(Project).filter(Project.id == project_id).first()
        project.status, project.progress, project.message = "QUEUED", 0, "간편 자동 렌더 대기"
        db.commit()
        db.close()
        task_status.set_status(task_id, status="running", phase="rendering", progress=20.0,
                               message="렌더링 시작...", summary=summary)
        render_worker.run_render(project_id)
        db = SessionLocal()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project and project.status == "COMPLETED":
            task_status.set_status(task_id, status="done", phase="done", progress=100.0,
                                   message="영상 완성")
        else:
            task_status.set_status(task_id, status="error", phase="rendering",
                                   message=f"렌더 실패: {getattr(project, 'message', '')}")
    except Exception as e:
        logger.error(f"[easy] 실패: {e}", exc_info=True)
        task_status.set_status(task_id, status="error", message=str(e)[:300])
    finally:
        db.close()
