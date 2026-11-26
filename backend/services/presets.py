"""
DocuMotion - CinemaFlow 프리셋 (전환 효과 / 텍스트 스타일)

전환·텍스트 스타일의 단일 소스. 렌더러, 미리보기, 비트 스냅, BGM 덕킹, API 검증,
자동 연출이 모두 이 모듈의 상수/헬퍼를 사용해 동일한 overlap 규칙을 공유한다.

이 모듈은 moviepy 를 import 하지 않는다 (pydantic 스키마에서도 import 하기 때문).
"""
from __future__ import annotations

import json
import re


# ────────────────────────────────────────────────────────────────────────────
# 전환 효과
# ────────────────────────────────────────────────────────────────────────────
# duration: None → 프로젝트 transition_duration 사용 (사용자 조절 가능한 기존 효과)
#           float → 고정 길이 (빠른 컷 계열)
# overlap:  True  → 앞 슬라이드 끝과 이 슬라이드 시작이 duration 만큼 겹침
#           False → 겹침 없이 순차 배치 (none / fade_black)
TRANSITIONS: dict[str, dict] = {
    "none":         {"duration": 0.0,  "overlap": False, "description": "하드 컷"},
    "crossfade":    {"duration": None, "overlap": True,  "description": "크로스페이드 (디졸브)"},
    "fade_black":   {"duration": None, "overlap": False, "description": "검정으로 페이드 아웃 → 페이드 인"},
    "slide_left":   {"duration": None, "overlap": True,  "description": "다음 장면이 오른쪽에서 밀려 들어옴"},
    "slide_right":  {"duration": None, "overlap": True,  "description": "다음 장면이 왼쪽에서 밀려 들어옴"},
    "whip_left":    {"duration": 0.35, "overlap": True,  "description": "빠른 휩 팬 ← (모션 블러)"},
    "whip_right":   {"duration": 0.35, "overlap": True,  "description": "빠른 휩 팬 → (모션 블러)"},
    "zoom_through": {"duration": 0.45, "overlap": True,  "description": "앞 장면으로 줌 돌진 → 다음 장면 줌 안착"},
    "light_leak":   {"duration": 0.4,  "overlap": True,  "description": "따뜻한 빛 번짐 디졸브"},
    "flash":        {"duration": 0.3,  "overlap": True,  "description": "화이트 플래시 컷"},
}
VALID_TRANSITIONS: tuple[str, ...] = tuple(TRANSITIONS.keys())
DEFAULT_TRANSITION = "crossfade"

# 겹침은 인접 두 슬라이드 중 짧은 쪽 길이의 이 비율을 넘지 않는다.
# (한 슬라이드가 앞/뒤 양쪽 전환에 모두 겹쳐도 최소 10%는 단독 노출)
MAX_OVERLAP_RATIO = 0.45

# 자동 연출(auto_video) 전환 순환 — 차분한 디졸브 사이에 빠른 컷을 섞어 리듬감
AUTO_TRANSITION_CYCLE: tuple[str, ...] = (
    "crossfade", "whip_left", "fade_black", "zoom_through",
    "slide_left", "light_leak", "crossfade", "whip_right", "flash",
)


def normalize_transition(name) -> str:
    """알 수 없는 값/None → 'none' (렌더 경로용 관대한 정규화)."""
    name = (name or "none").strip().lower() if isinstance(name, str) else "none"
    return name if name in TRANSITIONS else "none"


def transition_duration(name, project_duration: float = 0.7) -> float:
    """전환 효과 자체의 길이(초). duration=None 프리셋은 프로젝트 설정값."""
    spec = TRANSITIONS[normalize_transition(name)]
    d = spec["duration"]
    if d is None:
        d = project_duration if project_duration and project_duration > 0 else 0.7
    return float(d)


def slide_overlap(name, project_duration: float, prev_dur: float, cur_dur: float) -> float:
    """앞 슬라이드(prev_dur)와 현재 슬라이드(cur_dur)가 겹치는 시간(초).
    overlap 프리셋만 >0, 짧은 쪽 길이의 MAX_OVERLAP_RATIO 로 상한."""
    spec = TRANSITIONS[normalize_transition(name)]
    if not spec["overlap"]:
        return 0.0
    cap = MAX_OVERLAP_RATIO * max(0.0, min(prev_dur or 0.0, cur_dur or 0.0))
    return max(0.0, min(transition_duration(name, project_duration), cap))


