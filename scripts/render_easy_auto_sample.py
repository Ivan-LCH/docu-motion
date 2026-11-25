#!/usr/bin/env python
"""
간편 자동 모드 샘플 렌더 — 원본 프로젝트는 건드리지 않고 복제본으로 렌더한다.

  /workspace/venvs/docu/bin/python scripts/render_easy_auto_sample.py \
      --project 1856a4d7-0883-4a1e-9f19-006f26c5a02b \
      --clone-name "일월수목원 간편 자동 시안" --resolution 720p --out-dir /tmp/easy_sample \
      --captions captions.json  [--track-json track.json | --search "잔잔한 가을 산책"] [--bgm song.mp3]

  captions.json: [{"index": 1, "place": "장미 정원", "feel": "천천히 걷기"}, ...]  (index = 슬라이드 순서)
  음악: --bgm(로컬 파일) > --track-json(Jamendo 검색 결과 hit 1개) > --search/AI 키워드 후보 중 첫 곡.
        Jamendo 는 JAMENDO_CLIENT_ID 환경변수가 있어야 한다 (값은 출력하지 않음).

과정: 복제 → 자막 기록 → 음악 선택 → 계획(easy) → 음악 길이 맞춤 → worker.run_render → JSON 요약.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import config  # noqa: E402
from backend.db.models import Project  # noqa: E402
from backend.db.session import SessionLocal, init_db  # noqa: E402
from backend.services import auto_edit, easy_auto, worker  # noqa: E402
from backend.services.presets import to_real_hold_seconds, to_real_reveal_end  # noqa: E402
from scripts.render_auto_edit_sample import clone_project  # noqa: E402


def _apply_captions(db, project_id: str, path: str) -> int:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    slides = easy_auto._slides(db, project_id)
    n = 0
    for r in rows:
        i = int(r.get("index", -1))
        if not 0 <= i < len(slides):
            continue
        m = easy_auto._meta_of(slides[i])
        for key, val in (("easy_place", r.get("place")), ("easy_feel", r.get("feel"))):
            if str(val or "").strip():
                m[key] = str(val).strip()
        slides[i].meta = json.dumps(m, ensure_ascii=False)
        n += 1
    db.commit()
    return n


def _choose_music(db, project_id: str, args) -> dict:
    if args.bgm:
        auto_edit.ensure_bgm(db, db.query(Project).get(project_id), str(Path(args.bgm).resolve()))
        return {"source": "local", "file": Path(args.bgm).name}
    if not config.JAMENDO_CLIENT_ID:
        raise SystemExit("음악 없음: --bgm 을 주거나 JAMENDO_CLIENT_ID 환경변수를 설정하세요")
    if args.track_json:
        track = json.loads(Path(args.track_json).read_text(encoding="utf-8"))
    else:
        res = asyncio.run(easy_auto.music_candidates(db, project_id, query=args.search or None,
                                                     slide_duration=args.slide_duration))
        if not res["candidates"]:
            raise SystemExit(f"후보 없음 (query={res['query_used']!r})")
        track = res["candidates"][0]
        print(f"[music] {res['query_used']!r} → {track['title']} ({track['duration']}s, "
              f"{track['fit']['label']})", file=sys.stderr)
    out = asyncio.run(easy_auto.choose_track(db, project_id, track))
    return {"source": "jamendo", "credit": out["credit"], "credit_text": out["credit_text"]}


def main():
    ap = argparse.ArgumentParser(description="간편 자동 모드 샘플 렌더 (프로젝트 복제본)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--clone-name", default="")
    ap.add_argument("--resolution", choices=("720p", "1080p"), default="720p")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--captions", default="", help='[{"index", "place", "feel"}] JSON 파일')
    ap.add_argument("--track-json", default="")
    ap.add_argument("--search", default="")
    ap.add_argument("--bgm", default="")
    ap.add_argument("--slide-duration", type=float, default=3.5)
    ap.add_argument("--stylize-ratio", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        pid = clone_project(db, args.project, args.clone_name or "간편 자동 시안")
        captioned = _apply_captions(db, pid, args.captions) if args.captions else 0
        music = _choose_music(db, pid, args)
        prep = easy_auto.prepare(db, pid, {"resolution": args.resolution, "slide_duration": args.slide_duration,
                                           "stylize_ratio": args.stylize_ratio, "auto_curate": False},
                                 seed=args.seed)
        project = db.query(Project).get(pid)
        slides = easy_auto._slides(db, pid)
        tl = easy_auto.timeline_of(db, project, slides)
        items = easy_auto._items(slides)
        stylized = []
        for k, it in enumerate(items):
            hold = to_real_hold_seconds(it)
            if hold > 0:
                base = tl["reveal_bases"][k] or tl["durations"][k]
                stylized.append({"index": k, "effect": it["photo_effect"],
                                 "reveal_end": round(tl["starts"][k] + to_real_reveal_end(base, it["effect_reveal"]), 2),
                                 "hold": hold, "duration": round(tl["durations"][k], 2),
                                 "extended_from": tl["reveal_bases"][k]})
    finally:
        db.close()

    status, message, elapsed = "PLANNED", "", 0.0
    result = config.OUTPUTS_DIR / pid / "result.mp4"
    if not args.no_render:
        t0 = time.time()
        worker.run_render(pid)
        elapsed = time.time() - t0
        db = SessionLocal()
        try:
            p = db.query(Project).get(pid)
            status, message = p.status, p.message
        finally:
            db.close()

    fit = prep["fit"]
    out = {
        "project_id": pid, "status": status, "message": message,
        "captioned_slides": captioned, "music": music,
        "length_estimate": fit["natural_length"], "track_duration": fit["track_duration"],
        "fit_mode": fit["mode"], "fit_label": fit["label"], "scale": fit["scale"],
        "video_length": round(tl["total"], 2),
        "slide_durations": [round(d, 2) for d in tl["durations"]],
        "transitions": [it.get("transition") for it in items],
        "cards": [{"order_index": c["order_index"], "title": c["title"], "sub": c["sub"]}
                  for c in prep["plan"]["cards"]],
        "stylized": stylized,
        "result": str(result) if result.exists() else None,
        "render_seconds": round(elapsed, 1),
    }
    if args.out_dir:
        d = Path(args.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        if result.exists():
            shutil.copy2(result, d / "result.mp4")
            out["copied_to"] = str(d / "result.mp4")
        credit = config.OUTPUTS_DIR / pid / "music_credit.txt"
        if credit.exists():
            shutil.copy2(credit, d / "music_credit.txt")
        (d / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if status in ("COMPLETED", "PLANNED") else 1


if __name__ == "__main__":
    sys.exit(main())
