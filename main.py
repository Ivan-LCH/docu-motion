# -----------------------------------------------------------------------------------------------------------------------------#
# 1. Import & Library
# -----------------------------------------------------------------------------------------------------------------------------#
import os, asyncio, json, time, shutil, fitz, re, logging
import streamlit as st
import edge_tts
from pathlib import Path
from datetime import datetime
from PIL import Image

if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, TextClip, CompositeVideoClip, ColorClip

import youtube_manager
from google import genai


# -----------------------------------------------------------------------------------------------------------------------------#
# 2. Logging Setup
# -----------------------------------------------------------------------------------------------------------------------------#
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    stream_handler = logging.StreamHandler()
    file_handler   = logging.FileHandler("app.log", encoding='utf-8')
    formatter      = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    stream_handler.setFormatter(formatter)
    file_handler  .setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)



# -----------------------------------------------------------------------------------------------------------------------------#
# 3. Configuration & Layout (와이드 레이아웃 유지)
# -----------------------------------------------------------------------------------------------------------------------------#
VERSION        = "3.4.9 (YT Debug)"
PROJECT_NAME   = "DocuMotion Studio"

st.set_page_config(page_title=PROJECT_NAME, page_icon="🎬", layout="wide")

CANVAS_SIZE    = (1280, 720) 
FONT_PATH      = "font.ttf"  
FONT_SIZE      = 32          
TEXT_COLOR     = 'white'     
BG_COLOR       = (0, 0, 0)
YT_DESCRIPTION = "AI Video (가이드 Ivan, 슬라이드 NotebookLM, 자막 Gemini, 영상 자체프로그램)"

IS_CLOUD       = "STREAMLIT_RUNTIME_ENV" in os.environ

try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    if "YOUTUBE_TOKEN_JSON" in st.secrets:
        with open("token.json", "w", encoding="utf-8") as f:
            f.write(st.secrets["YOUTUBE_TOKEN_JSON"])
except Exception as e:
    st.error("🔑 Secrets 설정을 확인하세요."); st.stop()

BASE_DIR     = Path(__file__).parent
TEMP_DIR     = BASE_DIR / "temp"
OUTPUT_DIR   = BASE_DIR / "outputs"

for folder in [TEMP_DIR, OUTPUT_DIR]: folder.mkdir(parents=True, exist_ok=True)



# -----------------------------------------------------------------------------------------------------------------------------#
# 4. Helper Functions
# -----------------------------------------------------------------------------------------------------------------------------#
def clear_work_directories():
    for folder in [TEMP_DIR, OUTPUT_DIR]:
        if folder.exists():
            for filename in os.listdir(folder):
                file_path = folder / filename
                try:
                    if os.path.isfile(file_path): os.unlink(file_path)
                    elif os.path.isdir(file_path): shutil.rmtree(file_path)
                except Exception: pass

def split_sentences(text):
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s for s in sentences if s]

def render_video(data):
    total_slides = len(data)
    final_clips = []
    with st.status("🚀 영상 렌더링 중...", expanded=True) as status:
        try:
            for i, item in enumerate(data):
                if not item['text']: continue
                st.write(f"⏳ {i+1}/{total_slides} 슬라이드 합성 중...")
                a_path         = TEMP_DIR / f"v_{i}.mp3"
                asyncio.run(edge_tts.Communicate(item['text'], "ko-KR-SunHiNeural").save(str(a_path)))
                a_clip         = AudioFileClip(str(a_path))
                total_duration = a_clip.duration
                sentences      = split_sentences(item['text'])
                total_chars    = sum(len(s) for s in sentences)
                bg_clip        = ColorClip(size=CANVAS_SIZE, color=BG_COLOR).set_duration(total_duration)
                img_clip       = ImageClip(str(item['image'])).resize(height=int(CANVAS_SIZE[1] * 0.88)).set_position(('center', 'top')).set_duration(total_duration)
                subtitle_clips = []
                current_start  = 0

                for s in sentences:
                    dur          = (len(s) / total_chars) * total_duration if total_chars > 0 else total_duration
                    txt_clip     = TextClip(
                        txt      = s, 
                        font     = FONT_PATH, 
                        fontsize = FONT_SIZE, 
                        color    = TEXT_COLOR, 
                        size     = (CANVAS_SIZE[0] - 100, None), 
                        method   = 'caption', 
                        align    = 'center'
                    ).set_start(current_start).set_duration(dur).set_position(('center', CANVAS_SIZE[1] - 75))
                    subtitle_clips.append(txt_clip)
                    current_start += dur
                final_clips.append(CompositeVideoClip([bg_clip, img_clip] + subtitle_clips).set_audio(a_clip))

            out_path = OUTPUT_DIR / f"Docu_{datetime.now().strftime('%H%M%S')}.mp4"
            concatenate_videoclips(final_clips, method="compose").write_videofile(str(out_path), fps=24, logger=None)
            status.update(label="✅ 렌더링 완료!", state="complete", expanded=False)
            return out_path

        except Exception as e:
            st.error(f"렌더링 오류: {e}")
            return None


