#!/usr/bin/env python
"""
자동 편집 1단계 샘플 렌더 — 원본 프로젝트는 건드리지 않고 복제본으로 렌더한다.

  /workspace/venvs/docu/bin/python scripts/render_auto_edit_sample.py \
      --project 1856a4d7-0883-4a1e-9f19-006f26c5a02b \
      --clone-name "일월수목원 자동 편집 시안" \
      --bgm outputs/1856a4d7-0883-4a1e-9f19-006f26c5a02b/assets/bgm_flute_piano_variations.mp3 \
      --resolution 720p --out-dir /tmp/auto_edit_sample

과정: 프로젝트·슬라이드 DB 행 복제(새 id) → assets 파일 복사 → BGM 지정(bgm_<이름>) →
plan_auto_edit → worker.run_render → 프로젝트 id / 결과 경로 / 계획 요약을 JSON 으로 출력.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backend.core.config import OUTPUTS_DIR  # noqa: E402
from backend.db.models import Project, Slide  # noqa: E402
from backend.db.session import SessionLocal, init_db  # noqa: E402
from backend.services import worker  # noqa: E402
from backend.services.auto_edit import AutoEditError, plan_auto_edit  # noqa: E402

_SKIP_COLS = {"id", "created_at", "updated_at"}


def _columns(model) -> list[str]:
    return [c.name for c in model.__table__.columns]


def clone_project(db, src_id: str, name: str) -> str:
    src = db.query(Project).filter(Project.id == src_id).first()
    if not src:
        raise SystemExit(f"프로젝트 없음: {src_id}")
    new_id = str(uuid.uuid4())
    data = {c: getattr(src, c) for c in _columns(Project) if c not in _SKIP_COLS}
    data.update(id=new_id, name=name or f"{src.name} (자동 편집)", status="DRAFT",
                progress=0, message="", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(Project(**data))
    slides = (db.query(Slide).filter(Slide.project_id == src_id)
              .order_by(Slide.order_index).all())
    for s in slides:
        row = {c: getattr(s, c) for c in _columns(Slide) if c != "id"}
        row.update(id=str(uuid.uuid4()), project_id=new_id)
        db.add(Slide(**row))
    db.commit()

    # 자산 파일 복사 (최상위 파일만 — 렌더 캐시/미리보기 폴더는 제외)
    src_assets = OUTPUTS_DIR / src_id / "assets"
    dst_assets = OUTPUTS_DIR / new_id / "assets"
    dst_assets.mkdir(parents=True, exist_ok=True)
    copied = 0
    if src_assets.is_dir():
        for f in src_assets.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_assets / f.name)
                copied += 1
    print(f"[clone] {src_id} → {new_id} ({len(slides)} slides, {copied} files)", file=sys.stderr)
    return new_id


def main():
    ap = argparse.ArgumentParser(description="자동 편집 1단계 샘플 렌더 (프로젝트 복제본)")
    ap.add_argument("--project", required=True, help="원본 프로젝트 id")
    ap.add_argument("--clone-name", default="", help="복제 프로젝트 이름")
    ap.add_argument("--bgm", default="", help="BGM 파일 경로 (비우면 원본 프로젝트 BGM)")
    ap.add_argument("--resolution", choices=("720p", "1080p"), default="720p")
    ap.add_argument("--out-dir", default="", help="result.mp4 / plan.json 을 복사할 폴더")
    ap.add_argument("--intro-seconds", type=float, default=5.0)
    ap.add_argument("--slide-duration", type=float, default=3.0)
    ap.add_argument("--style-preset", default="cinematic")
    ap.add_argument("--no-cards", action="store_true", help="위치 카드 끄기")
    ap.add_argument("--no-kinetic", action="store_true", help="키네틱 타이포 끄기")
    ap.add_argument("--no-geocode", action="store_true", help="역지오코딩(네트워크) 끄기")
    ap.add_argument("--use-ai", action="store_true", help="키네틱 문구 Gemini 생성")
    ap.add_argument("--no-render", action="store_true", help="계획만 (렌더 생략)")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        new_id = clone_project(db, args.project, args.clone_name)
        opts = {
            "resolution": args.resolution,
            "intro_seconds": args.intro_seconds,
            "slide_duration": args.slide_duration,
            "style_preset": args.style_preset,
            "location_cards": not args.no_cards,
            "kinetic": not args.no_kinetic,
            "use_geocode": not args.no_geocode,
            "use_ai": args.use_ai,
            "narration": False,
            "bgm_path": str(Path(args.bgm).resolve()) if args.bgm else None,
        }
        try:
            summary = plan_auto_edit(db, new_id, opts)
        except AutoEditError as e:
            raise SystemExit(f"계획 실패: {e}")
    finally:
        db.close()

    result = OUTPUTS_DIR / new_id / "result.mp4"
    status, message, elapsed = "PLANNED", "", 0.0
    if not args.no_render:
        t0 = time.time()
        worker.run_render(new_id)
        elapsed = time.time() - t0
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == new_id).first()
            status, message = p.status, p.message
        finally:
            db.close()

    out = {
        "project_id": new_id,
        "status": status,
        "message": message,
        "result": str(result) if result.exists() else None,
        "render_seconds": round(elapsed, 1),
        "plan": summary,
    }
    if args.out_dir:
        d = Path(args.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        if result.exists():
            shutil.copy2(result, d / "result.mp4")
            out["copied_to"] = str(d / "result.mp4")
        (d / "plan.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if status in ("COMPLETED", "PLANNED") else 1


if __name__ == "__main__":
    sys.exit(main())
