"""
DocuMotion - Centralized Logger
"""
import logging
import sys
from pathlib import Path
from backend.core.config import LOGS_DIR, DEBUG

LOGS_DIR.mkdir(parents=True, exist_ok=True)

def setup_logging():
    level = logging.DEBUG if DEBUG else logging.INFO
    fmt   = "%(asctime)s [%(levelname)8s] %(name)s - %(message)s"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "backend.log", encoding="utf-8"),
    ]

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # 외부 라이브러리 로그 제한
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("moviepy").setLevel(logging.WARNING)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
