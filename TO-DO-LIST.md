================================================================================
 docu_motion - TO-DO LIST (Active Pipeline)
================================================================================
범례: [ ] 미완료 | [/] 진행중 | [O] 완료 | [S] 보류
================================================================================

──────────────────────────────────────────────────────────────────────────────
1. 🎬 비디오 렌더링 파이프라인 (Video Rendering & Encoding)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 1-1. [renderer.py] 비디오 슬라이드 자막(subtitles) + TTS 복합 오디오 품질 개선
        . 현황: CompositeAudioClip으로 원본 오디오 + TTS 오디오를 믹싱하는 구조 작동 중
        . 개선: TTS 볼륨 독립 조절 + 원본 오디오 fade-out/in 처리
  - [ ] 1-2. [renderer.py] 이미지 슬라이드 Ken Burns(패닝·줌) 효과 적용
        . 현황: `ImageClip`을 정적으로 표시만 함 (set_position('center', 'top'))
        . 개선: 슬라이드별 이미지 확대·이동 효과로 영상미 향상
  - [ ] 1-3. [renderer.py] 렌더링 실패 시 temp_render 디렉터리 정리 방어 로직 보완
        . 현황: except 블록에서 shutil.rmtree로 전체 삭제하여 TTS wav 캐시도 날아감
        . 개선: 실패 시 wav 캐시만 보존하고 중간 결과물만 삭제
  - [ ] 1-4. [renderer.py + models.py] 장면 전환 효과 (Scene Transitions)
        . 현황: concatenate_videoclips로 hard cut만 지원
        . 개선 (Phase 1): crossfade, fade_to_black 지원
        . 개선 (Phase 2): slide_left, slide_right 추가
        . DB: Slide 모델에 transition 컬럼 추가 (default: 'none')
        . UI: SlideCard 하단에 전환 효과 드롭다운
  - [ ] 1-5. [renderer.py + models.py] BGM 배경음악 지원
        . 현황: 원본 오디오 + TTS만 지원, BGM 없음
        . 개선: Project 모델에 bgm_filename/bgm_volume 추가
        . 렌더링 시 전체 영상 길이에 맞춰 BGM 루프/페이드 처리
        . UI: 사이드바에 BGM 업로드 + 볼륨 슬라이더
  - [ ] 1-6. [config.py + models.py] 화면 비율 선택 (16:9 / 9:16 / 1:1)
        . 현황: CANVAS_SIZE가 (1280, 720)으로 하드코딩
        . 개선: Project별 aspect_ratio 설정, 렌더링 시 동적 해상도 적용
        . 활용: 유튜브 Shorts(9:16), 인스타 릴스(1:1) 등

──────────────────────────────────────────────────────────────────────────────
2. 🗣️ 오디오 & TTS 최적화 (Audio & TTS Processing)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 2-1. [tts_manager.py] TTS 서버 헬스체크 실패 시 edge-tts 자동 폴백 명시화
        . 현황: renderer.py에서 tts_engine.generate() 실패 → edge-tts asyncio.run()으로 분산 처리
        . 개선: TTSEngine 내부에서 폴백 처리를 캡슐화(단일 책임), renderer.py 코드 단순화
  - [ ] 2-2. [tts_manager.py] TTS 서버가 미응답일 때 /load 재시도 로직 강화
        . 현황: load_model() 실패 시 그냥 False 반환, 렌더러는 별도 처리 없음
        . 개선: worker.py 또는 renderer에서 load 실패 감지 후 렌더 중단 or edge-tts 전용 모드로 전환

──────────────────────────────────────────────────────────────────────────────
3. 📝 콘텐츠 생성 및 분석 (Content Generation & Analysis)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 3-1. [slides.py] 콜라주 기능 레이아웃 고도화
        . 현황: 이미지를 단순 가로 나열(x_offset 누적)
        . 개선: 2x2, 3x1 등 그리드 레이아웃 옵션 추가, 각 셀 크기 균일화
  - [ ] 3-2. [slides.py] 대용량 동영상 업로드 시 스트리밍 업로드 또는 청크 분할 처리
        . 현황: await f.read()로 메모리에 전체 로드 후 저장 → 수백MB 영상 시 메모리 과다
        . 개선: UploadFile을 청크 단위(64KB)로 파일에 직접 스트리밍

