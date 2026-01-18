# -----------------------------------------------------------------------------------------------------------------------------#
# 1. Import & Library
# -----------------------------------------------------------------------------------------------------------------------------#
# [OS/시스템] 파일 처리, 비동기, JSON 파싱, 정규식, 로깅
import os, asyncio, json, time, shutil, fitz, re, logging

# [UI] Streamlit 웹 인터페이스
import streamlit as st

# [TTS] Microsoft Edge TTS - 텍스트를 음성으로 변환
import edge_tts

# [유틸] 경로 처리 및 시간
from pathlib import Path
from datetime import datetime

# [이미지] PIL - 이미지 처리 (moviepy 호환성 패치 포함)
from PIL import Image
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS  # PIL 10.0+ 버전 호환성

# [영상처리] MoviePy - 이미지/오디오를 영상으로 합성
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, TextClip, CompositeVideoClip, ColorClip

# [외부API] YouTube 업로드 매니저, Google Gemini AI
import youtube_manager
from google import genai


# -----------------------------------------------------------------------------------------------------------------------------#
# 2. Logging Setup
# -----------------------------------------------------------------------------------------------------------------------------#
# 모듈별 로거 생성 - INFO 레벨 이상만 기록
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 중복 핸들러 방지: 핸들러가 없을 경우에만 추가
if not logger.handlers:
    stream_handler = logging.StreamHandler()                          # 콘솔 출력용 핸들러
    file_handler   = logging.FileHandler("app.log", encoding='utf-8') # 파일 기록용 핸들러 (app.log)
    formatter      = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')  # 로그 포맷: 시간-레벨-메시지

    stream_handler.setFormatter(formatter)
    file_handler  .setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)



# -----------------------------------------------------------------------------------------------------------------------------#
# 3. Configuration & Layout (와이드 레이아웃 유지)
# -----------------------------------------------------------------------------------------------------------------------------#
# [앱 정보] 버전 및 프로젝트명
VERSION        = "3.4.9 (YT Debug)"
PROJECT_NAME   = "DocuMotion Studio"

st.set_page_config(page_title=PROJECT_NAME, page_icon="🎬", layout="wide")

# [영상 렌더링 설정] 캔버스 크기, 폰트, 자막 스타일
CANVAS_SIZE    = (1280, 720)  # 720p HD 해상도
FONT_PATH      = "font.ttf"   # 자막용 폰트 파일 경로
FONT_SIZE      = 32           # 자막 폰트 크기 (px)
TEXT_COLOR     = 'white'      # 자막 텍스트 색상
BG_COLOR       = (0, 0, 0)    # 배경색 (검정)
YT_DESCRIPTION = "AI Video (가이드 Ivan, 슬라이드 NotebookLM, 자막 Gemini, 영상 자체프로그램)"  # 유튜브 기본 설명

# [환경 분기] 클라우드(Streamlit Cloud) vs 로컬 환경 구분
IS_CLOUD       = "STREAMLIT_RUNTIME_ENV" in os.environ

# [API 키 로딩] Streamlit Secrets에서 API 키 및 토큰 로드
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    # 클라우드 환경: secrets에서 YouTube 토큰을 파일로 추출
    if "YOUTUBE_TOKEN_JSON" in st.secrets:
        with open("token.json", "w", encoding="utf-8") as f:
            f.write(st.secrets["YOUTUBE_TOKEN_JSON"])
except Exception as e:
    st.error("🔑 Secrets 설정을 확인하세요."); st.stop()

# [디렉토리 설정] 임시 파일 및 출력 파일 저장 경로
BASE_DIR     = Path(__file__).parent
TEMP_DIR     = BASE_DIR / "temp"      # TTS 오디오, 추출된 이미지 저장
OUTPUT_DIR   = BASE_DIR / "outputs"   # 최종 렌더링된 영상 저장

# 디렉토리 자동 생성
for folder in [TEMP_DIR, OUTPUT_DIR]: folder.mkdir(parents=True, exist_ok=True)



# -----------------------------------------------------------------------------------------------------------------------------#
# 4. Helper Functions
# -----------------------------------------------------------------------------------------------------------------------------#

# ─────────────────────────────────────────────────────────────────────────────
# clear_work_directories: 작업 디렉토리 초기화
# - temp/, outputs/ 폴더 내 모든 파일/폴더 삭제
# - 새 프로젝트 시작 또는 수동 클렌징 시 호출
# ─────────────────────────────────────────────────────────────────────────────
def clear_work_directories():
    for folder in [TEMP_DIR, OUTPUT_DIR]:
        if folder.exists():
            for filename in os.listdir(folder):
                file_path = folder / filename
                try:
                    if os.path.isfile(file_path): os.unlink(file_path)
                    elif os.path.isdir(file_path): shutil.rmtree(file_path)
                except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
# split_sentences: 텍스트를 문장 단위로 분리
# - 정규식: 마침표/느낌표/물음표 뒤 공백 기준 분할
# - 자막 타이밍 계산에 사용 (문장별 표시 시간 산출)
# ─────────────────────────────────────────────────────────────────────────────
def split_sentences(text):
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s for s in sentences if s]

