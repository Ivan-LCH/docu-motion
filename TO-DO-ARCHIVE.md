================================================================================
 docu_motion - TO-DO ARCHIVE
================================================================================

## 2026-03-19 (검토완료)

──────────────────────────────────────────────────────────────────────────────
1. 🎬 비디오 렌더링 파이프라인 (Video Rendering & Encoding)
──────────────────────────────────────────────────────────────────────────────
  - [C] 1-1. [renderer.py] 비디오 슬라이드 자막(subtitles) + TTS 복합 오디오 품질 개선
        . 현황: CompositeAudioClip으로 원본 오디오 + TTS 오디오를 믹싱하는 구조 작동 중
        . 개선: TTS 볼륨 독립 조절 + 원본 오디오 fade-out/in 처리
        . 완료: Slide.tts_volume 컬럼 추가, TTS 구간에서 원본 오디오 duck(30%) + fade 처리, UI 슬라이더 추가
  - [C] 1-2. [renderer.py] 이미지 슬라이드 Ken Burns(패닝·줌) 효과 적용
        . 현황: `ImageClip`을 정적으로 표시만 함 (set_position('center', 'top'))
        . 개선: 슬라이드별 이미지 확대·이동 효과로 영상미 향상
        . 완료: 짝수 슬라이드 줌인(1.0→1.08), 홀수 슬라이드 줌아웃(1.0→0.93) 적용
  - [C] 1-3. [renderer.py] 렌더링 실패 시 temp_render 디렉터리 정리 방어 로직 보완
        . 현황: except 블록에서 shutil.rmtree로 전체 삭제하여 TTS wav 캐시도 날아감
        . 개선: 실패 시 wav 캐시만 보존하고 중간 결과물만 삭제
        . 완료: .wav 이외의 파일만 삭제하도록 변경
  - [C] 1-4. [renderer.py + models.py] 장면 전환 효과 (Scene Transitions)
        . 현황: concatenate_videoclips로 hard cut만 지원
        . 개선 (Phase 1): crossfade, fade_to_black 지원
        . 개선 (Phase 2): slide_left, slide_right 추가
        . DB: Slide 모델에 transition 컬럼 추가 (default: 'none')
        . UI: SlideCard 하단에 전환 효과 드롭다운
  - [C] 1-5. [renderer.py + models.py] BGM 배경음악 지원
        . 현황: 원본 오디오 + TTS만 지원, BGM 없음
        . 개선: Project 모델에 bgm_filename/bgm_volume 추가
        . 렌더링 시 전체 영상 길이에 맞춰 BGM 루프/페이드 처리
        . UI: 사이드바에 BGM 업로드 + 볼륨 슬라이더
        . 완료: Editor 사이드바에 BGM 업로드/삭제/볼륨 UI 추가, API client 연결
  - [C] 1-6. [config.py + models.py] 화면 비율 선택 (16:9 / 9:16 / 1:1)
        . 현황: CANVAS_SIZE가 (1280, 720)으로 하드코딩
        . 개선: Project별 aspect_ratio 설정, 렌더링 시 동적 해상도 적용
        . 활용: 유튜브 Shorts(9:16), 인스타 릴스(1:1) 등
        . 완료: Editor 사이드바에 화면비율 선택 UI 추가, renderer.py CANVAS_SIZE→canvas_size 파라미터화

──────────────────────────────────────────────────────────────────────────────
2. 🗣️ 오디오 & TTS 최적화 (Audio & TTS Processing)
──────────────────────────────────────────────────────────────────────────────
  - [C] 2-1. [tts_manager.py] TTS 서버 헬스체크 실패 시 edge-tts 자동 폴백 명시화
        . 현황: renderer.py에서 tts_engine.generate() 실패 → edge-tts asyncio.run()으로 분산 처리
        . 개선: TTSEngine 내부에서 폴백 처리를 캡슐화(단일 책임), renderer.py 코드 단순화
        . 완료: generate_with_fallback() 메서드 추가, renderer.py에서 분산 폴백 로직 제거
  - [C] 2-2. [tts_manager.py] TTS 서버가 미응답일 때 /load 재시도 로직 강화
        . 현황: load_model() 실패 시 그냥 False 반환, 렌더러는 별도 처리 없음
        . 개선: worker.py 또는 renderer에서 load 실패 감지 후 렌더 중단 or edge-tts 전용 모드로 전환
        . 완료: worker.py에서 load_model() 최대 3회 재시도, 실패 시 edge-tts 폴백 모드로 렌더링 진행

