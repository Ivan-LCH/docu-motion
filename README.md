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