# ─────────────────────────────────────────────────────────────────────────────
# render_video: 슬라이드 데이터를 영상으로 렌더링
# - 입력: [{"image": Path, "text": str}, ...] 형태의 슬라이드 리스트
# - 처리 흐름:
#   1) 각 슬라이드의 텍스트 → TTS(edge_tts)로 오디오 생성 (ko-KR-SunHiNeural 음성)
#   2) 오디오 길이에 맞춰 이미지 클립 생성
#   3) 문장별 자막 클립 생성 (글자 수 비례로 표시 시간 계산)
#   4) 배경 + 이미지 + 자막 + 오디오 합성
#   5) 모든 슬라이드 연결 후 MP4 파일로 출력
# - 출력: 생성된 영상 파일 경로 (실패 시 None)
# - 진행률: 슬라이드 합성(0-50%) + 영상 인코딩(51-100%)
# ─────────────────────────────────────────────────────────────────────────────
from proglog import ProgressBarLogger

class StreamlitProgressLogger(ProgressBarLogger):
    """MoviePy 인코딩 진행률을 Streamlit 프로그레스바에 연결하는 커스텀 로거"""
    def __init__(self, progress_bar):
        super().__init__()
        self.progress_bar = progress_bar
    
    def bars_callback(self, bar, attr, value, old_value=None):
        # bar='t': 비디오 프레임 처리 진행률
        if bar == 't' and attr == 'index':
            total = self.bars[bar]['total']
            if total > 0:
                # 인코딩 진행률: 50% ~ 100% 구간
                encode_progress = int(value / total * 100)
                overall_progress = 50 + int(encode_progress * 0.5)
                self.progress_bar.progress(overall_progress, f"📼 영상 인코딩 중... ({encode_progress}%)")

