# 🎬 DocuMotion Studio Pro
AI 기반 기술 문서 영상 제작 자동화 솔루션 (v3.1.0)

기술 PDF 문서나 이미지 슬라이드를 업로드하면, **Gemini 2.0 Flash**가 전체 내용을 통합 분석하여 전문적인 스크립트를 작성합니다. 이후 **Edge-TTS**의 자연스러운 음성과 **MoviePy**의 엔진을 거쳐 고품질 MP4 설명 영상을 자동으로 생성합니다.

---

## 🚀 주요 기능
* **Gemini 2.0 Batch 분석**: 여러 슬라이드를 한 번의 호출로 분석하여 전체 맥락이 이어지는 대본 생성
* **고성능 TTS**: Microsoft Edge-TTS(ko-KR-SunHiNeural)를 통한 아나운서급 음성 합성
* **스마트 렌더링**: 각 슬라이드별 대사 길이를 자동으로 계산하여 영상 클립 시간 동기화
* **유튜브 쇼츠 연동**: 제작 완료 후 즉시 YouTube Shorts 업로드 기능 (자동/수동 선택 가능)
* **파일 관리 시스템**: 프로젝트 초기화 및 임시 파일 삭제(Cleansing) 기능 탑재
* **사진 효과**: 슬라이드마다 사진을 연필 스케치(`sketch`) 또는 만화(`cartoon`)로 바꿀 수 있다. 기본은 슬라이드 길이의 45% 동안 원본에서 효과로 크로스페이드(`effect_reveal`, 0=바로 적용 ~ 1=전체 구간)하며, Ken Burns·장면 전환과 함께 쓴다. 필터는 슬라이드당 한 번만 계산한다.
* **자동 편집 (1단계)**: 에디터 「🎬 자동 편집」 또는 `POST /api/v1/projects/{id}/auto-edit`. BGM 위 음악 전용 하이라이트로 자동 연출한다 — 첫 ~5초 무나레이션 음악 인트로 + 제목 키네틱 타이포, 장소가 바뀔 때 좌측 상단 📍 위치 카드(EXIF GPS 역지오코딩: 카카오 → Nominatim, GPS 없으면 장소 슬라이드 주소 폴백), 중간 2~3컷 짧은 키네틱 문구(흰 글씨 + 노랑 강조), 인트로 뒤 zoom_through·장소 전환 시 whip. BGM 필수. 샘플(원본 보존, 복제본 렌더): `/workspace/venvs/docu/bin/python scripts/render_auto_edit_sample.py --project <id> --clone-name "<이름>" --bgm <파일> --resolution 720p --out-dir <폴더>`
* **인코더 선택**: `VIDEO_CODEC` 환경변수(예: `h264_nvenc`, `libx264`)를 우선 사용. 비우면 NVIDIA 장치가 있을 때만 NVENC를 시도하고, CPU 박스는 바로 libx264로 인코딩한다.

---

## 🛠️ 설치 및 실행 방법

### 1. 환경 설정
프로젝트 루트 폴더에 `.env` 파일을 생성하고 아래의 API 키들을 입력하세요.
(주의: 이 파일은 .gitignore에 등록되어 GitHub에 노출되지 않습니다.)
- GOOGLE_API_KEY=발급받은_제미나이_키
- YOUTUBE_API_KEY=발급받은_유튜브_키

### 2. 필수 패키지 설치
가상환경(venv) 또는 로컬 환경에서 아래 명령어를 실행하세요.
- pip install -r requirements.txt

### 3. 애플리케이션 구동
- streamlit run main.py

---

## 📂 프로젝트 구조
* `main.py`: Streamlit 기반 웹 UI 및 비즈니스 로직
* `youtube_manager.py`: YouTube Data API v3 연동 모듈
* `temp/`: TTS 음성 및 이미지 처리를 위한 임시 저장소
* `outputs/`: 최종 렌더링된 MP4 파일 저장소
* `.gitignore`: 보안 및 불필요 파일 제외 설정

---
© 2026 DocuMotion Studio. All rights reserved.