def effect_duration(name, project_duration: float, prev_dur: float, cur_dur: float) -> float:
    """전환 애니메이션 길이. overlap 프리셋은 overlap 과 동일,
    fade_black 처럼 겹치지 않는 효과는 각 슬라이드 안에서 같은 상한으로 클램프."""
    spec = TRANSITIONS[normalize_transition(name)]
    if spec["overlap"]:
        return slide_overlap(name, project_duration, prev_dur, cur_dur)
    if spec["duration"] == 0.0:
        return 0.0
    cap = MAX_OVERLAP_RATIO * max(0.0, min(prev_dur or 0.0, cur_dur or 0.0))
    return max(0.0, min(transition_duration(name, project_duration), cap))


def compute_slide_starts(durations: list[float], transitions: list,
                         project_duration: float = 0.7) -> tuple[list[float], list[float]]:
    """슬라이드별 글로벌 시작 시각과 진입 overlap 을 계산.

    durations[i]   : i번째 클립 길이
    transitions[i] : i번째 슬라이드 '진입' 전환 (transitions[0] 은 무시)
    반환: (starts, overlaps) — starts[i] = starts[i-1] + durations[i-1] - overlaps[i]
    렌더/미리보기/비트 스냅/BGM 덕킹이 모두 이 규칙을 공유한다.
    """
    n = len(durations)
    starts = [0.0] * n
    overlaps = [0.0] * n
    for i in range(1, n):
        trans = transitions[i] if i < len(transitions) else "none"
        overlaps[i] = slide_overlap(trans, project_duration, durations[i - 1], durations[i])
        starts[i] = starts[i - 1] + durations[i - 1] - overlaps[i]
    return starts, overlaps


def timeline_duration(durations: list[float], transitions: list,
                      project_duration: float = 0.7) -> float:
    if not durations:
        return 0.0
    starts, _ = compute_slide_starts(durations, transitions, project_duration)
    return starts[-1] + durations[-1]


# ────────────────────────────────────────────────────────────────────────────
# 사진 효과 (슬라이드 단위, 원본 → 스케치/만화/수채)
# ────────────────────────────────────────────────────────────────────────────
# effect_reveal: 슬라이드 길이 중 원본→스타일 크로스페이드가 차지하는 비율.
#                0 = 처음부터 스타일, 1 = 슬라이드 전체가 전환.
PHOTO_EFFECTS: dict[str, dict] = {
    "none":    {"description": "원본 사진"},
    "sketch":  {"description": "연필 스케치 (gray → invert → blur → color-dodge)"},
    "cartoon": {"description": "만화 (색면 평탄화·외곽선·채도 강화, 슬라이드당 1회)"},
    "sketch_soft": {"description": "부드러운 스케치 (연필, 선 얇고 종이 밝음)"},
    "watercolor":  {"description": "부드러운 수채 애니풍 (색면 평탄화, 얇은 선, 수채 질감)"},
    "sketch_color": {"description": "파스텔 채색 스케치 (부드러운 연필선 + 낮은 채도 파스텔 색면)"},
}
VALID_PHOTO_EFFECTS: tuple[str, ...] = tuple(PHOTO_EFFECTS.keys())
DEFAULT_PHOTO_EFFECT = "none"
DEFAULT_EFFECT_REVEAL = 0.45
# meta.sketch_softness (0~1): sketch 는 없거나 0 이면 기존 진한 스케치, sketch_soft 는 기본 0.75.
DEFAULT_SKETCH_SOFTNESS = 0.75
# meta.color_strength (0~1): sketch_color 의 파스텔 채색 진하기. 없으면 0.70.
DEFAULT_COLOR_STRENGTH = 0.70


def normalize_photo_effect(name) -> str:
    """알 수 없는 값/None → 'none' (렌더 경로용 관대한 정규화)."""
    name = (name or DEFAULT_PHOTO_EFFECT).strip().lower() if isinstance(name, str) else DEFAULT_PHOTO_EFFECT
    return name if name in PHOTO_EFFECTS else DEFAULT_PHOTO_EFFECT


