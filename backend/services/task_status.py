"""
DocuMotion - 백그라운드 태스크 진행 상태 (Photo-Vlog F1/F2 공용)

렌더 status(Project.progress) 오염 없이 선별/캡션 생성 같은 백그라운드 작업의
진행률을 프론트에 폴링으로 제공한다. in-memory 이므로 재시작하면 소실 →
프론트는 404 를 받아 "다시 시도" 안내를 표시한다.
"""
from __future__ import annotations

import threading
from typing import Optional

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def set_status(task_id: str, **fields) -> None:
    with _lock:
        t = _tasks.setdefault(task_id, {"status": "pending", "progress": 0.0})
        t.update(fields)


def get_status(task_id: str) -> Optional[dict]:
    with _lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None


def clear(task_id: str) -> None:
    with _lock:
        _tasks.pop(task_id, None)
