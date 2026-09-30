"""
DocuMotion - 원클릭 자동 영상 파이프라인

구글 포토에서 사진을 고르면, 아래 전 과정을 한 번의 호출로 자동 처리한다:

  1. 자동 큐레이션 — 흐림/어두움/중복 컷 자동 제거 (제안 수락 단계 생략)
  2. 스마트 나레이션 — narration_ratio 비율만큼만 자막+TTS 생성.
     나머지는 use_tts=0, 자막 없이 BGM 위로 그냥 통과.
     나레이션 대상은 슬라이드 전체에 고르게 분산 (리듬감 유지).
  3. 자동 연출 — 슬라이드별 Ken Burns 강도 랜덤 + 전환 효과 순환 배정
  4. 렌더 — 기존 worker.run_render 재사용

개별 단계 API(큐레이션/나레이션/렌더)를 순서대로 손으로 호출하던
불편함을 없애는 것이 목적. 실패해도 파이프라인은 계속 진행하고
상태에 기록한다 (graceful degradation).
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime
from pathlib import Path

from backend.core.config import OUTPUTS_DIR, GOOGLE_API_KEY
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import SessionLocal
from backend.services import task_status
from backend.services import narration as narration_svc
from backend.services import worker as render_worker
from backend.services.curation import curate_photos
from backend.services.color_grade import VALID_STYLE_PRESETS

logger = get_logger(__name__)

TASK_KEY = "auto_video:{project_id}"

TRANSITION_CYCLE = ["crossfade", "fade_black", "slide_left", "slide_right"]


def _set(task_id: str, phase: str, progress: float, message: str = "", **extra):
    task_status.set_status(task_id, status="running", phase=phase,
                           progress=round(progress, 1), message=message, **extra)


def _auto_curate(db, project_id: str, assets_dir: Path, task_id: str) -> dict:
    """큐레이션 분석 → blurry/dark/duplicate 자동 삭제. 반환: {deleted, remaining}"""
    slides = (db.query(Slide).filter(Slide.project_id == project_id,
                                    Slide.slide_type != "video")
              .order_by(Slide.order_index).all())
    photos = [{"slide_id": s.id,
               "path": str(assets_dir / s.image_filename),
               "exif": s.exif}
              for s in slides if s.image_filename]
    if not photos:
        return {"deleted": 0, "remaining": db.query(Slide).filter(
            Slide.project_id == project_id).count()}

    result = curate_photos(photos)
    doomed = {sg["slide_id"] for sg in result.get("suggestions", [])}
    deleted = 0
    for s in slides:
        if s.id in doomed:
            if s.image_filename:
                p = assets_dir / s.image_filename
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    logger.warning(f"자산 삭제 실패: {p}")
            db.delete(s)
            deleted += 1
    db.commit()
    remaining = (db.query(Slide).filter(Slide.project_id == project_id)
                 .order_by(Slide.order_index).all())
    for idx, s in enumerate(remaining):
        s.order_index = idx
    db.commit()
    logger.info(f"[auto-video] curation: {deleted}장 제거, {len(remaining)}장 유지")
    return {"deleted": deleted, "remaining": len(remaining)}


def _pick_narration_targets(slides: list[Slide], ratio: float) -> set[str]:
    """나레이션을 달 슬라이드 id 집합. 전체에 고르게 분산."""
    ids = [s.id for s in slides if s.slide_type != "video" and s.image_filename]
    n = len(ids)
    if n == 0 or ratio <= 0:
        return set()
    count = max(1, math.ceil(n * min(ratio, 1.0)))
    if count >= n:
        return set(ids)
    # 고르게 분산: round(i * n / count)
    return {ids[round(i * n / count)] for i in range(count)}


def run_auto_video(project_id: str, opts: dict):
    """BackgroundTasks.add_task(run_auto_video, project_id, opts) 으로 호출."""
    task_id = TASK_KEY.format(project_id=project_id)
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            task_status.set_status(task_id, status="error", message="Project not found")
            return

        narration_ratio = float(opts.get("narration_ratio", 0.4))
        tone = opts.get("tone", "documentary")
        style_preset = opts.get("style_preset", "cinematic")
        auto_curate = opts.get("auto_curate", True)
        if style_preset not in VALID_STYLE_PRESETS:
            style_preset = "cinematic"

        assets_dir = OUTPUTS_DIR / project_id / "assets"
        _set(task_id, "starting", 2, "자동 영상 파이프라인 시작")

        # ── 1. 자동 큐레이션 (0 → 15%) ──
        curated = {"deleted": 0, "remaining": 0}
        if auto_curate:
            _set(task_id, "curating", 5, "흐림/중복 컷 자동 정리 중...")
            curated = _auto_curate(db, project_id, assets_dir, task_id)
        _set(task_id, "curating", 15,
             f"큐레이션 완료: {curated['deleted']}장 제거", **curated)

        slides = (db.query(Slide).filter(Slide.project_id == project_id)
                  .order_by(Slide.order_index).all())
        if not slides:
            task_status.set_status(task_id, status="error",
                                   message="슬라이드가 없습니다")
            return

        # ── 2+3. 스마트 나레이션 (15 → 55%) ──
        targets = _pick_narration_targets(slides, narration_ratio)
        _set(task_id, "narrating", 18,
             f"나레이션 계획: {len(targets)}/{len(slides)}장")
        done = failed = silent = 0
        can_narrate = bool(GOOGLE_API_KEY)
        if not can_narrate:
            logger.warning("[auto-video] GOOGLE_API_KEY 없음 — 전체 무나레이션으로 진행")
        for i, s in enumerate(slides):
            if s.id in targets and can_narrate and s.image_filename:
                try:
                    text = narration_svc.generate_narration_for_image(
                        assets_dir / s.image_filename,
                        project_title=project.name or "",
                        tone=tone)
                except Exception as e:
                    logger.warning(f"나레이션 실패({s.id}): {e}")
                    text = ""
                if text:
                    s.text = text
                    s.use_tts = 1
                    done += 1
                else:
                    s.text = ""
                    s.use_tts = 0
                    failed += 1
            else:
                # 자막 없이 그냥 통과 — BGM만 깔림
                s.text = ""
                s.use_tts = 0
                s.subtitles = "[]"
                silent += 1
            frac = 18 + 37 * (i + 1) / max(len(slides), 1)
            _set(task_id, "narrating", frac,
                 f"나레이션 생성 중... ({i + 1}/{len(slides)})")
            db.commit()
            time.sleep(0.3)  # Gemini RPM 절약
        _set(task_id, "narrating", 55,
             f"나레이션 완료: {done}장, 무자막 통과: {silent}장",
             narrated=done, silent=silent, failed=failed)

        # ── 4. 자동 연출 (55 → 60%) ──
        _set(task_id, "directing", 57, "자동 연출 적용 중...")
        img_slides = [s for s in slides if s.slide_type != "video"]
        for i, s in enumerate(img_slides):
            s.ken_burns = random.randint(30, 80)
            s.transition = TRANSITION_CYCLE[i % len(TRANSITION_CYCLE)]
        project.style_preset = style_preset
        project.stage = "scripted"
        project.updated_at = datetime.utcnow()
        db.commit()
        _set(task_id, "directing", 60,
             f"연출 적용: {style_preset}, Ken Burns·전환 자동 배정")

        # ── 5. 렌더 (60 → 100%: worker가 project.progress 관리) ──
        _set(task_id, "rendering", 62, "렌더링 시작...")
        db.close()
        render_worker.run_render(project_id)
        db = SessionLocal()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project and project.status == "COMPLETED":
            task_status.set_status(task_id, status="done", phase="done",
                                   progress=100.0, message="자동 영상 완성")
        else:
            task_status.set_status(
                task_id, status="error", phase="rendering",
                message=f"렌더 실패: {getattr(project, 'message', '')}")
    except Exception as e:
        logger.error(f"[auto-video] 파이프라인 실패: {e}")
        task_status.set_status(task_id, status="error", message=str(e)[:300])
    finally:
        db.close()
