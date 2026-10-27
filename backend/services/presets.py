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
    """hero/punch 문구 결정.
    우선순위: meta.{style}_text → meta.title → label(파일명 아님) → meta.caption
             → meta.name(장소) → 나레이션 첫 문장. punch 는 max_chars 로 자름."""
    style = normalize_text_style(style)
    if style not in ("hero", "punch"):
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
