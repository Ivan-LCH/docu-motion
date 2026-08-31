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
                "trim_start": s.trim_start or 0.0,
                "trim_end": s.trim_end or 0.0,
                "transition": s.transition or "none",
                "tts_volume": s.tts_volume if s.tts_volume is not None else 1.0,
                "rotation": s.rotation if s.rotation is not None else 0,
                "overlays": s.overlays or "[]",
                "image_fit": s.image_fit or "cover",
                "ken_burns": s.ken_burns if s.ken_burns is not None else 0,
                "meta": s.meta or "{}",
            }
            for s in slides
        ]

        assets_dir  = OUTPUTS_DIR / project_id / "assets"
        output_file = OUTPUTS_DIR / project_id / "result.mp4"

        # BGM 정보
        bgm_filename = getattr(project, 'bgm_filename', '') or ''
        bgm_vol      = getattr(project, 'bgm_volume', 0.3) or 0.3
        bgm_path     = str(assets_dir / bgm_filename) if bgm_filename else ''

        # 화면 비율 + 해상도 (8-9)
        from backend.services.renderer import ASPECT_RATIO_MAP
        aspect_ratio = getattr(project, 'aspect_ratio', '16:9') or '16:9'
        resolution   = getattr(project, 'resolution', '720p') or '720p'
        _base        = ASPECT_RATIO_MAP.get(aspect_ratio, (1280, 720))
        _scale       = 1.5 if resolution == '1080p' else 1.0
        canvas_size  = (int(_base[0] * _scale), int(_base[1] * _scale))

        # 전환 효과 길이 (8-10)
        transition_duration = getattr(project, 'transition_duration', 0.7) or 0.7

        # 자동 연출 스타일 (Phase A)
        style_preset = getattr(project, 'style_preset', 'none') or 'none'

        # Master TTS Volume
        tts_master_volume = getattr(project, 'tts_master_volume', 1.0) or 1.0

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

        # TTS 모델 사전 로드 (최대 3회 재시도, 실패 시 edge-tts 전용 모드)
        tts = TTSEngine(voice_name=(getattr(project, 'tts_voice', '') or ''))
        tts_loaded = False
        progress_callback(5, "TTS AI 모델을 GPU 메모리에 불러오는 중...")
        for attempt in range(1, 4):
            if tts.load_model():
                tts_loaded = True
                break
            logger.warning(f"TTS load attempt {attempt}/3 failed")
            if attempt < 3:
                import time; time.sleep(2)

        if not tts_loaded:
            logger.warning("TTS 서버 로드 실패 → edge-tts 전용 모드로 렌더링 진행")
            progress_callback(8, "TTS 서버 미응답 — edge-tts 폴백 모드로 진행...")

        # TTS 완료 시 GPU 메모리 해제 콜백 (비디오 인코딩으로 GPU 재활용)
        def on_tts_done():
            logger.info("TTS 완료 - GPU 메모리 해제 후 NVENC 인코딩 준비")
            progress_callback(50, "TTS 완료 - GPU 메모리 해제 중...")
            if tts_loaded:
                tts.unload_model()

        # 전역 설정 (6-10)
        subtitle_font_size = getattr(project, 'subtitle_font_size', 30) or 30
        subtitle_font_color = getattr(project, 'subtitle_font_color', 'white') or 'white'
        watermark_text = getattr(project, 'watermark_text', '') or ''
        watermark_opacity = getattr(project, 'watermark_opacity', 0.3) or 0.3
        default_slide_duration = getattr(project, 'default_slide_duration', 3.0) or 3.0
        # 인트로 타이틀: 전역 설정 입력이 비어있으면 프로젝트명을 사용
        title_text = (getattr(project, 'title_text', '') or '').strip() or (project.name or '').strip()

        # 렌더링 실행
        renderer.render_project(
            project_id=project_id,
            slides=slides_data,
            assets_dir=assets_dir,
            output_file=output_file,
            progress_callback=progress_callback,
            on_tts_done=on_tts_done,
            bgm_path=bgm_path,
            bgm_volume=bgm_vol,
            canvas_size=canvas_size,
            tts_master_volume=tts_master_volume,
            subtitle_font_size=subtitle_font_size,
            subtitle_font_color=subtitle_font_color,
            watermark_text=watermark_text,
            watermark_opacity=watermark_opacity,
            default_slide_duration=default_slide_duration,
            title_text=title_text,
            transition_duration=transition_duration,
            style_preset=style_preset,
            tts_voice=(getattr(project, 'tts_voice', '') or ''),
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
