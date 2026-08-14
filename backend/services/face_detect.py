"""
DocuMotion - 얼굴 감지 (Auto-Style Phase B, Tier 2)

opencv-python-headless Haar cascade 로 이미지 내 가장 큰 얼굴 중심을 찾아
자동 줌/크롭 중심을 얼굴로 보낸다. cv2 가 없거나 얼굴이 없으면 None → 중앙 크롭.

가드: cv2 import/번들 cascade 누락 시 _CV2_OK=False → 항상 None 반환 (render 영향 無).
캐시: in-process dict (path+mtime 키). 얼굴 감지가 충분히 빠르고, 이미지 픽셀은 편집 시
      mtime 이 바뀌어 자동 무효화된다.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from backend.core.logger import get_logger

logger = get_logger(__name__)

try:
    import cv2
    _CASCADE_XML = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    _CV2_OK = hasattr(cv2, "CascadeClassifier") and os.path.exists(_CASCADE_XML)
    _CASCADE = cv2.CascadeClassifier(_CASCADE_XML) if _CV2_OK else None
    if _CV2_OK and (_CASCADE is None or _CASCADE.empty()):
        _CV2_OK = False
        _CASCADE = None
except Exception as e:
    cv2 = None
    _CV2_OK = False
    _CASCADE = None
    logger.debug(f"cv2 face cascade 사용 불가: {e}")

if not _CV2_OK:
    logger.info("Face detection 비활성화 (opencv-python-headless 미설치 또는 cascade 누락) — 중앙 크롭 사용")

# in-process 캐시: (path, mtime) -> (fx, fy) or None
_face_cache: dict[Tuple[str, float], Optional[Tuple[float, float]]] = {}


def detect_face_center(image_path: str, min_size: int = 30) -> Optional[Tuple[float, float]]:
    """
    이미지 내 가장 큰 얼굴 중심을 정규화 좌표 (fx, fy) ∈ [0,1]² 로 반환.
    cv2 미설치 / 얼굴 없음 / 오류 → None (호출자는 중앙 크롭).
    결과는 (path, mtime) 키로 in-process 캐싱.
    """
    if not _CV2_OK or not image_path or not os.path.exists(image_path):
        return None
    try:
        st = os.stat(image_path)
        key = (image_path, st.st_mtime)
        cached = _face_cache.get(key)
        if cached is not None:
            return cached  # (fx,fy) — None 은 아래에서 명시적 저장 전엔 캐시 안 함(재시도 기회)

        img = cv2.imread(image_path)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        if min(h, w) < min_size:
            return None
        faces = _CASCADE.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size))
        if len(faces) == 0:
            return None
        # 가장 큰 얼굴
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        center = (float((x + fw / 2.0) / w), float((y + fh / 2.0) / h))
        _face_cache[key] = center
        if len(_face_cache) > 500:  # crude bound
            _face_cache.pop(next(iter(_face_cache)))
        return center
    except Exception as e:
        logger.debug(f"얼굴 감지 실패({image_path}): {e}")
        return None
