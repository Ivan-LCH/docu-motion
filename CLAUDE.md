# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DocuMotion Studio — a full-stack AI-powered video creation platform that converts images, PDFs, and video clips into narrated videos with TTS, background music, transitions, and YouTube upload support.

## Tech Stack

- **Backend**: FastAPI (Python 3.11), SQLAlchemy 2.0 + SQLite (WAL mode), MoviePy + FFmpeg for video rendering
- **Frontend**: React 18 + TypeScript, Vite 5, React Router DOM
- **TTS**: Remote Qwen3-TTS server via HTTP API (with edge-tts fallback)
- **Infrastructure**: Docker container with NVIDIA CUDA/NVENC GPU support
- **Video codec**: `h264_nvenc` (GPU-accelerated H.264 encoding)

## Development Commands

```bash
# Start everything (builds frontend, launches backend)
bash start_server.sh

# Backend only (auto-reloads on Python file changes)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend build (must be run manually by user — AI should only request this)
cd frontend && npm run build

# Frontend dev server (proxies /api/* to localhost:8000)
cd frontend && npm run dev

# Stop server
bash stop_server.sh
```

No test suite is currently configured.

## Architecture

### Request Flow

```
Browser → React SPA → /api/v1/* → FastAPI → Services → SQLite / FFmpeg / TTS API
                ↓
         /assets/{project_id}/* (static file serving with range requests)
```

### Backend Structure (`backend/`)

- `main.py` — FastAPI app entry, mounts static files from `frontend/dist/`, serves SPA catch-all
- `api/v1/` — Route modules: `projects.py`, `slides.py`, `render.py`, `youtube.py`
- `services/` — Business logic:
  - `renderer.py` — MoviePy-based video composition (image slides, video clips, transitions, subtitles, BGM mixing)
  - `tts_manager.py` — Remote TTS API client (calls Qwen3-TTS server, voice cloning support)
  - `worker.py` — Background rendering pipeline: TTS generation → video encoding → status tracking
  - `youtube_manager.py` — YouTube Data API v3 OAuth2 upload
- `db/models.py` — SQLAlchemy ORM: `Project` and `Slide` models with cascade delete
- `db/session.py` — DB session management, `init_db()` auto-creates schema on startup
- `schema/project.py` — Pydantic request/response schemas
- `core/config.py` — Environment variables and path constants
- `core/logger.py` — Centralized logging (file + console, external libs muted to WARNING)

### Frontend Structure (`frontend/src/`)

- `pages/Dashboard.tsx` — Project list & creation
- `pages/Editor.tsx` — Main slide editor with drag-and-drop reordering, rendering controls, and YouTube upload
- `api/client.ts` — Typed API client (all backend calls go through here)
- `components/ToastContext.tsx` — Toast notification system

### Rendering Pipeline (worker.py → renderer.py)

1. Project status: `DRAFT → QUEUED → PROCESSING → COMPLETED` (or `ERROR`)
2. TTS phase: generates speech WAV files for each slide's narration text via remote API
3. Video encoding phase: composites image/video slides with audio, transitions, and BGM
4. Output: `outputs/{project_id}/result.mp4`

### Slide Types

- **Image slides**: static image + TTS narration text + transition
- **Video slides**: video clip with trim (start/end), volume control, optional TTS overlay, JSON subtitles

### Aspect Ratios

`16:9` (1280×720), `9:16` (720×1280), `1:1` (720×720)

## Key Paths

| Path | Purpose |
|------|---------|
| `data/docu_motion.db` | SQLite database |
| `outputs/{project_id}/` | Rendered videos and uploaded assets |
| `logs/backend.log` | Application log file |
| `resources/font.ttf` | Font used for subtitle rendering |
| `.env` | API keys (TTS_SERVER_URL, YOUTUBE_API_KEY, etc.) |

## Development Policies (from .cursorrules)

- All development happens inside the Docker container (mounted at `/DATA/my_prog/docu_motion`)
- Backend auto-reloads via uvicorn `--reload` — do not manually restart the server
- Frontend builds (`npm run build`) must be triggered by the user, not by AI autonomously
- When the user wants to review or discuss changes, explain the approach first and get agreement before modifying code