def render_video(data):
    total_slides = len(data)
    final_clips = []
    
    # 통합 프로그레스바 생성
    progress_bar = st.progress(0, text="🚀 렌더링 준비 중...")
    
    try:
        for i, item in enumerate(data):
            if not item['text']: continue
            
            # 슬라이드 진행률: 0% ~ 50% 구간
            slide_progress = int((i / total_slides) * 50)
            progress_bar.progress(slide_progress, f"⏳ 슬라이드 {i+1}/{total_slides} 합성 중... ({slide_progress}%)")
            
            # Step 1: TTS 오디오 생성
            a_path         = TEMP_DIR / f"v_{i}.mp3"
            asyncio.run(edge_tts.Communicate(item['text'], "ko-KR-SunHiNeural").save(str(a_path)))
            a_clip         = AudioFileClip(str(a_path))
            total_duration = a_clip.duration
            
            # Step 2: 문장 분리 및 자막 타이밍 계산 준비
            sentences      = split_sentences(item['text'])
            total_chars    = sum(len(s) for s in sentences)
            
            # Step 3: 배경(검정) 및 이미지 클립 생성
            bg_clip        = ColorClip(size=CANVAS_SIZE, color=BG_COLOR).set_duration(total_duration)
            img_clip       = ImageClip(str(item['image'])).resize(height=int(CANVAS_SIZE[1] * 0.88)).set_position(('center', 'top')).set_duration(total_duration)
            
            # Step 4: 문장별 자막 클립 생성 (글자 수 비례 타이밍)
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
                
            # Step 5: 레이어 합성 (배경 → 이미지 → 자막) + 오디오 연결
            final_clips.append(CompositeVideoClip([bg_clip, img_clip] + subtitle_clips).set_audio(a_clip))

        # 슬라이드 합성 완료 (50%)
        progress_bar.progress(50, "📼 영상 인코딩 시작...")
        
        # 모든 슬라이드 연결 및 파일 출력 (커스텀 로거로 진행률 표시)
        out_path = OUTPUT_DIR / f"Docu_{datetime.now().strftime('%H%M%S')}.mp4"
        st_logger = StreamlitProgressLogger(progress_bar)
        concatenate_videoclips(final_clips, method="compose").write_videofile(str(out_path), fps=24, logger=st_logger)
        
        # 완료 (100%)
        progress_bar.progress(100, "✅ 렌더링 완료!")
        return out_path

    except Exception as e:
        st.error(f"렌더링 오류: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# upload_to_youtube: YouTube Shorts 업로드 처리
# - 입력: file_path(영상 경로), title(제목), description(설명)
# - youtube_manager 모듈을 통해 업로드 수행
# - 성공 시 URL 반환, 실패 시 에러 메시지 + 로깅
# - 토큰 만료, 할당량 초과 등의 에러 핸들링 포함
# ─────────────────────────────────────────────────────────────────────────────
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
# 앱의 메인 UI 구성
# - 사이드바: 파일 업로드, 유튜브 설정, 클렌징/렌더링 버튼
# - 메인 영역: 타임라인 에디터, JSON 일괄 입력, 영상 미리보기
# - 세션 상태: master_slides(슬라이드 목록), scripts(대사 텍스트), last_v(마지막 렌더링 영상)
# ===================================================================================================================
def main():
    st.title(f"🎬 {PROJECT_NAME} {VERSION}")
    
    # ─────────────────────────────────────────────────────────────────
    # 사이드바: 파일 업로드 및 설정
    # ─────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("📂 교육 자료 업로드")
        # 클라우드 환경: 이미지만 지원 (단일 파일)
        # 로컬 환경: PDF + 다중 이미지 지원
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
        # 수동 클렌징: 작업 디렉토리 및 세션 상태 초기화
        if st.button("🧹 수동 클렌징", width='stretch'):
            clear_work_directories()
            if 'master_slides' in st.session_state: del st.session_state.master_slides
            st.rerun()
        render_btn = st.button("🚀 영상 렌더링 시작", type="primary", width='stretch')

    # ─────────────────────────────────────────────────────────────────
    # 메인 영역: 파일 업로드 시 활성화
    # ─────────────────────────────────────────────────────────────────
    if pdf_in or img_in:
        # 새 파일 업로드 감지: 기존 작업 초기화
        if 'current_file_set' not in st.session_state:
            st.session_state.current_file_set = True
            clear_work_directories()
            if 'master_slides' in st.session_state: del st.session_state.master_slides

        # 슬라이드 목록 생성 (최초 1회만 실행)
        if 'master_slides' not in st.session_state:
            assets = []
            # PDF 처리: 각 페이지를 PNG 이미지로 변환 (150 DPI)
            if pdf_in and not IS_CLOUD:
                doc = fitz.open(stream=pdf_in.read(), filetype="pdf")
                for i in range(len(doc)):
                    target = TEMP_DIR / f"p_{i+1:02d}.png"
                    doc.load_page(i).get_pixmap(dpi=150).save(str(target))
                    assets.append({'path': target, 'label': f"Page {i+1}"})
            
            # 이미지 처리: temp 폴더에 저장
            if img_in:
                input_list = [img_in] if IS_CLOUD else img_in
                for idx, img in enumerate(input_list):
                    target = TEMP_DIR / f"i_{idx+1:02d}.png"; open(target, "wb").write(img.getbuffer())
                    assets.append({'path': target, 'label': img.name})
            
            # 세션 상태 초기화: 슬라이드 목록 + 대사 스크립트 딕셔너리
            st.session_state.master_slides = assets
            st.session_state.scripts = {i: "" for i in range(len(assets))}

        # JSON 일괄 입력: {슬라이드번호: 대사텍스트} 형식의 JSON 파싱
        with st.expander("🛠️ JSON 대사 일괄 입력", expanded=False):
            json_text = st.text_area("JSON 데이터를 붙여넣으세요")
            if st.button("✅ 일괄 적용", width='stretch'):
                try:
                    clean_json = re.sub(r'\[cite.*?\]', '', json_text)  # Gemini 인용 태그 제거
                    data = json.loads(clean_json)
                    for k, v in data.items():
                        idx = int(k); st.session_state.scripts[idx] = v
                        st.session_state[f"t_{idx}"] = v
                    st.rerun()
                except Exception as e: st.error(f"JSON 오류: {e}")

        # ─────────────────────────────────────────────────────────────
        # 타임라인 에디터: 슬라이드별 이미지 + 대사 입력
        # ─────────────────────────────────────────────────────────────
        st.subheader("📑 편집 타임라인")
        for i, slide in enumerate(st.session_state.master_slides):
            with st.container(border=True):
                c1, c2 = st.columns([1, 2])
                with c1: st.image(str(slide['path']), width='stretch')
                with c2:
                    st.text_area(f"Slide {i+1}", key=f"t_{i}", height=120)
                    st.session_state.scripts[i] = st.session_state[f"t_{i}"]

        # ─────────────────────────────────────────────────────────────
        # 렌더링 트리거: 영상 생성 및 자동 업로드
        # ─────────────────────────────────────────────────────────────
        if render_btn:
            render_data = [{
                "image" : s['path'], 
                "text"  : st.session_state.scripts[idx]
            } for idx, s in enumerate(st.session_state.master_slides)]

            video_file = render_video(render_data)
            if video_file:
                st.session_state.last_v = str(video_file)
                st.video(st.session_state.last_v)
                
                # 자동 업로드 활성화 시 렌더링 직후 유튜브 업로드
                if auto_upload:
                    upload_to_youtube(st.session_state.last_v, video_title_input, YT_DESCRIPTION)

        # ─────────────────────────────────────────────────────────────
        # 영상 후처리: 수동 업로드 및 다운로드 버튼
        # ─────────────────────────────────────────────────────────────
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
        # 파일 미업로드 상태: 세션 정리 및 안내 메시지
        if 'current_file_set' in st.session_state: del st.session_state.current_file_set
        st.info("파일을 업로드하여 시작하세요.")


# ===================================================================================================================
# 엔트리 포인트
# ===================================================================================================================
if __name__ == "__main__":
    main()


# ===================================================================================================================
# End of program
# ===================================================================================================================
