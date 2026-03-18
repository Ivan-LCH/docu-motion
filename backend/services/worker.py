"""
DocuMotion - Background Rendering Worker
FastAPI BackgroundTasks로 호출되는 렌더링 워커
"""
from datetime import datetime
from pathlib import Path

from backend.core.config import OUTPUTS_DIR
from backend.core.logger import get_logger
from backend.db.session import SessionLocal
from backend.db.models import Project, Slide
from backend.services import renderer
from backend.services.tts_manager import TTSEngine

logger = get_logger(__name__)


def run_render(project_id: str):
    """
    BackgroundTasks.add_task(run_render, project_id) 으로 호출됨
    별도 스레드에서 실행되므로 새로운 DB 세션 생성 필요
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            logger.error(f"Project not found for rendering: {project_id}")
            return

        # 상태: PROCESSING
        project.status     = "PROCESSING"
        project.progress   = 0
        project.message    = "렌더링 시작..."
        project.updated_at = datetime.utcnow()
        db.commit()

        # 슬라이드 로드
        slides = (
            db.query(Slide)
            .filter(Slide.project_id == project_id)
            .order_by(Slide.order_index)
            .all()
        )
        slides_data = [
            {
                "image_filename": s.image_filename,
                "text": s.text,
                "slide_type": s.slide_type or "image",
                "video_filename": s.video_filename or "",
                "volume": s.volume if s.volume is not None else 1.0,
                "subtitles": s.subtitles or "[]",
                "use_tts": (s.use_tts if s.use_tts is not None else 1),
            }
            for s in slides
        ]

        assets_dir  = OUTPUTS_DIR / project_id / "assets"
        output_file = OUTPUTS_DIR / project_id / "result.mp4"

        # progress_callback: renderer에서 (percent, message)로 호출
        # MoviePy logger: **kwargs(message=...) 형태로 호출 - 충돌 가능성 있으므로 래퍼 처리
        def progress_callback(percent: int = 0, message: str = ""):
            try:
                db.query(Project).filter(Project.id == project_id).update(
                    {"status": "PROCESSING", "progress": percent,
                     "message": message, "updated_at": datetime.utcnow()}
                )
                db.commit()
                logger.info(f"[{project_id}] {percent}% - {message}")
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")

        # TTS 모델 사전 로드
        tts = TTSEngine()
        progress_callback(5, "TTS AI 모델을 GPU 메모리에 불러오는 중...")
        tts.load_model()

        # TTS 완료 시 GPU 메모리 해제 콜백 (비디오 인코딩으로 GPU 재활용)
        def on_tts_done():
            logger.info("TTS 완료 - GPU 메모리 해제 후 NVENC 인코딩 준비")
            progress_callback(50, "TTS 완료 - GPU 메모리 해제 중...")
            tts.unload_model()

        # 렌더링 실행
        renderer.render_project(
            project_id=project_id,
            slides=slides_data,
            assets_dir=assets_dir,
            output_file=output_file,
            progress_callback=progress_callback,
            on_tts_done=on_tts_done
        )

        # 완료
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            project.status     = "COMPLETED"
            project.progress   = 100
            project.message    = "렌더링 완료!"
            project.updated_at = datetime.utcnow()
            db.commit()
        logger.info(f"Render completed: {project_id}")

    except Exception as e:
        logger.error(f"Render failed: {project_id} - {e}", exc_info=True)
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                project.status     = "ERROR"
                project.message    = f"오류: {str(e)[:200]}"
                project.updated_at = datetime.utcnow()
                db.commit()
        except Exception as inner_e:
            logger.error(f"Failed to update error status: {inner_e}")
    finally:
        # 렌더링(성공/실패) 종료 후 메모리 네 넌지 모델 언로드 (추가 보호로)
        try:
            tts = TTSEngine()
            tts.unload_model()
        except Exception:
            pass
        db.close()
