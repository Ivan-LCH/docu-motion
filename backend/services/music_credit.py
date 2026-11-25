"""
DocuMotion - BGM 크레딧 문구 (Jamendo CC 라이선스)

slides[0].meta.bgm_credit = {title, artist, license_url, page_url, jamendo_id}
  · 영상 끝 자막:   '♪ 제목 — 아티스트 · Jamendo (CC BY-SA 3.0)'
  · YouTube 설명:   '음악: "제목" by 아티스트 (Jamendo) — <license_url>'
렌더러와 간편 자동 API 가 같이 쓴다 (moviepy 비의존).
"""
from __future__ import annotations

import re
from typing import Optional

_CC_RE = re.compile(r"creativecommons\.org/(?:licenses|publicdomain)/([a-z\-]+)/?([\d.]+)?", re.I)


def license_short(url: str) -> str:
    """CC 라이선스 URL → 'CC BY-NC-SA 3.0'. 알 수 없으면 ''."""
    m = _CC_RE.search(url or "")
    if not m:
        return ""
    kind = m.group(1).lower()
    if kind in ("zero", "mark"):
        return "CC0" if kind == "zero" else "Public Domain"
    ver = f" {m.group(2)}" if m.group(2) else ""
    return f"CC {kind.upper()}{ver}"


def _clean(v) -> str:
    return " ".join(str(v or "").split())[:120]


def credit_line(credit: Optional[dict]) -> str:
    """영상 끝에 작게 넣는 한 줄. 제목이 없으면 ''."""
    if not isinstance(credit, dict):
        return ""
    title, artist = _clean(credit.get("title")), _clean(credit.get("artist"))
    if not title:
        return ""
    lic = license_short(str(credit.get("license_url") or ""))
    line = f"♪ {title}" + (f" — {artist}" if artist else "") + " · Jamendo"
    return line + (f" ({lic})" if lic else "")


def credit_description(credit: Optional[dict]) -> str:
    """YouTube 설명란용 크레딧 문구 (music_credit.txt)."""
    if not isinstance(credit, dict) or not _clean(credit.get("title")):
        return ""
    title, artist = _clean(credit.get("title")), _clean(credit.get("artist"))
    lic_url = str(credit.get("license_url") or "").strip()
    text = f'음악: "{title}"' + (f" by {artist}" if artist else "") + " (Jamendo)"
    if lic_url:
        text += f" — {lic_url}"
    page = str(credit.get("page_url") or "").strip()
    if page:
        text += f"\n곡 페이지: {page}"
    return text
