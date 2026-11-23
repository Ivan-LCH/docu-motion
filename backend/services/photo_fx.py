"""
슬라이드 사진 효과 — 연필 스케치 / 만화.

한 슬라이드당 이미 리사이즈된 스틸(Ken Burns 소스)에 한 번만 적용한다.
렌더러는 그 결과를 원본과 크로스페이드하고, 프레임마다 필터를 다시 돌리지 않는다.
ML 모델 없음. CPU only.

스케치: grayscale → invert → Gaussian blur → color-dodge.
만화:   색면 평탄화(bilateral+stylization) + 양자화 + 채도 강화 + 잉크 외곽선
        (가장 긴 변 1280 으로 제한 후 원 해상도로 복원). 그림 구간은 시네마틱 색보정을 건너뛴다.

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
    # dodge 결과는 종이가 하얗고 선이 옅다. 잔차를 키워 연필선이 분명하게 읽히게 한다.
    residual = np.minimum((np.uint16(255) - sketch.astype(np.uint16)) * np.uint16(3), np.uint16(255))
    sketch = (np.uint16(255) - residual).astype(np.uint8)
    return np.stack([sketch, sketch, sketch], axis=-1)


def _cartoon(rgb: np.ndarray) -> np.ndarray:
    try:
        import cv2
        return _cartoon_cv(rgb, cv2)
    except Exception:
        return _cartoon_pil(rgb)


def _match_luma(src: np.ndarray, out: np.ndarray) -> np.ndarray:
    """스타일 결과의 평균 휘도를 원본에 맞춰, 그림 구간이 갑자기 어두워지지 않게 한다."""
    s = src.astype(np.float32)
    o = out.astype(np.float32)
    ys = float((0.299 * s[..., 0] + 0.587 * s[..., 1] + 0.114 * s[..., 2]).mean())
    yo = float((0.299 * o[..., 0] + 0.587 * o[..., 1] + 0.114 * o[..., 2]).mean())
    if yo > 1.0 and ys > 0:
        o = np.clip(o * (ys / yo), 0, 255)
    return o.astype(np.uint8)


def _ink_edges(gray_u8: np.ndarray, cv2) -> np.ndarray:
    """구조적 외곽선만 남긴 잉크 마스크 (1=유지, 낮을수록 선). 잔디처럼 잔 텍스처는 줄인다."""
    blur = cv2.medianBlur(gray_u8, 7)
    edges = cv2.Canny(blur, 80, 160)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    wgt = np.where(edges > 0, 0.16, 1.0).astype(np.float32)
    return cv2.GaussianBlur(wgt, (3, 3), 0)


def _boost_sat(rgb: np.ndarray, cv2, amount: float = 1.4) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 1] = np.clip(hsv[..., 1] * amount, 0, 255)
    return cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2RGB)


def _posterize(rgb: np.ndarray, step: int = 28) -> np.ndarray:
    step = max(2, int(step))
    return ((rgb.astype(np.uint16) // np.uint16(step)) * np.uint16(step)).clip(0, 255).astype(np.uint8)


def _cartoon_cv(rgb: np.ndarray, cv2) -> np.ndarray:
    """색면 평탄화 + 양자화 + 채도 강화 + 잉크 외곽선. 긴 변 1280 제한."""
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
    flat = work
    for _ in range(2):
        flat = cv2.bilateralFilter(flat, d=9, sigmaColor=80, sigmaSpace=80)
    styled = cv2.stylization(flat, sigma_s=60, sigma_r=0.45)
    mix = cv2.addWeighted(styled, 0.7, flat, 0.3, 0)
    poster = _boost_sat(_posterize(mix, 28), cv2, 1.4)
    ink = _ink_edges(cv2.cvtColor(work, cv2.COLOR_RGB2GRAY), cv2)
    out = np.clip(poster.astype(np.float32) * ink[..., None], 0, 255).astype(np.uint8)
    out = _match_luma(work, out)
    if out.shape[0] != h or out.shape[1] != w:
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(out)


def _cartoon_pil(rgb: np.ndarray) -> np.ndarray:
    """cv2 가 없을 때의 근사. 다운스케일 블러 + 양자화 + 채도 + 기울기 외곽선."""
    from PIL import Image, ImageEnhance, ImageFilter

    h, w = rgb.shape[:2]
    im = Image.fromarray(rgb, mode="RGB")
    small = im.resize((max(1, w // 2), max(1, h // 2)), Image.BILINEAR)
    small = small.filter(ImageFilter.SMOOTH_MORE).filter(ImageFilter.SMOOTH_MORE)
    small = small.filter(ImageFilter.SMOOTH_MORE)
    smooth = np.asarray(small.resize((w, h), Image.BILINEAR)).astype(np.uint8)
    smooth = _posterize(smooth, 28)
    smooth = np.asarray(ImageEnhance.Color(Image.fromarray(smooth, mode="RGB")).enhance(1.4))

    gray = (
        0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    ).astype(np.float32)
    gy, gx = np.gradient(gray)
    mag = np.sqrt(gx * gx + gy * gy)
    thresh = max(18.0, float(np.percentile(mag, 90)))
    # 1=유지, 0.16=잉크
    wgt = np.where(mag >= thresh, 0.16, 1.0).astype(np.float32)
    out = np.clip(smooth.astype(np.float32) * wgt[..., None], 0, 255).astype(np.uint8)
    return _match_luma(rgb, out)


# ────────────────────────────────────────────────────────────────────────────
# 2단계: 그림/만화 → 실사 전환 (CF 느낌의 마스크 리빌)
# ────────────────────────────────────────────────────────────────────────────
# 슬라이드 앞부분은 스타일 스틸(스케치/만화)로 시작해 마스크가 번지며 실사가 드러난다.
# 마스크 필드는 슬라이드당 한 번 만들고, 프레임마다 임계값만 바꾼다 (CPU 저렴).
#   brush    : 가로로 길쭉한 붓결 + 저주파 얼룩 + 좌→우 바이어스 — 물감이 씻기듯
#   wipe     : 살짝 기운 대각 와이프, 가장자리 흔들림 + 부드러운 페더
#   dissolve : 전체 크로스페이드
TO_REAL_STYLES: tuple[str, ...] = ("brush", "wipe", "dissolve")
TO_REAL_HOLD = 0.3   # 슬라이드 길이 중 그림 상태로 머무는 비율 (전환 시작 시점)


def normalize_to_real(value) -> str:
    """meta.effect_to_real 정규화. 알 수 없는 값/빈 값 → '' (기존 실사→그림 크로스페이드)."""
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    return v if v in TO_REAL_STYLES else ""


def to_real_progress(t: float, duration: float, reveal: float, hold: float = TO_REAL_HOLD) -> float:
    """0 = 그림(스타일) 그대로, 1 = 실사. hold 비율만큼 그림으로 머문 뒤 reveal 비율 동안 전환.
    reveal<=0 이면 hold 시점에 즉시 컷. 전환은 슬라이드 끝 5% 전에 끝나도록 당긴다."""
    try:
        t_f, d_f, r_f = float(t), float(duration), float(reveal)
    except (TypeError, ValueError):
        return 1.0
    if d_f != d_f or t_f != t_f or r_f != r_f or d_f <= 0:
        return 1.0
    r_f = max(0.0, min(1.0, r_f))
    window = r_f * d_f
    start = max(0.0, min(hold * d_f, d_f * 0.95 - window))
    if window <= 1e-6:
        return 0.0 if t_f < start else 1.0
    p = (t_f - start) / window
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return p * p * (3.0 - 2.0 * p)


def _resize_field(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    try:
        import cv2
        return cv2.resize(arr.astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    except Exception:
        from PIL import Image
        return np.asarray(Image.fromarray(arr.astype(np.float32), mode="F").resize((w, h), Image.BICUBIC))


def _rank_normalize(field: np.ndarray) -> np.ndarray:
    """값 분포를 0~1 균등으로 — 임계값 p 가 '드러난 면적 비율 p' 에 대응하게."""
    flat = field.ravel()
    order = np.argsort(flat, kind="stable")
    ranks = np.empty_like(order, dtype=np.float32)
    ranks[order] = np.linspace(0.0, 1.0, flat.size, dtype=np.float32)
    return ranks.reshape(field.shape)


class RevealMask:
    """(h, w) 크기 실사 마스크 생성기. at(p) → float32 HxW (1=실사) 또는 스칼라(dissolve)."""

    def __init__(self, h: int, w: int, style: str, seed: int = 0):
        self.h, self.w = max(1, int(h)), max(1, int(w))
        self.style = normalize_to_real(style) or "dissolve"
        rng = np.random.default_rng(1000 + int(seed))
        ys = np.linspace(0.0, 1.0, self.h, dtype=np.float32)[:, None]
        xs = np.linspace(0.0, 1.0, self.w, dtype=np.float32)[None, :]
        if self.style == "brush":
            # 붓결: 세로로 촘촘, 가로로 길게 늘린 노이즈 → 가로 스트로크
            strokes = _resize_field(rng.random((max(4, self.h // 18), max(3, self.w // 160))), self.w, self.h)
            blobs = _resize_field(rng.random((5, 8)), self.w, self.h)
            fine = _resize_field(rng.random((max(8, self.h // 6), max(8, self.w // 6))), self.w, self.h)
            direction = xs * 0.9 + ys * 0.2          # 좌상단 → 우하단으로 칠해 나감
            field = 0.42 * strokes + 0.22 * blobs + 0.08 * fine + 0.55 * direction
            self.field = _rank_normalize(field)
            self.feather = 0.07
        elif self.style == "wipe":
            wobble = _resize_field(rng.random((max(4, self.h // 40), 2)), self.w, self.h)
            field = xs * 0.86 + ys * 0.14 + (wobble - 0.5) * 0.06
            self.field = _rank_normalize(field)
            self.feather = 0.05
        else:
            self.field = None
            self.feather = 0.0

    def at(self, p: float):
        p = max(0.0, min(1.0, float(p)))
        if self.field is None:
            return p
        if p <= 0.0:
            return np.zeros_like(self.field)
        if p >= 1.0:
            return np.ones_like(self.field)
        f = self.feather
        # p=0 → 전부 0, p=1 → 전부 1 (페더 포함)
        m = (p * (1.0 + f) - self.field) / f
        return np.clip(m, 0.0, 1.0)


def blend_mask(real: np.ndarray, styled: np.ndarray, mask) -> np.ndarray:
    """mask(1=실사) 로 합성. 스칼라면 blend_rgb, 배열이면 픽셀별."""
    if np.isscalar(mask) or np.ndim(mask) == 0:
        return blend_rgb(styled, real, float(mask))
    m = np.asarray(mask, dtype=np.float32)
    if m.shape != real.shape[:2]:
        m = _resize_field(m, real.shape[1], real.shape[0])
    if float(m.max()) <= 0.0:
        return styled
    if float(m.min()) >= 1.0:
        return real
    s = styled.astype(np.float32)
    out = s + (real.astype(np.float32) - s) * m[..., None]
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)