def clamp_effect_reveal(value, default: float = DEFAULT_EFFECT_REVEAL) -> float:
    """0~1 로 클램프. 비어 있거나 숫자가 아니면 default (렌더 경로)."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f == float("inf") or f == float("-inf"):
        return default
    return max(0.0, min(1.0, f))


def parse_sketch_softness(value):
    """meta.sketch_softness → 0~1 float. 비어 있거나 숫자가 아니면 None (효과별 기본값 사용)."""
    return _parse_unit(value)


def parse_color_strength(value):
    """meta.color_strength → 0~1 float. 비어 있거나 숫자가 아니면 None (DEFAULT_COLOR_STRENGTH 사용)."""
    return _parse_unit(value)


def _parse_unit(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(1.0, f))


# ────────────────────────────────────────────────────────────────────────────
# 텍스트 스타일
# ────────────────────────────────────────────────────────────────────────────
# font_scale: min(캔버스 폭, 높이) 대비 폰트 크기 (caption 은 프로젝트 자막 폰트 크기 사용)
#             → 16:9 / 9:16 / 1:1 모두 같은 상대 크기
# y:          텍스트 블록 중심의 세로 위치 (캔버스 높이 비율, caption 은 하단 여백 기준)
TEXT_STYLES: dict[str, dict] = {
    "caption": {"description": "하단 자막 (문장별, 짧은 fade-up)",
                "anim": "fade_up", "anim_dur": 0.3},
    "hero":    {"description": "큰 굵은 제목 — 하단 중앙, 떠오르며 확대",
                "anim": "rise_scale", "anim_dur": 0.5, "font_scale": 0.1, "y": 0.74,
                "max_lines": 2},
    "punch":   {"description": "짧은 한 단어 — 화면 중앙 팝",
                "anim": "pop", "anim_dur": 0.15, "font_scale": 0.18, "y": 0.5,
                "max_chars": 8, "max_lines": 1},
    # 잡지식 에디토리얼 타이포 (자동 편집): 좌/우 하단 한쪽 정렬, 외곽선 없는 차분한 흰색/오프화이트.
    # 문구는 meta.kinetic_text, 보조줄(날짜 등)은 meta.kinetic_sub.
    "kinetic": {"description": "잡지식 타이포 — 좌/우 하단 정렬, 차분한 화이트 (자동 편집)",
                "anim": "editorial_fade", "anim_dur": 0.4, "font_scale": 0.046,
                "title_font_scale": 0.056, "margin_x": 0.055, "margin_y": 0.085,
                "max_chars": 24, "max_lines": 2, "exit_dur": 0.35, "highlight": "#F7F7F2"},
    "none":    {"description": "텍스트 없음 (나레이션 음성은 유지)"},
}
VALID_TEXT_STYLES: tuple[str, ...] = tuple(TEXT_STYLES.keys())
DEFAULT_TEXT_STYLE = "caption"

# hero/punch 문구로 쓰지 않을 라벨 (업로드 파일명 등)
_FILENAME_LIKE = re.compile(
    r"\.(jpe?g|png|gif|webp|heic|heif|bmp|tiff?|mp4|mov|m4v|avi|mkv|webm|pdf)$", re.I)


def normalize_text_style(name) -> str:
    name = (name or DEFAULT_TEXT_STYLE).strip().lower() if isinstance(name, str) else DEFAULT_TEXT_STYLE
    return name if name in TEXT_STYLES else DEFAULT_TEXT_STYLE


def _parse_meta(meta) -> dict:
    if isinstance(meta, dict):
        return meta
    try:
        m = json.loads(meta or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def resolve_style_text(item: dict, style: str) -> str:
    """hero/punch/kinetic 문구 결정.
    우선순위: meta.{style}_text → meta.title → label(파일명 아님) → meta.caption
             → meta.name(장소) → 나레이션 첫 문장. punch/kinetic 은 max_chars 로 자름."""
    style = normalize_text_style(style)
    if style not in ("hero", "punch", "kinetic"):
        return ""
    meta = _parse_meta(item.get("meta"))
    label = (item.get("label") or "").strip()
    if _FILENAME_LIKE.search(label) or label.startswith("Collage ("):
        label = ""
    first_sentence = ""
    narration = (item.get("text") or "").strip()
    if narration:
        first_sentence = re.split(r"(?<=[.!?])\s+", narration)[0].strip()

    candidates = [meta.get(f"{style}_text"), meta.get("title"), label,
                  meta.get("caption"), meta.get("name"), first_sentence]
    text = next((str(c).strip() for c in candidates if isinstance(c, str) and c.strip()), "")
    max_chars = TEXT_STYLES[style].get("max_chars")
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


# ────────────────────────────────────────────────────────────────────────────
# 슬라이드 길이 · 그림→실사 홀드 (렌더 / 미리보기 / 비트 스냅 / 간편 자동 길이 추정 공용)
# ────────────────────────────────────────────────────────────────────────────
# 그림→실사(meta.effect_to_real) 슬라이드는 실사가 다 드러난 뒤 real_hold 초 동안 실사로 머물고,
# 그다음에야 다음 슬라이드 전환(overlap)이 시작되도록 '그 슬라이드만' 늘린다.
# effect_reveal 은 슬라이드 길이의 비율이라 길이를 늘리면 드러나기도 같이 늘어난다 →
# 늘리기 전 길이(reveal_base)로 드러나기 타이밍을 절대 초로 고정하고 끝에 홀드만 붙인다.
#   reveal_end = start + window,  window = reveal × base,
#   start = max(0, min(0.3 × base, 0.95 × base − window))   (photo_fx.to_real_progress 와 동일)
#   필요 길이 D = max(base, reveal_end + hold + overlap(next, D))   (overlap 은 D 에 대해 단조 → 고정점 반복)
# 끄기: 슬라이드 meta.to_real_hold = 0, 프로젝트 전체 slides[0].meta.to_real_hold_off = true.
DEFAULT_TO_REAL_HOLD = 1.5
MAX_TO_REAL_HOLD = 6.0
TO_REAL_START_RATIO = 0.3                  # photo_fx.TO_REAL_HOLD 와 동일 (그림으로 머무는 비율)
_TO_REAL_STYLE_NAMES = ("brush", "wipe", "dissolve")   # photo_fx.TO_REAL_STYLES 와 동일
MIN_EASY_DURATION, MAX_EASY_DURATION = 1.0, 12.0


def _num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def is_free_length(item: dict) -> bool:
    """자유 길이 슬라이드 = 텍스트 없는 비디오 외 슬라이드 (렌더러 '텍스트 없음' 분기와 동일)."""
    return (item.get("slide_type") or "image") != "video" and not (item.get("text") or "").strip()


def free_base_duration(item: dict, default: float) -> float:
    """자유 길이 슬라이드의 기본 길이: meta.easy_duration(간편 자동 음악 맞춤) > 프로젝트 기본."""
    v = _num(_parse_meta(item.get("meta")).get("easy_duration"))
    if v is not None and v > 0:
        return max(MIN_EASY_DURATION, min(MAX_EASY_DURATION, v))
    return float(default or 3.0)


def to_real_reveal_end(duration: float, reveal) -> float:
    """그림→실사 드러나기가 끝나는 시각(초, 슬라이드 기준). photo_fx.to_real_progress 와 같은 식."""
    d = max(0.0, float(duration or 0.0))
    window = clamp_effect_reveal(reveal) * d
    start = max(0.0, min(TO_REAL_START_RATIO * d, d * 0.95 - window))
    return start + window


def to_real_hold_seconds(item: dict, hold_off: bool = False) -> float:
    """이 슬라이드에 필요한 실사 홀드(초). 0 = 해당 없음/꺼짐.
    대상: 이미지 슬라이드 + photo_effect != none + 유효한 meta.effect_to_real."""
    if hold_off:
        return 0.0
    if (item.get("slide_type") or "image") == "video" or not item.get("image_filename"):
        return 0.0
    if normalize_photo_effect(item.get("photo_effect")) == "none":
        return 0.0
    meta = _parse_meta(item.get("meta"))
    if str(meta.get("effect_to_real") or "").strip().lower() not in _TO_REAL_STYLE_NAMES:
        return 0.0
    v = _num(meta.get("to_real_hold"))
    if v is None:
        return DEFAULT_TO_REAL_HOLD
    return max(0.0, min(MAX_TO_REAL_HOLD, v))


def project_hold_off(items: list) -> bool:
    """slides[0].meta.to_real_hold_off == true → 프로젝트 전체 홀드 끔."""
    if not items:
        return False
    return _parse_meta(items[0].get("meta")).get("to_real_hold_off") is True


def to_real_min_duration(base: float, reveal, hold: float, next_transition,
                         next_dur, project_duration: float = 0.7) -> float:
    """reveal_end(base) + hold + 다음 슬라이드 진입 overlap ≤ D 를 만족하는 최소 길이 D (≥ base).
    next_transition/next_dur 가 None 이면 마지막 슬라이드 (overlap 없음)."""
    base = float(base or 0.0)
    if hold <= 0 or base <= 0:
        return base
    need0 = to_real_reveal_end(base, reveal) + hold
    d = max(base, need0)
    for _ in range(6):                     # overlap 은 min(전환 길이, 0.45 × min(D, next)) → 빠르게 수렴
        ov = 0.0 if next_dur is None else slide_overlap(next_transition, project_duration, d, next_dur)
        nd = max(base, need0 + ov)
        if abs(nd - d) < 1e-6:
            break
        d = nd
    return d


def apply_to_real_hold(durations: list, items: list, transitions: list,
                       project_duration: float = 0.7, hold_off=None) -> tuple[list, list]:
    """슬라이드 길이 목록에 그림→실사 홀드를 적용.
    반환: (new_durations, reveal_bases) — reveal_bases[i] 는 늘어난 슬라이드의 원래 길이(드러나기 기준), 아니면 None.
    뒤에서부터 계산해 다음 슬라이드의 최종 길이로 overlap 상한을 정확히 맞춘다."""
    n = len(durations)
    out = [float(d or 0.0) for d in durations]
    bases: list = [None] * n
    if hold_off is None:
        hold_off = project_hold_off(items)
    for i in range(n - 1, -1, -1):
        hold = to_real_hold_seconds(items[i], hold_off)
        if hold <= 0:
            continue
        nxt_t = (transitions[i + 1] if i + 1 < len(transitions) else "none") if i + 1 < n else None
        nxt_d = out[i + 1] if i + 1 < n else None
        need = to_real_min_duration(out[i], items[i].get("effect_reveal"), hold, nxt_t, nxt_d,
                                    project_duration)
        if need > out[i] + 1e-6:
            bases[i] = out[i]
            out[i] = need
    return out, bases


# ────────────────────────────────────────────────────────────────────────────
# 간편 자동 영상 클립 (구글 포토 등에서 가져온 휴대폰 영상)
# ────────────────────────────────────────────────────────────────────────────
# 클립당 최대 EASY_CLIP_MAX 초, 원본의 '가운데' 구간을 쓴다 (앞 EASY_CLIP_LEAD 초는 최소한 건너뜀).
# 휴대폰 영상은 녹화 버튼을 누르고 떼는 앞·끝이 흔들리고, 찍으려던 장면은 보통 가운데에 있다.
# 사진만 음악 길이에 맞춰 늘고 줄며, 영상 클립 길이는 고정이다 (easy_auto.solve_photo_scale).
EASY_CLIP_MAX = 6.0
EASY_CLIP_LEAD = 0.5
# 원본 오디오 기본값: 음소거 (BGM 필수 + 휴대폰 소리는 대부분 바람·잡음). slide.volume 이 덮어쓰기 손잡이.
EASY_VIDEO_VOLUME = 0.0


def easy_clip_window(full: float, max_len: float = EASY_CLIP_MAX,
                     lead: float = EASY_CLIP_LEAD) -> tuple[float, float]:
    """원본 길이 full → (trim_start, trim_end). 짧으면 전체, 길면 가운데 max_len 초."""
    full = max(0.0, float(full or 0.0))
    if full <= max_len:
        return 0.0, round(full, 3)
    ts = min(max(lead, (full - max_len) / 2.0), full - max_len)
    return round(ts, 3), round(ts + max_len, 3)


def static_slide_duration(item: dict, default: float, video_duration=None) -> float:
    """렌더 전 길이 추정 (간편 자동 길이 추정 / 미리보기 이웃 길이).
    자유 길이 → free_base_duration, 비디오 → 트림 구간(원본 길이는 video_duration(item) 콜백),
    텍스트 슬라이드 → 무TTS 렌더 규칙 max(3, 글자 × 0.15) (TTS 슬라이드는 근사)."""
    if (item.get("slide_type") or "image") == "video":
        ts = _num(item.get("trim_start")) or 0.0
        te = _num(item.get("trim_end")) or 0.0
        full = None
        if video_duration is not None:
            try:
                full = video_duration(item)
            except Exception:
                full = None
        if te > ts:
            return max(0.1, (min(te, full) if full else te) - ts)
        return max(0.1, (full or default) - ts)
    if is_free_length(item):
        return free_base_duration(item, default)
    return max(3.0, len((item.get("text") or "").strip()) * 0.15)


def plan_timeline(items: list, default: float, project_duration: float = 0.7,
                  video_duration=None) -> dict:
    """렌더러와 같은 규칙의 타임라인 추정: 길이(+홀드) → compute_slide_starts.
    반환: {durations, reveal_bases, starts, overlaps, total}"""
    if not items:
        return {"durations": [], "reveal_bases": [], "starts": [], "overlaps": [], "total": 0.0}
    base = [static_slide_duration(it, default, video_duration) for it in items]
    transitions = [it.get("transition") or "none" for it in items]
    durs, bases = apply_to_real_hold(base, items, transitions, project_duration)
    starts, overlaps = compute_slide_starts(durs, transitions, project_duration)
    return {"durations": durs, "reveal_bases": bases, "starts": starts, "overlaps": overlaps,
            "total": starts[-1] + durs[-1]}


# ────────────────────────────────────────────────────────────────────────────
# 프로젝트 단위 설정 (Project 에 meta 컬럼이 없어 첫 슬라이드 meta 에 둔다)
# ────────────────────────────────────────────────────────────────────────────
# 순서 변경·삭제로 첫 슬라이드가 바뀌어도 잃지 않도록, 읽을 때는 앞에서부터 처음 나오는 값을 쓰고
# 간편 자동 계획/음악 선택 때 slides[0] 으로 다시 모은다 (easy_auto.consolidate_project_meta).
PROJECT_META_KEYS: tuple[str, ...] = (
    "bgm_credit",       # {title, artist, license_url, page_url, jamendo_id}
    "show_bgm_credit",  # false → 영상 끝 크레딧 자막 끔 (기본 표시)
    "bgm_fade_out",     # BGM 끝 페이드 아웃(초). 없으면 기존 min(2.0, 길이 5%)
    "bgm_loop_xfade",   # BGM 루프 크로스페이드(초). 없으면 DEFAULT_BGM_LOOP_XFADE, 0 = 이어 붙이기
    "bgm_fit",          # 간편 자동 음악 맞춤 결과 {mode, scale, track_duration, video_length}
    "to_real_hold_off", # true → 그림→실사 홀드 끔
    "auto_plan",        # 자동 편집 계획 기록 {easy, seed, n_intro, stylize_ratio, ...} (다시 섞기용)
    "easy_settings",    # 간편 자동 옵션 {resolution, stylize_ratio, slide_duration, auto_curate}
    "user_ordered",     # true → 사용자가 순서를 직접 바꿈 (slide_order.reorder_slides)
)
DEFAULT_BGM_LOOP_XFADE = 1.5


def project_meta(items: list) -> dict:
    """슬라이드 목록(dict 또는 meta 문자열을 가진 객체)에서 PROJECT_META_KEYS 를 모은다."""
    out: dict = {}
    for it in items or []:
        raw = it.get("meta") if isinstance(it, dict) else getattr(it, "meta", None)
        m = _parse_meta(raw)
        for k in PROJECT_META_KEYS:
            if k not in out and k in m:
                out[k] = m[k]
        if len(out) == len(PROJECT_META_KEYS):
            break
    return out
