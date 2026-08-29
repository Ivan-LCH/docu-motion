"""
DocuMotion - FastAPI 메인 엔트리포인트
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.core.config import APP_NAME, VERSION, OUTPUTS_DIR
from backend.core.logger import setup_logging, get_logger
from backend.db.session import init_db
from backend.api.v1 import projects, slides, render, youtube, photos, preview, locations, narration, organize, curation

# ─────────────────────────────────────────────
# 시작/종료 이벤트
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = get_logger("main")
    logger.info(f"Starting {APP_NAME} {VERSION}")
    init_db()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    logger.info("Shutting down...")


# ─────────────────────────────────────────────
# FastAPI 앱
# ─────────────────────────────────────────────
app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    lifespan=lifespan
)

# CORS (React 개발 서버 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# API 라우터 등록
# ─────────────────────────────────────────────
PREFIX = "/api/v1"
app.include_router(projects.router, prefix=PREFIX)
app.include_router(slides.router,   prefix=PREFIX)
app.include_router(render.router,   prefix=PREFIX)
app.include_router(youtube.router,  prefix=PREFIX)
app.include_router(photos.router,   prefix=PREFIX)
app.include_router(preview.router,  prefix=PREFIX)
app.include_router(locations.router, prefix=PREFIX)
app.include_router(narration.router, prefix=PREFIX)
app.include_router(organize.router,  prefix=PREFIX)
app.include_router(curation.router,  prefix=PREFIX)


# ─────────────────────────────────────────────
# 정적 에셋 서빙 (프로젝트 이미지)
# ─────────────────────────────────────────────
@app.get("/assets/{project_id}/{filename:path}")
async def serve_asset(project_id: str, filename: str):
    """프로젝트 이미지 파일 서빙"""
    file_path = OUTPUTS_DIR / project_id / "assets" / filename
    if not file_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


# ─────────────────────────────────────────────
# React 프론트엔드 정적 파일 서빙
# ─────────────────────────────────────────────
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
else:
    @app.get("/")
    async def root():
        return {"app": APP_NAME, "version": VERSION, "status": "API only mode (frontend not built)"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
