"""
DocuMotion - AI 나레이션 생성 서비스

Gemini Vision으로 슬라이드 이미지를 분석해 나레이션 초안을 만들고,
긴 스크립트를 슬라이드 수에 맞춰 자동 분할한다.

- GOOGLE_API_KEY 없거나 실패 시 빈 문자열/None 반환 (호출자가 graceful 처리)
- 환각 방지: 사진에 보이는 것만 묘사하도록 프롬프트에서 명시
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

import httpx

from backend.core.config import GOOGLE_API_KEY, GEMINI_MODEL
from backend.core.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = 30.0

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
}


def _post_gemini(parts: list, generation_config: Optional[dict] = None) -> Optional[str]:
    """Gemini generateContent 호출 → 텍스트. 실패 시 None. 429은 5초 후 1회 재시도."""
    if not GOOGLE_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": generation_config or {"temperature": 0.7},
    }
    for attempt in range(2):
        try:
            r = httpx.post(url, json=payload, timeout=_TIMEOUT)
            if r.status_code == 429 and attempt == 0:
                logger.warning("Gemini 429 (rate limit) — 5초 후 재시도")
                import time
                time.sleep(5)
                continue
            if r.status_code != 200:
                logger.warning(f"Gemini 응답 {r.status_code}: {r.text[:160]}")
                return None
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.warning(f"Gemini 호출 실패: {e}")
            return None
    return None


def generate_narration_for_image(image_path: Path, project_title: str = "",
                                 tone: str = "documentary") -> str:
    """
    슬라이드 이미지 1장 → 한국어 나레이션 초안 (2~3문장).
    tone: "documentary"(기본, 기존 동작) | "vlog"(캐주얼 1인칭)
    실패 시 빈 문자열.
    """
    if not image_path.exists():
        return ""
    mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")

    title_hint = f"영상 주제는 '{project_title}'이다. " if project_title else ""
    if tone == "vlog":
        tone_rule = "- 밝고 캐주얼한 브이로그 톤, 친구에게 말하듯 1인칭 ('~했어요', '~더라고요')"
    else:
        tone_rule = "- 구어체, 담담하고 따뜻한 다큐멘터리 톤"
    prompt = (
        "이 사진은 사진 슬라이쇼 영상의 한 장면이다. "
        f"{title_hint}"
        "이 장면 위에 얹을 나레이션(낭독용 대사)을 한국어로 2~3문장 작성하라.\n"
        "규칙:\n"
        "- 사진에 실제로 보이는 것만 묘사할 것 (장소명/날짜/인물 이름 등 보이지 않는 사실 지어내기 금지)\n"
        f"{tone_rule}\n"
        "- '사진에는', '이미지에는' 같은 메타 표현 금지, 장면을 직접 말할 것\n"
        "- 문장만 출력 (따옴표/머리말/설명 없이)"
    )
    text = _post_gemini([
        {"text": prompt},
        {"inline_data": {"mime_type": mime, "data": b64}},
    ])
    if not text:
        return ""
    # 따옴표로 감싸져 오는 경우 제거
    return text.strip().strip('"').strip("'")


_SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array", "items": {"type": "string"},
            "description": "슬라이드 순서대로 배정된 나레이션. 정확히 요청 개수만큼.",
        },
    },
    "required": ["parts"],
}


def split_script(script: str, num_parts: int) -> list[str]:
    """
    긴 스크립트를 num_parts개 슬라이드용 나레이션으로 분할.
    반환 길이는 항상 num_parts (부족분은 빈 문자열). 실패 시 빈 리스트.
    """
    if num_parts <= 0 or not script.strip():
        return []
    prompt = (
        f"아래 스크립트를 사진 슬라이쇼 영상의 {num_parts}개 장면에 얹을 나레이션으로 나누어라.\n"
        "규칙:\n"
        "- parts 배열 길이는 정확히 "
        f"{num_parts}개\n"
        "- 원문의 문장을 최대한 보존하되, 문장 중간이 끊기지 않게 배분\n"
        "- 각 파트는 1~3문장, 낭독하기 자연스럽게\n"
        "- 원문에 없는 내용 추가 금지\n\n"
        f"스크립트:\n{script}"
    )
    text = _post_gemini(
        [{"text": prompt}],
        generation_config={
            "responseMimeType": "application/json",
            "responseSchema": _SPLIT_SCHEMA,
            "temperature": 0.3,
        },
    )
    if not text:
        return []
    try:
        data = json.loads(text)
        parts = [str(p).strip() for p in data.get("parts", [])]
    except Exception as e:
        logger.warning(f"스크립트 분할 파싱 실패: {e}")
        return []
    # 개수 보정: 부족하면 빈 문자열, 넘치면 마지막에 합침
    if len(parts) < num_parts:
        parts += [""] * (num_parts - len(parts))
    elif len(parts) > num_parts:
        parts = parts[: num_parts - 1] + [" ".join(parts[num_parts - 1:])]
    return parts