──────────────────────────────────────────────────────────────────────────────
3. 📝 콘텐츠 생성 및 분석 (Content Generation & Analysis)
──────────────────────────────────────────────────────────────────────────────
  - [C] 3-1. [slides.py] 콜라주 기능 레이아웃 고도화
        . 현황: 이미지를 단순 가로 나열(x_offset 누적)
        . 개선: 2x2, 3x1 등 그리드 레이아웃 옵션 추가, 각 셀 크기 균일화
        . 완료: auto/horizontal/2x2/3x1/1x3 레이아웃 옵션, 균일 셀 크기(640x480), UI 드롭다운 추가
  - [C] 3-2. [slides.py] 대용량 동영상 업로드 시 스트리밍 업로드 또는 청크 분할 처리
        . 현황: await f.read()로 메모리에 전체 로드 후 저장 → 수백MB 영상 시 메모리 과다
        . 개선: UploadFile을 청크 단위(64KB)로 파일에 직접 스트리밍
        . 완료: 64KB 청크 스트리밍 저장으로 변경

──────────────────────────────────────────────────────────────────────────────
4. 🔑 외부 API 연동 및 인증 (External APIs & Auth)
──────────────────────────────────────────────────────────────────────────────
  - [C] 4-1. [youtube_manager.py + API + Frontend] YouTube 토큰 자동 갱신 및 인앱 재인증 흐름 구축
        . 현황: token.json 없으면 None 반환, 만료 감지 없음, get_token.py를 별도 CLI로 수동 실행
        . 목표: 업로드 버튼 클릭 시 앱 화면 안에서 인증까지 자동 완료
        . [C] 4-1-1. [케이스 1] Access Token 만료 — 자동 처리 (사용자 개입 불필요)
              > _get_credentials()에서 creds.expired 감지 → Request()로 자동 갱신 → token.json 저장
        . [C] 4-1-2. [케이스 2] Refresh Token 만료/취소 — 인앱 팝업 재인증
              > /api/v1/youtube/auth-url 엔드포인트 추가, google_auth_oauthlib.flow.Flow로 OAuth URL 생성
        . [C] 4-1-3. [Backend] /api/v1/youtube/auth-callback 엔드포인트 추가
              > code → token 교환 후 token.json 저장, 팝업에서 postMessage로 완료 알림
        . [C] 4-1-4. [Frontend] YouTubeModal에 인증 상태 배너 + 401 감지 + 팝업 인증 흐름 구현
              > auth-status 체크, window.open() 팝업, postMessage 수신 → 상태 갱신

  - [C] 4-2. [신규] Google Photos Picker API 연동 파이프라인 구축
        . 참고: Library API의 photoslibrary.readonly 스코프가 2025.04.01 폐지됨
        .       → Photos Picker API (photospicker.mediaitems.readonly)로 전환
        . [C] 4-2-1. API 연동 및 인증
              > 스코프: photospicker.mediaitems.readonly (비제한 스코프)
              > YouTube 스코프와 통합, 인앱 OAuth 재인증 흐름 공유
              > /api/v1/photos/auth-url, auth-callback 엔드포인트
        . [C] 4-2-2. Picker 세션 기반 미디어 선택 흐름
              > POST /api/v1/photos/session → pickerUri 반환
              > GET /api/v1/photos/session/{id} → 폴링 (mediaItemsSet 확인)
              > 선택 완료 시 GET /v1/mediaItems → baseUrl 획득
        . [C] 4-2-3. [UI/UX] Picker 팝업 연동 인터페이스
              > GooglePhotosModal: 세션 생성 → pickerUri 팝업 → 폴링 → 자동 가져오기
              > 사이드바 "Google Photos에서 가져오기" 버튼
              > 팝업 닫힘 감지 + 선택 취소 처리
              > [수정] OAuth 인증: 수동 코드 붙여넣기 방식으로 전환 (Tailscale IP 환경 대응)
              > [수정] GoogleAuthSection → Dashboard 사이드바로 이동 (프로젝트 목록 페이지에서 인증)
        . [C] 4-2-4. 선택된 미디어 로컬 다운로드
              > POST /api/v1/photos/import/{project_id} (session_id 기반)
              > 사진: baseUrl+=d, 동영상: baseUrl+=dv (Authorization 헤더 포함)
              > 65KB 청크 스트리밍 저장 → assets 디렉터리, 세션 자동 삭제
        . [C] 4-2-5. docu_motion 렌더링 파이프라인 연계
              > mimeType 기반 image/video 자동 판별 → Slide DB 생성
              > image_filename / video_filename 자동 매핑