──────────────────────────────────────────────────────────────────────────────
4. 🔑 외부 API 연동 및 인증 (External APIs & Auth)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 4-1. [youtube_manager.py + API + Frontend] YouTube 토큰 자동 갱신 및 인앱 재인증 흐름 구축
        . 현황: token.json 없으면 None 반환, 만료 감지 없음, get_token.py를 별도 CLI로 수동 실행
        . 목표: 업로드 버튼 클릭 시 앱 화면 안에서 인증까지 자동 완료
        . [ ] 4-1-1. [케이스 1] Access Token 만료 — 자동 처리 (사용자 개입 불필요)
              > `creds.expired and creds.refresh_token` 조건 감지
              > `google.auth.transport.requests.Request()`로 자동 갱신 (코드 1~2줄)
              > 갱신된 token.json 덮어쓰기 저장
        . [ ] 4-1-2. [케이스 2] Refresh Token 만료/취소 — 인앱 팝업 재인증
              > [Backend] `/api/v1/youtube/auth-url` 엔드포인트 신규 추가
              > google-auth-oauthlib으로 OAuth 인증 URL 생성 후 반환
        . [ ] 4-1-3. [Backend] `/api/v1/youtube/auth-callback` 엔드포인트 신규 추가
              > Google에서 리다이렉트된 code를 받아 token.json 갱신 후 완료 응답
        . [ ] 4-1-4. [Frontend] 업로드 요청 시 401/토큰 만료 응답 감지
              > window.open()으로 인증 URL을 팝업 창으로 열기
              > 콜백 완료 후 팝업 닫힘 감지 → 업로드 자동 재시도

  - [ ] 4-2. [신규] Google Photos 연동 파이프라인 구축 (미디어 소스 접근)
        . [ ] 4-2-1. API 연동 및 인증 (GCP에서 Photos Library API 활성화,
              > 기존 token.json SCOPES에 `photoslibrary.readonly` 추가 후 재발급)
        . [ ] 4-2-2. 미디어 목록 조회 구현
              > `mediaItems.list` / 앨범별 `mediaItems.search` 엔드포인트
              > Pagination (`nextPageToken`) 처리
              > 응답 데이터: baseUrl, mimeType, mediaMetadata(width/height, video.status)
        . [ ] 4-2-3. [UI/UX] 미디어 선택 인터페이스 개발
              > 썸네일 갤러리(baseUrl을 <img src>에 직접 사용 가능, 약 60분 유효)
              > 사진/동영상 체크박스 다중 선택 → "가져오기" 버튼
              > 기존 Editor.tsx의 업로드 영역에 "Google Photos에서 가져오기" 탭 추가 검토
        . [ ] 4-2-4. 선택된 미디어 로컬 다운로드
              > 사진: baseUrl + "=d" 파라미터로 원본 다운로드
              > 동영상: baseUrl + "=dv" 파라미터, video.status == "READY" 확인 필수
              > 다운로드 완료 후 기존 slides.py _assets_dir()에 저장 → Slide DB 생성
        . [ ] 4-2-5. docu_motion 렌더링 파이프라인 연계
              > 다운로드된 파일경로를 image_filename / video_filename에 매핑
              > slide_type(image/video) 자동 판별 후 DB 저장 (slides.py 업로드 로직과 통합)

──────────────────────────────────────────────────────────────────────────────
5. ⚙️ 컨테이너 / 인프라 관리 (Infra & DevOps)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 5-1. [docker-compose.yml] TTS 서버 컨테이너 재시작 정책 강화
        . 현황: TTS 서버 다운 시 렌더링 전체 실패 → edge-tts 폴백으로 버팀
        . 개선: restart: always + healthcheck로 TTS 서버 자동 복구 보장
  - [ ] 5-2. GPU 메모리 사용량 모니터링 및 OOM 발생 시 자동 메모리 정리 스크립트 추가
        . 현황: TTS 모델 UNLOAD는 on_tts_done 콜백으로 수동 트리거
        . 개선: VRAM 사용량 임계치 초과 시 자동 unload → NVENC 인코딩 안정성 확보

──────────────────────────────────────────────────────────────────────────────
6. 🎨 에디터 UI/UX 고도화 (Editor UI Overhaul)
──────────────────────────────────────────────────────────────────────────────
  - [ ] 6-1. [Editor.tsx] 드래그&드롭 슬라이드 순서 변경
        . 현황: ⬆️⬇️ 버튼으로만 순서 이동 가능 (moveSlide → 배열 swap)
        . 개선: @dnd-kit/core 도입 → 슬라이드 카드를 드래그하여 직관적으로 순서 변경
  - [ ] 6-2. [Editor.tsx] 3분할 레이아웃 전환
        . 현황: 사이드바 260px + 메인 1열 (gridTemplateColumns: '260px 1fr')
        . 개선: 좌측 썸네일 패널 | 중앙 편집 영역 | 우측 미리보기 (상용 NLE 스타일)
  - [ ] 6-3. [Editor.tsx] 동영상 Trim 비주얼 슬라이더 (range slider)
        . 현황: trim_start / trim_end 숫자 input 직접 입력 (type="number" step=0.01)
        . 개선: 듀얼 핸들 range slider + 현재 구간 하이라이트 바
  - [ ] 6-4. [Editor.tsx / SlideCard] 썸네일 그리드 패널
        . 현황: 슬라이드 카드가 세로 리스트로 나열, 전체 구조 한눈에 파악 어려움
        . 개선: 좌측 패널에 작은 썸네일 그리드 → 클릭 시 해당 슬라이드 편집 영역으로 스크롤
  - [ ] 6-5. [Editor.tsx] 자막 타임라인 트랙 뷰 (중기)
        . 현황: 자막을 테이블(table)로 편집 — 시작/종료 시간을 mm:ss 입력
        . 개선: 가로 타임라인 바 위에 자막 블록을 드래그로 배치/리사이즈
  - [ ] 6-6. [index.css + 전체] 다크모드 / 테마 시스템 정비
        . 현황: CSS 변수(var(--bg-card) 등)는 이미 있으나 테마 전환 UI 없음
        . 개선: 라이트/다크 모드 토글 + 색상 팔레트 정비

================================================================================
📝 참고: 완료된 과거 이력은 TO-DO-ARCHIVE.md 파일에서 확인하세요.
================================================================================
