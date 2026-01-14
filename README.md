# 🎬 DocuMotion Studio Pro
AI 기반 기술 문서 영상 제작 자동화 솔루션

기술 PDF 문서나 이미지 슬라이드를 업로드하면, **Gemini 2.0 Flash**가 내용을 분석하여 전문적인 스크립트를 작성하고, **Edge-TTS**와 **MoviePy**를 통해 고품질 설명 영상을 자동으로 생성합니다.

---

## 🚀 주요 기능
* **AI 일괄 분석**: Gemini 2.0 Flash 모델을 사용한 슬라이드 맥락 분석
* **자동 TTS 합성**: 마이크로소프트 Edge-TTS(SunHi 보이스) 지원
* **스마트 렌더링**: 음성 길이에 맞춘 이미지 클립 자동 생성 및 MP4 합성
* **플랫폼 연동**: 유튜브 쇼츠 자동 업로드 및 로컬 다운로드 지원
* **파일 관리**: 서버 용량 확보를 위한 원클릭 클렌징 기능

---

## 🛠️ 설치 및 실행 방법

### 1. 환경 설정
프로젝트 루트에 `.env` 파일을 생성하고 아래 키를 입력하세요.
```env
GOOGLE_API_KEY=your_gemini_api_key
YOUTUBE_API_KEY=your_youtube_api_key