──────────────────────────────────────────────────────────────────────────────
5. ⚙️ 컨테이너 / 인프라 관리 (Infra & DevOps)
──────────────────────────────────────────────────────────────────────────────
  - [C] 5-1. [docker-compose.yml] TTS 서버 컨테이너 재시작 정책 강화
        . 현황: TTS 서버 다운 시 렌더링 전체 실패 → edge-tts 폴백으로 버팀
        . 개선: restart: always + healthcheck로 TTS 서버 자동 복구 보장
        . 완료: docu-app 컨테이너에 healthcheck 추가 (TTS 서버는 외부 관리, 2-1 폴백 캡슐화로 대응)

──────────────────────────────────────────────────────────────────────────────
6. 🎨 에디터 UI/UX 고도화 (Editor UI Overhaul)
──────────────────────────────────────────────────────────────────────────────
  - [C] 6-1. [Editor.tsx] 드래그&드롭 슬라이드 순서 변경
        . 현황: ⬆️⬇️ 버튼으로만 순서 이동 가능 (moveSlide → 배열 swap)
        . 개선: @dnd-kit/core 도입 → 슬라이드 카드를 드래그하여 직관적으로 순서 변경
        . 완료: HTML5 Drag API로 구현 (추가 의존성 없음), 썸네일 패널+카드 모두 드래그 가능
  - [C] 6-2. [Editor.tsx] 3분할 레이아웃 전환
        . 현황: 사이드바 260px + 메인 1열 (gridTemplateColumns: '260px 1fr')
        . 개선: 좌측 썸네일 패널 | 중앙 편집 영역 | 우측 미리보기 (상용 NLE 스타일)
        . 완료: 260px 사이드바 + 120px 썸네일 패널 + 메인 편집 영역 3분할
  - [C] 6-3. [Editor.tsx] 동영상 Trim 비주얼 슬라이더 (range slider)
        . 현황: trim_start / trim_end 숫자 input 직접 입력 (type="number" step=0.01)
        . 개선: 듀얼 핸들 range slider + 현재 구간 하이라이트 바
        . 완료: 듀얼 핸들 range slider 구현, 선택 구간 하이라이트 바, video duration 자동 감지
  - [C] 6-4. [Editor.tsx / SlideCard] 썸네일 그리드 패널
        . 현황: 슬라이드 카드가 세로 리스트로 나열, 전체 구조 한눈에 파악 어려움
        . 개선: 좌측 패널에 작은 썸네일 그리드 → 클릭 시 해당 슬라이드 편집 영역으로 스크롤
        . 완료: 3분할 레이아웃 좌측 120px 패널에 썸네일 그리드 구현, 클릭 시 해당 카드로 스크롤
  - [C] 6-5. [Editor.tsx] 자막 타임라인 트랙 뷰 (중기)
        . 현황: 자막을 테이블(table)로 편집 — 시작/종료 시간을 mm:ss 입력
        . 개선: 가로 타임라인 바 위에 자막 블록을 드래그로 배치/리사이즈
        . 완료: 자막 테이블 하단에 가로 타임라인 바 추가, 자막 블록을 비례 너비로 표시
  - [C] 6-6. [index.css + 전체] 다크모드 / 테마 시스템 정비
        . 현황: CSS 변수(var(--bg-card) 등)는 이미 있으나 테마 전환 UI 없음
        . 개선: 라이트/다크 모드 토글 + 색상 팔레트 정비
        . 완료: [data-theme="light"] 변수 추가, App.tsx에 ThemeToggle 컴포넌트, localStorage 저장
  - [C] 6-7. [Editor.tsx + renderer.py] 슬라이드 90도 회전 기능 추가
        . 개별 슬라이드(이미지/영상)를 90도씩 회전 가능하도록 구현
        . **UI: 회전 버튼 클릭 시 에디터 상에서 즉시 회전된 결과(Preview) 확인 가능하도록 처리**
        . DB: Slide 모델에 rotation 컬럼 추가 (0, 90, 180, 270)
        . 완료: ↻ 회전 버튼 추가, CSS transform으로 즉시 미리보기, PIL/MoviePy 회전 렌더링
  - [C] 6-8. [Editor.tsx + renderer.py] 전역 TTS 볼륨 컨트롤 및 상속 구조
        . 프로젝트 전체 TTS 볼륨(Master) 설정 기능 추가
        . 개별 슬라이드 TTS 볼륨은 Master 대비 곱셈으로 적용 (최종 볼륨 = 개별 × Master)
        . UI: 프로젝트 설정 영역에 'Master TTS Volume' 슬라이더 추가
        . 완료: Project.tts_master_volume 컬럼, 사이드바 슬라이더, renderer에서 곱셈 적용

