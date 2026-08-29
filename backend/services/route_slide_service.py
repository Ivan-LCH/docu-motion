"""
DocuMotion - 경로 슬라이드 생성 서비스 (Photo-Vlog F3 리팩터링)

api/v1/slides.py 의 route 생성 엔드포인트 본문을 추출한 것. 기존 엔드포인트와
F3 organize(자동 구성) 경로 삽입 양쪽에서 호출된다. 동작은 추출 전과 동일.

origin/destination: 문자열이면 geocode, {lat,lng[,name]} dict 이면 좌표 그대로 사용
(EXIF GPS → 자동 경로 제안용).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from sqlalchemy.orm import Session

from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.services import map_service
from backend.services.renderer import ASPECT_RATIO_MAP

logger = get_logger(__name__)


def _resolve_point(pt: Union[str, dict]) -> dict:
    """문자열 쿼리 → geocode, dict → 좌표 검증 후 통과. 실패 시 ValueError."""
    if isinstance(pt, dict):
        lat, lng = float(pt.get("lat")), float(pt.get("lng"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"잘못된 좌표: {pt}")
        return {"name": pt.get("name") or f"{lat:.4f}, {lng:.4f}", "lat": lat, "lng": lng}
    return map_service.geocode(str(pt))


def create_route_slide(
    db: Session,
    project: Project,
    assets_dir: Path,
    origin: Union[str, dict],
    destination: Union[str, dict],
    profile: str = "driving",
    duration: float = 5.0,
    n_frames: int = 30,
    insert_at: Optional[int] = None,
) -> Slide:
    """출발지→도착지 경로 애니메이션 슬라이드 생성 + DB 저장. 호출자가 commit 책임 (여기서 수행)."""
    project_id = project.id
    origin = _resolve_point(origin)
    destination = _resolve_point(destination)
    route = map_service.get_route(origin, destination, profile=profile)

    slide_id = str(uuid.uuid4())
    n_frames = max(6, min(60, int(n_frames)))
    canvas = ASPECT_RATIO_MAP.get(project.aspect_ratio or "16:9", (1280, 720))
    frames = map_service.render_route_frames(
        route["geometry_lnglat"], n_frames, canvas, assets_dir, slide_id,
        distance_m=route["distance_m"], duration_s=route["duration_s"], profile=route["profile"],
    )

    fps = (n_frames - 1) / float(duration) if duration > 0 else 6.0
    meta = {
        "type": "route",
        "origin": origin,
        "destination": destination,
        "profile": route["profile"],
        "geometry_lnglat": route["geometry_lnglat"],
        "distance_m": route["distance_m"],
        "duration_s": route["duration_s"],
        "frames": frames,
        "n_frames": n_frames,
        "duration": float(duration),
        "fps": fps,
        "canvas": [canvas[0], canvas[1]],
    }

    existing = db.query(Slide).filter(Slide.project_id == project_id).order_by(Slide.order_index).all()
    if insert_at is not None and insert_at >= 0:
        insert_idx = min(insert_at, len(existing))
    else:
        insert_idx = len(existing)

    slide = Slide(
        id=slide_id,
        project_id=project_id,
        order_index=insert_idx,
        image_filename=frames[0] if frames else "",
        label=f"{origin['name']} → {destination['name']}",
        text=f"{origin['name']}에서 {destination['name']}까지 이동했습니다.",
        slide_type="route",
        use_tts=1,
        meta=json.dumps(meta, ensure_ascii=False),
    )
    db.add(slide)
    combined = existing[:insert_idx] + [slide] + existing[insert_idx:]
    for idx, s in enumerate(combined):
        s.order_index = idx
    project.stage = "uploaded"
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(slide)
    logger.info(f"[{project_id}] Route slide created: {origin['name']} → {destination['name']} ({n_frames} frames)")
    return slide