def upload_to_youtube(file_path: str, title: str, description: str = "AI Video"):
    """유튜브 업로드 공통 함수"""
    with st.spinner(f"🚀 '{title}' 유튜브 업로드 중..."):
        try:
            url = youtube_manager.upload_short(
                file_path   = file_path,
                title       = title,
                description = description
            )
            if url:
                st.success(f"✅ 업로드 성공: {url}")
                return url
            else:
                st.error("❌ 유튜브 업로드 실패: URL을 반환하지 않았습니다. 인증 또는 할당량을 확인하세요.")
                return None
            
        except Exception as e:
            st.error(f"🚨 유튜브 업로드 중 예외 발생: {str(e)}")
            logger.error(f"YouTube Upload Exception: {e}", exc_info=True)
            return None


# ===================================================================================================================
# Main UI
# ===================================================================================================================
def main():
    st.title(f"🎬 {PROJECT_NAME} {VERSION}")
    
    with st.sidebar:
        st.header("📂 자산 업로드")
        if IS_CLOUD:
            pdf_in = None
            img_in = st.file_uploader("이미지", type=["png", "jpg"], key="cloud_up")
        else:
            pdf_in = st.file_uploader("기술 PDF", type=["pdf"], key="pdf_up")
            img_in = st.file_uploader("이미지", type=["png", "jpg"], accept_multiple_files=True, key="img_up")
        
        st.divider()
        st.header("⚙️ 유튜브 설정")
        video_title_input = st.text_input("📺 유튜브 제목", value="DocuMotion Video")
        auto_upload       = st.checkbox  ("✅ 유튜브 자동 업로드", value=False)
        
        st.divider()
        if st.button("🧹 수동 클렌징", width='stretch'):
            clear_work_directories()
            if 'master_slides' in st.session_state: del st.session_state.master_slides
            st.rerun()
        render_btn = st.button("🚀 영상 렌더링 시작", type="primary", width='stretch')

    if pdf_in or img_in:
        if 'current_file_set' not in st.session_state:
            st.session_state.current_file_set = True
            clear_work_directories()
            if 'master_slides' in st.session_state: del st.session_state.master_slides

        if 'master_slides' not in st.session_state:
            assets = []
            if pdf_in and not IS_CLOUD:
                doc = fitz.open(stream=pdf_in.read(), filetype="pdf")
                for i in range(len(doc)):
                    target = TEMP_DIR / f"p_{i+1:02d}.png"
                    doc.load_page(i).get_pixmap(dpi=150).save(str(target))
                    assets.append({'path': target, 'label': f"Page {i+1}"})
            
            if img_in:
                input_list = [img_in] if IS_CLOUD else img_in
                for idx, img in enumerate(input_list):
                    target = TEMP_DIR / f"i_{idx+1:02d}.png"; open(target, "wb").write(img.getbuffer())
                    assets.append({'path': target, 'label': img.name})
            st.session_state.master_slides = assets
            st.session_state.scripts = {i: "" for i in range(len(assets))}

        with st.expander("🛠️ JSON 대사 일괄 입력", expanded=False):
            json_text = st.text_area("JSON 데이터를 붙여넣으세요")
            if st.button("✅ 일괄 적용", width='stretch'):
                try:
                    clean_json = re.sub(r'\[cite.*?\]', '', json_text)
                    data = json.loads(clean_json)
                    for k, v in data.items():
                        idx = int(k); st.session_state.scripts[idx] = v
                        st.session_state[f"t_{idx}"] = v
                    st.rerun()
                except Exception as e: st.error(f"JSON 오류: {e}")

        st.subheader("📑 편집 타임라인")
        for i, slide in enumerate(st.session_state.master_slides):
            with st.container(border=True):
                c1, c2 = st.columns([1, 2])
                with c1: st.image(str(slide['path']), width='stretch')
                with c2:
                    st.text_area(f"Slide {i+1}", key=f"t_{i}", height=120)
                    st.session_state.scripts[i] = st.session_state[f"t_{i}"]

        if render_btn:
            render_data = [{"image": s['path'], "text": st.session_state.scripts[idx]} for idx, s in enumerate(st.session_state.master_slides)]
            video_file = render_video(render_data)
            if video_file:
                st.session_state.last_v = str(video_file)
                st.video(st.session_state.last_v)
                
                if auto_upload:
                    upload_to_youtube(st.session_state.last_v, video_title_input, YT_DESCRIPTION)

        if 'last_v' in st.session_state:
            st.divider()
            col1, col2 = st.columns(2)
            with col1:
                if st.button("📺 YouTube 수동 업로드", width='stretch'):
                    upload_to_youtube(st.session_state.last_v, video_title_input, YT_DESCRIPTION)
            with col2:
                with open(st.session_state.last_v, "rb") as f:
                    st.download_button("💾 동영상 다운로드", f, file_name=f"{video_title_input}.mp4")
    else:
        if 'current_file_set' in st.session_state: del st.session_state.current_file_set
        st.info("파일을 업로드하여 시작하세요.")

if __name__ == "__main__":
    main()
    