## 2026-03-20 (완료)

──────────────────────────────────────────────────────────────────────────────
6. 🎨 에디터 UI/UX 고도화 (Editor UI Overhaul) — 계속
──────────────────────────────────────────────────────────────────────────────
  - [C] 6-10. [Editor.tsx] 전역 설정(Master) 전용 관리 메뉴 구축
        . Master TTS 볼륨, 장면 전환 전체 적용, TTS 자동 생성 일괄 On/Off
        . 추천 기능: 전역 자막 스타일(폰트/색상), 기본 슬라이드 시간, 워터마크 설정 등
        . 완료: GlobalSettingsModal 모달 구현, 전환/자막스타일/워터마크/슬라이드시간 설정, 전환 일괄적용 & TTS 일괄 On/Off 도구 버튼 추가
        . Backend: Project 모델에 6개 컬럼 추가 (default_transition, default_slide_duration, subtitle_font_size, subtitle_font_color, watermark_text, watermark_opacity)
        . renderer.py: 자막 폰트 크기/색상 파라미터화, 워터마크 TextClip 오버레이 추가, 기본 슬라이드 시간 파라미터화
  - [C] 6-11. [Editor.tsx] 미디어 업로드 및 버튼 UI 현대화 (디자인 개편)
        . 텍스트 위주 버튼 -> 아이콘+카드 형태의 세련된 디자인으로 교체
        . 전체적인 UI/UX 가시성 및 심미성 강화 (모던 앱 스타일)
        . 완료: action-card, upload-zone, btn-action-primary, slide-ctrl-btn, action-grid 등 CSS 클래스 추가, 사이드바 전면 리디자인 (섹션 분리, 아이콘+라벨 카드, 드롭존 스타일)
  - [C] 6-12. [Editor.tsx] 자막 편집 UX 개선 (타임라인 싱크 및 직관적 입력)
        . 현재 시작/종료 시간 수동 입력 방식의 불편함 개선
        . 영상 싱크에 맞게 직관적으로 조절하거나 타임라인에서 제어하는 방식 검토
        . 완료: SubtitleTimeline 드래그 컴포넌트 구현(블록 이동/좌우 리사이즈), 초 단위 숫자+range slider 병행 입력, 타임라인 클릭 시 비디오 시크 연동
