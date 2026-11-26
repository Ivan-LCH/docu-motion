"""
DocuMotion - 슬라이드 순서 변경 · 삭제 후 정리 (간편 자동 ① 가져오기 그리드)

  rehome_project_meta — 프로젝트 단위 meta(첫 슬라이드에 사는 키)를 새 첫 슬라이드로 옮긴다
  reorder_slides      — 전체 id 목록 그대로 order_index 0..n-1 재기록 + meta 이동 + user_ordered
  compact_after_delete — 삭제 뒤 order_index 0..n-1 + meta 이동 + 길이 맞춤 무효화
  invalidate_fit      — 슬라이드 수·길이에 따라 정해진 값(bgm_fit, 사진 easy_duration) 제거.
                        고른 곡(bgm_filename·bgm_credit)은 그대로 — 다음 렌더가 길이를 다시 맞춘다.
"""
from __future__ import annotations

import json

from backend.db.models import Slide
from backend.services import task_status
from backend.services.presets import PROJECT_META_KEYS

# 렌더러가 slides[0] 에서만 읽는 위치 키 (첫 슬라이드가 바뀌면 같이 옮긴다)
FIRST_SLIDE_META_KEYS: tuple[str, ...] = (*PROJECT_META_KEYS, "bgm_fade_in")
EASY_TASK_KEY = "easy_auto:{project_id}"


class SlideOrderError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _meta(s) -> dict:
    try:
        m = json.loads(s.meta or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _set_meta(s, m: dict) -> None:
    s.meta = json.dumps(m, ensure_ascii=False)


def _slides(db, project_id: str) -> list:
    return db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()


def collect_first_meta(slides: list) -> dict:
    """앞에서부터 처음 나오는 값 (presets.project_meta 와 같은 규칙)."""
    out: dict = {}
    for s in slides:
        m = _meta(s)
        for k in FIRST_SLIDE_META_KEYS:
            if k not in out and k in m:
                out[k] = m[k]
    return out


def rehome_project_meta(slides: list, pm: dict | None = None) -> dict:
    """slides(새 순서)의 첫 슬라이드에 프로젝트 meta 를 두고 나머지에서는 지운다.
    pm: 삭제된 슬라이드에서 미리 모은 값 (None → slides 에서 모음). 슬라이드별 키는 건드리지 않는다."""
    if pm is None:
        pm = collect_first_meta(slides)
    for i, s in enumerate(slides):
        m = _meta(s)
        before = dict(m)
        for k in FIRST_SLIDE_META_KEYS:
            if i == 0 and k in pm:
                m[k] = pm[k]
            else:
                m.pop(k, None)
        if m != before:
            _set_meta(s, m)
    return pm


def invalidate_fit(slides: list) -> None:
    """음악 길이 맞춤 결과 제거 → 다음 렌더(easy_auto.prepare)가 계획·길이·효과를 다시 계산."""
    for i, s in enumerate(slides):
        m = _meta(s)
        changed = False
        if "easy_duration" in m:
            m.pop("easy_duration")
            changed = True
        af = m.get("auto_fields")
        if isinstance(af, list) and "easy_duration" in af:
            m["auto_fields"] = [f for f in af if f != "easy_duration"]
            changed = True
        if i == 0 and "bgm_fit" in m:
            m.pop("bgm_fit")
            changed = True
        if changed:
            _set_meta(s, m)


def easy_render_running(project_id: str) -> bool:
    st = task_status.get_status(EASY_TASK_KEY.format(project_id=project_id))
    return bool(st and st.get("status") == "running")


def reorder_slides(db, project_id: str, slide_ids: list[str]) -> list:
    """slide_ids 가 프로젝트 슬라이드 전체와 정확히 같아야 한다 (중복·누락·남의 id → 400)."""
    slides = _slides(db, project_id)
    by_id = {s.id: s for s in slides}
    ids = [str(i) for i in slide_ids]
    if len(ids) != len(set(ids)):
        raise SlideOrderError("중복된 슬라이드가 있습니다")
    if set(ids) != set(by_id):
        raise SlideOrderError("슬라이드 목록이 맞지 않습니다. 새로고침 후 다시 시도하세요")
    if easy_render_running(project_id):
        raise SlideOrderError("렌더링 중에는 순서를 바꿀 수 없습니다", 409)
    pm = collect_first_meta(slides)
    ordered = [by_id[i] for i in ids]
    changed = [s.id for s in slides] != ids
    for idx, s in enumerate(ordered):
        s.order_index = idx
    if changed:
        pm["user_ordered"] = True
    rehome_project_meta(ordered, pm)
    db.commit()
    return ordered


def compact_after_delete(db, project_id: str, pm: dict) -> list:
    """삭제 커밋 전에 호출: 남은 슬라이드 order_index 0..n-1, 프로젝트 meta 이동, 길이 맞춤 무효화."""
    remaining = _slides(db, project_id)
    for idx, s in enumerate(remaining):
        s.order_index = idx
    rehome_project_meta(remaining, pm)
    invalidate_fit(remaining)
    return remaining
