"""
DocuMotion - TTS 음성 관리 API (다중 음성)

Remote Qwen3-TTS 서버의 음성 클론 목록을 프록시한다.
  GET    /voices              — 등록된 음성 목록
  POST   /voices              — 새 음성 등록 (voice_name + ref_audio 3~10초 + ref_text)
  DELETE /voices/{voice_name} — 음성 삭제

프로젝트별 음성 선택은 Project.tts_voice (빈 값 → env TTS_VOICE_NAME 기본 음성).
"""
import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.core.config import TTS_SERVER_URL
from backend.core.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["voices"])


@router.get("/voices")
async def list_voices():
    """TTS 서버에 등록된 음성 목록."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{TTS_SERVER_URL}/voices")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning(f"음성 목록 조회 실패: {e}")
        raise HTTPException(status_code=502, detail="TTS 서버에 연결할 수 없습니다")


@router.post("/voices")
async def register_voice(
    voice_name: str = Form(...),
    ref_audio: UploadFile = File(...),
    ref_text: str = Form(""),
):
    """새 음성 클론 등록 — 서버 재시작에도 유지."""
    voice_name = voice_name.strip()
    if not voice_name or not voice_name.isalnum() or not voice_name.isascii():
        raise HTTPException(status_code=400, detail="음성 이름은 영문/숫자만 가능합니다 (공백/기호 불가)")
    audio = await ref_audio.read()
    if not audio:
        raise HTTPException(status_code=400, detail="참조 오디오 파일이 비어있습니다")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{TTS_SERVER_URL}/register_voice",
                data={"voice_name": voice_name, "ref_text": ref_text},
                files={"ref_audio": (ref_audio.filename or "ref.wav", audio,
                                     ref_audio.content_type or "audio/wav")},
            )
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail=f"음성 등록 실패: {r.text[:200]}")
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"음성 등록 실패: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"음성 등록 실패: {e}")


@router.delete("/voices/{voice_name}")
async def delete_voice(voice_name: str):
    """등록된 음성 삭제."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(f"{TTS_SERVER_URL}/voices/{voice_name}")
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail=f"음성 삭제 실패: {r.text[:200]}")
            return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"음성 삭제 실패: {e}")
        raise HTTPException(status_code=502, detail=f"음성 삭제 실패: {e}")
