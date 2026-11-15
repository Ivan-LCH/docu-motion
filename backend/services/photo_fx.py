"""
슬라이드 사진 효과 — 연필 스케치 / 만화.

한 슬라이드당 이미 리사이즈된 스틸(Ken Burns 소스)에 한 번만 적용한다.
렌더러는 그 결과를 원본과 크로스페이드하고, 프레임마다 필터를 다시 돌리지 않는다.
ML 모델 없음. CPU only.

스케치: grayscale → invert → Gaussian blur → color-dodge.
만화:   cv2.stylization (가장 긴 변 1280 으로 제한 후 원 해상도로 복원).

opencv-python-headless 는 얼굴 감지용으로 이미 의존성이다.
필터는 그걸 쓰고, cv2 를 못 읽는 환경에서는 PIL 근사로 렌더가 죽지 않게 한다.
"""
from __future__ import annotations

import numpy as np

from backend.services.presets import normalize_photo_effect


def ensure_rgb_uint8(image: np.ndarray) -> np.ndarray:
    """HxWx3 uint8 RGB. 그레이/RGBA/float 입력을 맞춘다."""
    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    elif arr.ndim == 3 and arr.shape[-1] == 4:
        arr = arr[..., :3]
    elif arr.ndim != 3 or arr.shape[-1] != 3:
        raise ValueError(f"expected HxWx3 image, got shape {arr.shape}")

    if arr.dtype != np.uint8:
        if np.issubdtype(arr.dtype, np.floating):
            scale = 255.0 if float(np.nanmax(arr)) <= 1.5 else 1.0
            arr = np.clip(arr * scale, 0, 255)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def stylize_photo(image: np.ndarray, effect: str) -> np.ndarray:
    """효과 적용 스틸. 입력과 같은 HxWx3 uint8. 'none'/알 수 없는 값은 원본 복사."""
    rgb = ensure_rgb_uint8(image)
    name = normalize_photo_effect(effect)
    if name == "sketch":
        return _sketch(rgb)
    if name == "cartoon":
        return _cartoon(rgb)
    return rgb.copy()


def reveal_weight(t: float, duration: float, reveal: float) -> float:
    """0 = 원본 사진, 1 = 스타일 완성.

    reveal 은 슬라이드 길이 중 크로스페이드가 차지하는 비율 (0~1).
    0 이면 처음부터 스타일(애니메이션 없음). 끝점은 smoothstep.
    """
    try:
        reveal_f = float(reveal)
        duration_f = float(duration)
        t_f = float(t)
    except (TypeError, ValueError):
        return 1.0
    if reveal_f != reveal_f or duration_f != duration_f or t_f != t_f:
        return 1.0
    reveal_f = max(0.0, min(1.0, reveal_f))
    if reveal_f <= 0.0 or duration_f <= 0.0:
        return 1.0
    window = reveal_f * duration_f
    if window <= 1e-6:
        return 1.0
    p = t_f / window
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return p * p * (3.0 - 2.0 * p)


def blend_rgb(original: np.ndarray, styled: np.ndarray, weight: float) -> np.ndarray:
    """uint8 RGB 크로스페이드. weight 0/1 은 새 배열을 만들지 않는다."""
    w = float(weight)
    if w <= 0.0:
        return original
    if w >= 1.0:
        return styled
    w_i = int(round(w * 256.0))
    w_i = max(0, min(256, w_i))
    out = (
        original.astype(np.uint16) * np.uint16(256 - w_i)
        + styled.astype(np.uint16) * np.uint16(w_i)
    )
    return (out >> np.uint16(8)).astype(np.uint8)


def _sketch(rgb: np.ndarray) -> np.ndarray:
    sigma = max(1.2, min(rgb.shape[0], rgb.shape[1]) * 0.012)
    try:
        import cv2
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        inv = cv2.bitwise_not(gray)
        blur = cv2.GaussianBlur(inv, (0, 0), sigmaX=sigma, sigmaY=sigma)
    except Exception:
        from PIL import Image, ImageFilter
        gray_im = Image.fromarray(rgb, mode="RGB").convert("L")
        gray = np.asarray(gray_im)
        inv = 255 - gray
        blur = np.asarray(
            Image.fromarray(inv, mode="L").filter(ImageFilter.GaussianBlur(radius=sigma))
        )
    gray16 = gray.astype(np.uint16)
    blur16 = blur.astype(np.uint16)
    denom = np.maximum(np.uint16(256) - blur16, np.uint16(1))
    sketch = np.minimum((gray16 * np.uint16(256)) // denom, np.uint16(255)).astype(np.uint8)
    # dodge 결과는 종이가 하얗고 선이 옅다. 잔차를 키워 영상에서 연필선이 읽히게 한다.
    residual = np.minimum((np.uint16(255) - sketch.astype(np.uint16)) * np.uint16(2), np.uint16(255))
    sketch = (np.uint16(255) - residual).astype(np.uint8)
    return np.stack([sketch, sketch, sketch], axis=-1)


def _cartoon(rgb: np.ndarray) -> np.ndarray:
    try:
        import cv2
        return _cartoon_cv(rgb, cv2)
    except Exception:
        return _cartoon_pil(rgb)


def _cartoon_cv(rgb: np.ndarray, cv2) -> np.ndarray:
    """cv2.stylization. 긴 변을 1280 으로 제한해 슬라이드당 버퍼를 고정한다."""
    h, w = rgb.shape[:2]
    work = rgb
    long_side = max(h, w)
    cap = 1280
    if long_side > cap:
        scale = cap / float(long_side)
        work = cv2.resize(
            rgb,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    if not hasattr(cv2, "stylization"):
        raise RuntimeError("cv2.stylization unavailable")
    styled = cv2.stylization(work, sigma_s=60, sigma_r=0.45)
    if styled.shape[0] != h or styled.shape[1] != w:
        styled = cv2.resize(styled, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(styled)


def _cartoon_pil(rgb: np.ndarray) -> np.ndarray:
    """cv2 가 없을 때의 근사. 다운스케일 블러 + 양자화 + 기울기 외곽선."""
    from PIL import Image, ImageFilter

    h, w = rgb.shape[:2]
    im = Image.fromarray(rgb, mode="RGB")
    small = im.resize((max(1, w // 2), max(1, h // 2)), Image.BILINEAR)
    small = small.filter(ImageFilter.SMOOTH_MORE).filter(ImageFilter.SMOOTH_MORE)
    smooth = np.asarray(small.resize((w, h), Image.BILINEAR)).astype(np.uint8)
    q = np.uint8(28)
    smooth = (smooth // q) * q

    gray = (
        0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    ).astype(np.float32)
    gy, gx = np.gradient(gray)
    mag = np.sqrt(gx * gx + gy * gy)
    thresh = max(12.0, float(np.percentile(mag, 88)))
    edges = np.where(mag >= thresh, np.uint8(0), np.uint8(255))
    return (smooth.astype(np.uint16) * edges[..., None].astype(np.uint16) // np.uint16(255)).astype(np.uint8)
