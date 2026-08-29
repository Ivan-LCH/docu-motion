"""
DocuMotion - 스마트 사진 선별 (Photo-Vlog F1)

사진 30~100장 업로드 워크플로에서 남는 수작업을 자동화:
  (a) 유사 장면 클러스터링 (dHash+aHash 해밍 거리 + 촬영시각 근접) → 버스트 컷 묶음
  (b) 클러스터별 베스트 1장 선정 (선명도 + 노출 + 얼굴 보너스)
  (c) 흐림/어둠 컷 감지
결과는 "제안"일 뿐 — 실제 삭제는 사용자가 명시적으로 수락한 slide_ids 로만 일어난다.

해밍 ≤ HASH_SIMILARITY(10) AND 촬영시각 ≤ BURST_WINDOW_S(120s) → 같은 클러스터.
시각 없는 사진(스크린샷 등)은 시각 조건을 무시하고 해시만으로 판정.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from backend.core.logger import get_logger
from backend.services.face_detect import detect_face_center

logger = get_logger(__name__)

try:
    import cv2
    _CV2_OK = hasattr(cv2, "Laplacian")
except Exception:
    cv2 = None
    _CV2_OK = False

HASH_SIMILARITY = 10   # 해밍 거리 임계 (64bit 기준) — 이하 = 유사 장면
BURST_WINDOW_S = 120   # 촬영시각 근접 임계 (초) — 버스트 촬영 윈도우
DARK_LUMA = 50.0       # 평균 밝기 이하 = 어둡다고 판정
BLUR_VAR = 60.0        # Laplacian 분산 이하 = 흐리다고 판정

# 해시 캐시: (path, mtime) → (dhash, ahash, sharpness, luma, has_face)
_cache: dict[tuple, tuple] = {}


def _bits_str(hex_or_bin) -> int:
    return int(hex_or_bin, 16)


def _dhash(im_gray: Image.Image) -> int:
    """9x8 그레이 축소 → 좌우 차 비트 64bit."""
    small = im_gray.resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def _ahash(im_gray: Image.Image) -> int:
    """8x8 그레이 축소 → 평균 대비 비트 64bit."""
    small = im_gray.resize((8, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v > avg else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _features(path: Path) -> Optional[tuple]:
    """이미지 → (dhash, ahash, sharpness|None, luma, has_face). 캐시됨."""
    key = (str(path), path.stat().st_mtime if path.exists() else 0.0)
    if key in _cache:
        return _cache[key]
    if not path.exists():
        return None
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            dhash, ahash = _dhash(gray), _ahash(gray)
            hist = gray.histogram()
            total = sum(hist)
            luma = (sum(i * c for i, c in enumerate(hist)) / total) if total else 0.0
        sharpness = None
        if _CV2_OK:
            arr = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if arr is not None:
                sharpness = float(cv2.Laplacian(arr, cv2.CV_64F).var())
        has_face = detect_face_center(str(path)) is not None
        feats = (dhash, ahash, sharpness, luma, has_face)
        _cache[key] = feats
        if len(_cache) > 1000:
            _cache.pop(next(iter(_cache)))
        return feats
    except Exception as e:
        logger.debug(f"사진 특징 추출 실패({path}): {e}")
        return None


def _captured_epoch(exif_json: Optional[str]) -> Optional[float]:
    try:
        data = json.loads(exif_json) if exif_json else {}
        at = data.get("captured_at")
        return datetime.fromisoformat(at).timestamp() if at else None
    except Exception:
        return None


def _score(feats: tuple) -> float:
    """베스트 컷 점수 — 높을수록 좋음. sharpness/luma는 정규화 가드."""
    _, _, sharpness, luma, has_face = feats
    s = 0.0
    if sharpness is not None:
        s += min(sharpness / 300.0, 1.0)          # 선명도 (0~1)
    luma_score = 1.0 - min(abs(luma - 118.0) / 118.0, 1.0)  # 중간 밝기 최고
    s += luma_score
    if has_face:
        s += 0.5                                   # 얼굴 보너스
    return s


def curate_photos(photos: list[dict], progress_cb=None) -> dict:
    """
    photos: [{slide_id, path, exif}] (exif: Slide.exif JSON 문자열|None)
    반환: {suggestions: [{slide_id, action, reason, cluster_id, is_best}]}
      action ∈ {"duplicate", "blurry", "dark"} — 제외 제안. 베스트/유지 컷은 포함하지 않는다.
    """
    feats: dict[str, tuple] = {}
    epochs: dict[str, Optional[float]] = {}
    total = len(photos)
    for i, p in enumerate(photos):
        f = _features(Path(p["path"]))
        if f:
            feats[p["slide_id"]] = f
            epochs[p["slide_id"]] = _captured_epoch(p.get("exif"))
        if progress_cb:
            progress_cb((i + 1) / max(total, 1))

    ids = [p["slide_id"] for p in photos if p["slide_id"] in feats]

    # (a) union-find 클러스터링: 해밍 ≤ HASH_SIMILARITY AND (시각 근접 or 시각 없음)
    parent = {sid: sid for sid in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            dh = _hamming(feats[a][0], feats[b][0]) + _hamming(feats[a][1], feats[b][1])
            if dh > HASH_SIMILARITY * 2:   # dHash+aHash 합산 기준
                continue
            ta, tb = epochs[a], epochs[b]
            if ta is not None and tb is not None and abs(ta - tb) > BURST_WINDOW_S:
                continue
            union(a, b)

    clusters: dict[str, list[str]] = {}
    for sid in ids:
        clusters.setdefault(find(sid), []).append(sid)

    # (b) 클러스터별 베스트 + (c) 흐림/어둠 — 모든 사진에 정확히 1개 제안
    suggestions = []
    suggested: set[str] = set()

    def _add(sid, action, reason, cluster_id, is_best):
        if sid in suggested:
            return
        suggested.add(sid)
        suggestions.append({"slide_id": sid, "action": action, "reason": reason,
                            "cluster_id": cluster_id, "is_best": is_best})

    cluster_no = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        cluster_no += 1
        ranked = sorted(members, key=lambda s: _score(feats[s]), reverse=True)
        best = ranked[0]
        for rank, sid in enumerate(ranked):
            if sid != best:
                _add(sid, "duplicate",
                     f"유사 장면 {len(members)}장 중 {rank + 1}위 (베스트 컷 유지)",
                     cluster_no, False)
            elif feats[sid][3] < DARK_LUMA:
                _add(sid, "dark", f"어두운 사진 (밝기 {feats[sid][3]:.0f})",
                     cluster_no, True)
            elif feats[sid][2] is not None and feats[sid][2] < BLUR_VAR:
                _add(sid, "blurry", f"선명도 낮음 (Laplacian {feats[sid][2]:.0f})",
                     cluster_no, True)

    # 클러스터에 속하지 않은(또는 단독 클러스터) 사진의 흐림/어둠
    for sid in ids:
        if sid in suggested:
            continue
        _, _, sharpness, luma, _ = feats[sid]
        if luma < DARK_LUMA:
            _add(sid, "dark", f"어두운 사진 (밝기 {luma:.0f})", 0, False)
        elif sharpness is not None and sharpness < BLUR_VAR:
            _add(sid, "blurry", f"선명도 낮음 (Laplacian {sharpness:.0f})", 0, False)

    clustered = sum(len(m) for m in clusters.values() if len(m) > 1)
    return {"suggestions": suggestions, "total": total, "clustered": clustered}
