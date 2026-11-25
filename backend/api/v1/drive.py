"""
DocuMotion - Google 드라이브 가져오기 API (Google Picker + drive.file)

GET  /drive/config               — Picker 설정 {enabled, client_id, api_key, app_id, authenticated, reason}
GET  /drive/token                — Picker 용 단기 access token (⚠ 브라우저에 노출: Tailscale 개인용 앱 전제)
GET  /drive/auth-url             — photospicker + drive.file 통합 OAuth URL (기존 권한 유지)
POST /drive/exchange-code        — 코드 → token.json (refresh_token 보존)
POST /drive/import/{project_id}  — 선택 파일 다운로드 → 슬라이드 (사진/영상, 포토 가져오기와 같은 처리)
"""
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.db.models import Project, Slide
from backend.db.session import get_db
from backend.services import drive_manager, media_import
from backend.services.drive_manager import DriveError

router = APIRouter(prefix="/drive", tags=["drive"])
logger = get_logger(__name__)

OAUTH_REDIRECT = "http://localhost"
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
             "image/heif": ".heif", "image/gif": ".gif", "video/mp4": ".mp4", "video/quicktime": ".mov",
             "video/webm": ".webm", "video/x-matroska": ".mkv", "video/3gpp": ".3gp"}


@router.get("/config")
def drive_config():
    return drive_manager.get_config()


@router.get("/token")
def drive_token():
    try:
        return drive_manager.get_access_token()
    except DriveError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@router.get("/auth-url")
def drive_auth_url():
    try:
        return {"auth_url": drive_manager.generate_auth_url(OAUTH_REDIRECT)}
    except Exception as e:
        logger.error(f"Drive auth URL failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class CodeExchangeRequest(BaseModel):
    code: str


@router.post("/exchange-code")
def drive_exchange_code(req: CodeExchangeRequest):
    try:
        drive_manager.exchange_code(req.code, OAUTH_REDIRECT)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Drive code exchange failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


class DriveImportRequest(BaseModel):
    file_ids: list[str] = Field(..., min_length=1, max_length=300)
    sort_order: Literal["captured", "selected"] = "captured"


def sort_imported(rows: list[dict], sort_order: str) -> list[dict]:
    """rows: [{captured: ISO|None, ...}] (선택 순서). captured: 촬영 시각순 — 시각을 모르는 항목은
    선택 순서상 바로 앞 항목의 시각을 이어받아 그 뒤에 붙는다 (맨 앞이면 첫 시각)."""
    if sort_order != "captured":
        return rows
    known = [r["captured"] for r in rows if r.get("captured")]
    if not known:
        return rows
    prev = known[0]
    keyed = []
    for i, r in enumerate(rows):
        if r.get("captured"):
            prev = r["captured"]
        keyed.append((prev, 0 if r.get("captured") else 1, i, r))
    return [k[3] for k in sorted(keyed, key=lambda k: (k[0], k[1], k[2]))]


def _captured_of(slide: Slide) -> str | None:
    import json
    try:
        v = json.loads(slide.exif or "{}").get("captured_at")
        return str(v) if v else None
    except Exception:
        return None


@router.post("/import/{project_id}")
def drive_import(project_id: str, req: DriveImportRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        drive_manager.get_access_token()
    except DriveError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

    assets = OUTPUTS_DIR / project_id / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    rows, skipped = [], []
    for fid in dict.fromkeys(req.file_ids):          # 중복 제거 (선택 순서 유지)
        name = fid
        try:
            meta = drive_manager.get_file_meta(fid)
            name = meta.get("name") or fid
            mime = str(meta.get("mimeType") or "").lower()
            if not (mime.startswith("image/") or mime.startswith("video/")):
                skipped.append({"file_id": fid, "name": name, "reason": f"지원하지 않는 형식 ({mime or '알 수 없음'})"})
                continue
            size = int(meta.get("size") or 0)
            if size > drive_manager.MAX_DOWNLOAD_BYTES:
                skipped.append({"file_id": fid, "name": name, "reason": "파일이 너무 큽니다 (최대 2GB)"})
                continue
            stem = media_import.sanitize(Path(name).stem) or f"drive_{fid[:8]}"
            ext = Path(name).suffix.lower() or _MIME_EXT.get(mime, ".mp4" if mime.startswith("video/") else ".jpg")
            dest = assets / f"{stem}_{datetime.now().strftime('%H%M%S%f')}{ext}"
            drive_manager.download_file(fid, str(dest))
            try:
                slide, fname, kind = media_import.make_media_slide(
                    project_id, dest, name, mime, 0,
                    captured_hint=drive_manager.captured_hint(meta), gps_hint=drive_manager.gps_hint(meta))
            except ValueError:
                # HEIC 변환 실패 → 드라이브 JPEG 썸네일(2560px) 폴백
                jpg = dest.with_suffix(".jpg")
                if dest.exists():
                    dest.unlink()
                if not drive_manager.download_thumbnail(meta, str(jpg)):
                    skipped.append({"file_id": fid, "name": name, "reason": "HEIC 변환 실패"})
                    continue
                slide, fname, kind = media_import.make_media_slide(
                    project_id, jpg, name, "image/jpeg", 0,
                    captured_hint=drive_manager.captured_hint(meta), gps_hint=drive_manager.gps_hint(meta))
            rows.append({"slide": slide, "filename": fname, "type": kind, "captured": _captured_of(slide)})
            logger.info(f"Drive import: {name} -> {fname}")
        except DriveError as e:
            skipped.append({"file_id": fid, "name": name, "reason": str(e)})
        except Exception as e:
            logger.error(f"Drive import failed for {fid}: {e}")
            skipped.append({"file_id": fid, "name": name, "reason": f"가져오기 실패: {str(e)[:120]}"})

    existing = db.query(Slide).filter(Slide.project_id == project_id).count()
    imported = []
    for k, r in enumerate(sort_imported(rows, req.sort_order)):
        r["slide"].order_index = existing + k
        db.add(r["slide"])
        db.flush()                                  # slide.id 기본값 채우기
        imported.append({"id": r["slide"].id, "filename": r["filename"], "type": r["type"]})
    if imported:
        project.stage = "uploaded"
        project.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"[{project_id}] Drive import: {len(imported)}/{len(req.file_ids)} (skipped {len(skipped)})")
    return {"ok": True, "imported": imported, "count": len(imported), "skipped": skipped}
