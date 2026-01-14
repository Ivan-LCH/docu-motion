# -----------------------------------------------------------------------------------------------------------------------------#
# Import
# -----------------------------------------------------------------------------------------------------------------------------#
import os
import asyncio
import json
import time
import shutil
import fitz  # PyMuPDF
import streamlit as st
import edge_tts
from pathlib import Path
from datetime import datetime
from PIL import Image
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips

# 모듈 참조
from google import genai
import youtube_manager

# -----------------------------------------------------------------------------------------------------------------------------#
# Set Environment
# -----------------------------------------------------------------------------------------------------------------------------#
PROJECT_NAME        = "DocuMotion"
VERSION             = "3.0.0"
BASE_DIR            = Path(__file__).parent
TEMP_DIR            = BASE_DIR / "temp"
OUTPUT_DIR          = BASE_DIR / "outputs"

GOOGLE_API_KEY      = os.getenv('GOOGLE_API_KEY')
client              = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None

# 디렉토리 보존 및 생성
for folder in [TEMP_DIR, OUTPUT_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title=f"{PROJECT_NAME} Studio", page_icon="🎬", layout="wide")

# -----------------------------------------------------------------------------------------------------------------------------#
# Function: Cleansing (파일 삭제 로직)
# -----------------------------------------------------------------------------------------------------------------------------#
def clear_work_directories():
    """ 설명: temp 및 outputs 폴더 내의 모든 파일을 삭제하여 용량을 확보합니다. """
    print(f"🧹 [System] 파일 클렌징 시작...", flush=True)
    for folder in [TEMP_DIR, OUTPUT_DIR]:
        for filename in os.listdir(folder):
            file_path = folder / filename
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"❌ 삭제 실패 {file_path}: {e}", flush=True)
    st.success("🧹 모든 임시 파일과 생성된 영상이 삭제되었습니다.")

# -----------------------------------------------------------------------------------------------------------------------------#
# Function: Video Rendering
# -----------------------------------------------------------------------------------------------------------------------------#
async def _make_audio(text, path):
    await edge_tts.Communicate(text, "ko-KR-SunHiNeural").save(path)

def render_video(data):
    total_slides = len(data)
    clips = []
    pbar = st.progress(0, text="🚀 렌더링 준비 중...")
    
    print(f"\n{'='*50}", flush=True)
    print(f"🎬 [Render] 영상 합성 시작", flush=True)
    
    for i, item in enumerate(data):
        if not item['text']: continue
        pbar.progress(i / total_slides, text=f"⏳ 슬라이드 {i+1}/{total_slides} 합성 중...")
        a_path = TEMP_DIR / f"v_{i}.mp3"
        asyncio.run(_make_audio(item['text'], str(a_path)))
        a_clip = AudioFileClip(str(a_path))
        i_clip = ImageClip(str(item['image'])).set_duration(a_clip.duration).set_audio(a_clip).set_fps(24)
        clips.append(i_clip)
    
    if not clips: return None
    
    pbar.progress(0.95, text="⚙️ 최종 파일 인코딩 중...")
    out = OUTPUT_DIR / f"DocuMotion_{datetime.now().strftime('%H%M%S')}.mp4"
    concatenate_videoclips(clips, method="compose").write_videofile(str(out), codec="libx264", audio_codec="aac", logger=None)
    pbar.empty()
    return out

# -----------------------------------------------------------------------------------------------------------------------------#
# Main UI
# -----------------------------------------------------------------------------------------------------------------------------#
if __name__ == "__main__":
    st.title(f"🎬 {PROJECT_NAME} Studio (v{VERSION})")

    with st.sidebar:
        st.header("📂 파일 업로드")
        pdf_in = st.file_uploader("PDF", type=["pdf"])
        img_in = st.file_uploader("이미지", type=["png", "jpg"], accept_multiple_files=True)
        st.divider()
        
        st.header("⚙️ 프로젝트 관리")
        auto_upload = st.checkbox("✅ 렌더링 후 유튜브 자동 업로드", value=False)
        if st.button("🧹 전체 파일 삭제 (Cleansing)", width='stretch'):
            clear_work_directories()
        st.divider()
        
        batch_btn = st.button("✨ AI 대사 자동 생성", width='stretch')
        render_btn = st.button("🚀 영상 렌더링 시작", type="primary", width='stretch')

    if pdf_in or img_in:
        if 'master_slides' not in st.session_state:
            assets = []
            if pdf_in:
                doc = fitz.open(stream=pdf_in.read(), filetype="pdf")
                for i in range(len(doc)):
                    target = TEMP_DIR / f"p_{i+1:02d}.png"
                    doc.load_page(i).get_pixmap(dpi=150).save(str(target))
                    assets.append({'path': target, 'label': f'Page {i+1}'})
            if img_in:
                for idx, img in enumerate(img_in):
                    target = TEMP_DIR / f"i_{idx+1:02d}.png"
                    with open(target, "wb") as f: f.write(img.getbuffer())
                    assets.append({'path': target, 'label': img.name})
            st.session_state.master_slides = assets
            st.session_state.scripts = {i: "" for i in range(len(assets))}
            st.success(f"📂 총 {len(assets)}개의 슬라이드가 로드되었습니다.")

        with st.expander("🛠️ JSON 대사 일괄 입력", expanded=False):
            json_input = st.text_area("JSON 데이터를 붙여넣으세요", height=100)
            if st.button("✅ JSON 대사 일괄 적용", width='stretch'):
                try:
                    data = json.loads(json_input)
                    for k, v in data.items():
                        idx = int(k)
                        if idx in st.session_state.scripts:
                            st.session_state.scripts[idx] = v
                            st.session_state[f"t_{idx}"] = v 
                    st.rerun()
                except Exception as e: st.error(f"오류: {e}")

        st.subheader("📑 편집 타임라인")
        for i, slide in enumerate(st.session_state.master_slides):
            with st.container(border=True):
                c1, c2 = st.columns([1, 2])
                with c1: st.image(str(slide['path']), width='stretch')
                with c2:
                    st.text_area(f"Slide {i+1}", key=f"t_{i}", height=120)
                    st.session_state.scripts[i] = st.session_state[f"t_{i}"]

        if render_btn:
            edit_data = [{"image": s['path'], "text": st.session_state.scripts[idx]} for idx, s in enumerate(st.session_state.master_slides)]
            video_file = render_video(edit_data)
            if video_file:
                st.session_state.last_v = str(video_file)
                st.video(st.session_state.last_v)
                if auto_upload:
                    url = youtube_manager.upload_short(st.session_state.last_v, f"Docu_{datetime.now().strftime('%m%d')}", "AI Video")
                    if url: st.success(f"✅ 자동 업로드 성공: {url}")

        if 'last_v' in st.session_state:
            st.divider()
            # [신규] 버튼 레이아웃 배치 (업로드 & 다운로드)
            col_yt, col_dl = st.columns(2)
            with col_yt:
                if st.button("📺 YouTube 수동 업로드", width='stretch'):
                    url = youtube_manager.upload_short(st.session_state.last_v, "Docu Video", "AI Video")
                    if url: st.success(f"성공: {url}")
            with col_dl:
                with open(st.session_state.last_v, "rb") as f:
                    st.download_button(
                        label="💾 동영상 다운로드 (.mp4)",
                        data=f,
                        file_name=os.path.basename(st.session_state.last_v),
                        mime="video/mp4",
                        width='stretch'
                    )
    else:
        st.info("사이드바에서 파일을 업로드하세요.")