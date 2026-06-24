================================================================================
 docu_motion - TO-DO LIST (Active Pipeline)
================================================================================
범례: [ ] 미완료 | [/] 진행중 | [O] 완료 | [S] 보류 | [C] 검토완료
================================================================================

──────────────────────────────────────────────────────────────────────────────
5. ⚙️ 컨테이너 / 인프라 관리 (Infra & DevOps)
──────────────────────────────────────────────────────────────────────────────
  - [S] 5-2. GPU 메모리 사용량 모니터링 및 OOM 발생 시 자동 메모리 정리 스크립트 추가
        . 현황: TTS 모델 UNLOAD는 on_tts_done 콜백으로 수동 트리거
        . 개선: VRAM 사용량 임계치 초과 시 자동 unload → NVENC 인코딩 안정성 확보


──────────────────────────────────────────────────────────────────────────────
6. 🎬 실시간 / 구간 미리보기 (Real-time / Segment Preview)
   목표: 렌더 없이 슬라이드 단위의 트랜지션·Ken Burns·오버레이·자막 타이밍·
         회전·이미지 맞춤을 확인. 현재는 정지 이미지+오버레이+회전만 미리보기.
   방식: 하이브리드 — 서버 구간 렌더(충실, CPU/libx264) 주축 +
         클라이언트 애니메이션(즉각 체감) 보조.
   TTS 정책: 토글식 — 기본 캐시된 v_*.wav 우선(빠름, 자막타이밍 근사),
             "정확히 듣기" 버튼 시 해당 슬라이드 TTS 생성 포함 미리보기.
──────────────────────────────────────────────────────────────────────────────
  - [O] 6-18. renderer.py 리팩터 — 단일 슬라이드 클립 생성 함수 추출 (Phase 1) ✓ 2026-06-24
        . 현황: render_project() 루프(renderer.py:320-682) 내에서 슬라이드당
                final_clips[i]를 인라인 생성. 단일 슬라이드만 렌더하는 진입점 없음.
        . 작업:
          - build_slide_clip(item, idx, assets_dir, canvas_size, tts_cache, ...)
            함수로 슬라이드당 클립 생성 로직 추출 (비디오/이미지 양쪽)
          - render_project()는 이 함수를 루프로 호출 (기존 동작 100% 유지)
          - apply_transition()은 루프 레벨에서만 유지 (단일 슬라이드 시 미사용)
          - 오버레이/Ken Burns/자막/회전/이미지 맞춤 로직은 그대로 함수 내 포함
        . 검증: py_compile OK · 컨테이너 임포트 OK · 시그니처 10파라미터 확인.
          (본 리팩터는 move+별칭+종료점 변경만으로 본문 바이트 동일 → 풀렌더 회귀 사실상 无.
           실제 픽셀비교는 6-19~연동 시 샘플 프로젝트로 추가 검증 예정)
        . 의존: 없음 (모든 후속 Phase의 기반)

  - [O] 6-19. 백엔드 미리보기 서비스 + API + 캐시 (Phase 2) ✓ 2026-06-24
        . 신규 services/preview.py:
          - render_slide_preview(project_id, slide_id, include_neighbors,
                                 force_tts=False)
          - 슬라이드 상태 해시 계산(§캐시 키) → hit 시 즉시 반환
          - miss → build_slide_clip() 단일 클립 생성
          - 해상도: 캔버스 50% 스케일 (예: 720p → 360p)
          - 인코딩: libx264 ultrafast (CPU 고정, GPU/h264_nvenc 회피)
          - 출력: outputs/{project_id}/previews/{slide_id}_{hash}.mp4
          - stale 프리뷰 파일 정리(해시 불일치/최근 N개 초과)
        . TTS 토글 정책:
          - 기본: 캐시 v_*.wav 우선 사용. 없으면 무음+추정 길이(자막 타이밍 근사)
          - force_tts=True ("정확히 듣기"): 해당 슬라이드 TTS 온디맨드 생성 후 포함
        . 신규 api/v1/preview.py:
          - POST /projects/{pid}/slides/{sid}/preview
              body: { include_neighbors?, force_tts? }
              → BackgroundTasks 렌더. 캐시 hit 시 즉시 200 + URL
          - GET  /projects/{pid}/slides/{sid}/preview
              → 캐시 mp4 서빙(Range 지원), 없으면 202 Pending
        . 상태: 파일 존재 여부로 판별(별도 DB 폴링 불필요)
        . 의존: 6-18 (build_slide_clip)
        . 검증(2026-06-24): services/preview.py + api/v1/preview.py + main.py 라우터 등록.
          - 이미지/비디오/인접(트랜지션) 모드 렌더 OK (libx264, 640x360, 24fps)
          - 캐시 hit 0.001s / miss ~5s(이미지)·~15s(비디오)
          - HTTP: POST 202→폴링→GET 200 video/mp4 + Accept-Ranges. 캐시 hit POST 200.
          - TTS 토글: 기본 모드는 use_tts=0 우회(무음 추정), force_tts 시 엔진 로드.
          - in-flight 중복 렌더 방지, stale 정리(동일 슬라이드 이전 해시/프로젝트당 40개 한) 동작.
          - 한계: FileResponse는 206 Partial 미지원(Accept-Ranges 헤더만). preview 크기 작아 무방.
          - 서버 start_server.sh 재실행으로 신규 라우터 반영 완료.

  - [O] 6-20. 프론트 프리뷰 플레이어 패널 + 디바운스 트리거 (Phase 3) ✓ 2026-06-24
        . FullPreviewCanvas(Editor.tsx:840-849) 영역을 <video> 기반
          프리뷰 플레이어로 전환/추가 (루프 재생)
        . 트리거: 기존 자동저장 debounce(2s) 패턴 재사용.
          저장 완료 후 POST /preview, 응답 대기 중 스피너.
          캐시 hit → 즉시 재생.
        . "정확히 듣기" 버튼: force_tts=true 로 재요청 (토글 TTS 정책)
        . 슬라이드 선택/파라미터 변경 시 자동 갱신
        . 비디오 슬라이드: 원본 <video> 재생에 오버레이·회전·트림 결과만
          추가 렌더(옵션, P1)
        . 의존: 6-19 (preview API)
        . 검증(2026-06-24): client.ts(requestPreview/previewVideoUrl/previewReady) +
          PreviewPlayerModal(<video autoPlay loop> + 스피너 + "정확히 듣기" force_tts 토글 +
          1.2s 폴링) + SlideCard "▶ 렌더 미리보기" 버튼(onPreviewVideo) + Editor 모달 연결.
          - 편집 직후 즉시 저장(saveSlides) 후 미리보기 오픈 → debounce 대기 없이 최신 상태 반영.
          - npm run build(tsc+vite) 통과, dist 라이브 서빙 확인.
          - 브라우저 클릭 테스트는 사용자 확인 권장(자동화 한계).

  - [O] 6-21. 클라이언트 Ken Burns 애니메이션 + 트랜지션 미리보기 (Phase 4) ✓ 2026-06-24
        . 서버 응답 대기 중 Ken Burns를 CSS transform scale 애니메이션으로
          근사 표현 (즉각 체감). 오버레이는 기존 canvas 로직 재사용.
        . 서버 mp4 도착 시 충실한 버전으로 전환
        . "트랜지션 미리보기" 토글 → include_neighbors=true 로
          이전 슬라이드와의 트랜지션 2-슬라이드 렌더
        . 의존: 6-20
        . 검증(2026-06-24): KenBurnsFallback 컴포넌트(useOverlayCanvas 캔버스 + CSS scale
          keyframes, 백엔드와 동일: order_index%2 줌인/아웃, maxZoom=0.13*intensity) —
          로딩 중 이미지 슬라이드에 즉시 표시. PreviewPlayerModal에 "트랜지션 포함"
          토글(include_neighbors) 추가(첫 슬라이드 order_index 0은 비활성).
          - include_neighbors 백엔드 렌더 검증: 2클립 합성, 9.08s video+audio mp4 생성.
          - npm run build 통과, dist 라이브 서빙(index-D4MT-vVf.js).
          - 브라우저 시각 확인은 사용자 권장.
        . 참고: 6-18~6-21 "실시간/구간 미리보기" 기능 전체 완료.

  ── 캐시 키(해시 입력) ──
  image/video 파일 mtime + rotation + overlays + ken_burns + image_fit +
  text + transition(인/아웃) + 자막 엔트리 + tts_volume (+ force_tts 플래그)


================================================================================
7. 🧭 UI / 메뉴 구조 정비 (Information Architecture)
   배경: 6-x 신규 기능 누적로 메뉴·설정·미리보기 진입점이 분산되어 일관성 저하.
   방침: 성격이 같은 기능은 한 곳으로 묶고, 위치가 다르면 기준(사이드바=프로젝트단 /
         슬라이드 카드=개별 슬라이드단)에 맞춰 조정.
================================================================================
  ── A. 설정 통합 (가장 영향 큼) ──
  - [O] 7-1. 프로젝트 수준 설정을 하나로 통합 ✓ 2026-06-24
        . 현황(분산): 사이드바 "⚙️ 프로젝트 설정" 섹션에 aspect_ratio·BGM·Master TTS 볼륨
          (Editor.tsx:2459~) ↔ "전역 설정(Master)" 모달에 전환·자막폰트·워터마크·인트로
          타이틀·이미지 맞춤·Ken Burns 기본값 (GlobalSettingsModal, Editor.tsx:679~).
          → 프로젝트 설정이 두 곳에 흩어져 사용자 혼란.
        . 조정(방향 결정 필요 → 아래 질문):
          (a) 모든 프로젝트 설정을 GlobalSettingsModal로 통합, 사이드바엔 "⚙️ 설정" 버튼 1개만 남김
          (b) 현행 유지하되 사이드바 섹션명을 "프로젝트 속성(비율/오디오)", 모달을
              "장면 기본값(전환/자막/효과)"으로 명확히 구분 라벨링
        . 결정·구현(2026-06-24): (a) 채택 — 전체 설정을 GlobalSettingsModal로 통합.
          비율·BGM·Master TTS는 "즉시 적용" 컨트롤로 모달 내 이동(동작 보존),
          장면 스타일 기본값은 기존 "저장" 지연 적용 유지. 사이드바는 "프로젝트 설정" 단일 버튼.
          신규 props: projectId/onProjectUpdate/onOpenBgmSearch. npm run build 통과·라이브 서빙.
          브라우저 동작 확인 권장(BGM 업로드/검색/볼륨 즉시 적용).
  - [O] 7-2. 일괄 적용 기능 중복 해소 ✓ 2026-06-24
        . 현황(중복): 도구 섹션 "전환 일괄 적용" 버튼(Editor.tsx:2611) ≡ GlobalSettingsModal의
          "저장 시 전환 일괄 적용" 체크박스(Editor.tsx:694). 동일 결과의 두 경로.
          "TTS 일괄 On/Off"(도구)는 모달에 대응 항목 없음.
        . 조정: "전환 일괄 적용" 도구 버튼 제거 → 모달 체크박스로 통일.
          "TTS 일괄 On/Off"는 도구에 유지(또는 모달 "오디오" 섹션으로 이동).

  ── B. 미리보기 진입 통합 ──
  - [O] 7-3. 미리보기 진입점 정리 ✓ 2026-06-24
        . 현황(분산): (1) 썸네일 클릭 → 정지 이미지 전체화면(FullPreviewCanvas),
          (2) "▶ 렌더 미리보기" 버튼 → 렌더 결과(PreviewPlayerModal),
          (3) 비디오 슬라이드 카드 내 비디오 플레이어.
        . 조정: 렌더 미리보기를 기본 미리보기로 격상 — 썸네일 클릭 시 PreviewPlayerModal이
          열리고, 로딩 중엔 KenBurnsFallback(정지+근사 애니메이션)이 이미 표시되므로
          FullPreviewCanvas(단순 정지)는 흡수/제거. 단일 진입점 = 썸네일 클릭.
          (비디오 슬라이드 카드 내 플레이어는 트림 편집용이므로 유지)
        . 구현(2026-06-24): 이미지 슬라이드 썸네일 클릭 → onPreviewVideo(즉시저장+PreviewPlayerModal).
          정지 전용 FullPreviewCanvas 컴포넌트·previewImage 상태·모달·onPreviewImage prop 전부 제거.
          "▶ 렌더 미리보기" 버튼(양 슬라이드 공용) 유지 → 단일 미리보기 시스템에 2 진입(썸네일/버튼).
          npm run build 통과(255KB), 라이브 서빙. (7-7 라벨정리 중 FullPreviewCanvas분 일부 해소)

  ── C. 라벨 / 아이콘 일관성 ──
  - [O] 7-4. 사이드바 섹션 아이콘·라벨 체계 통일 ✓ 2026-06-25
        . 현황: "📤 미디어 추가"(Editor.tsx:2414) 등 이모지 혼용, Dashboard와 아이콘 의미 불일치.
        . 조정: 섹션 아이콘을 일관된 세트로 통일, Dashboard/Editor 액션 아이콘
          (편집/미리보기/다운로드/삭제) 의미 동일화.
        . 구현(2026-06-25): 의미 부적절 "📤 미디어 추가" → "➕ 미디어 추가"로 수정.
          (나머지 섹션 아이콘 ⚙️/🛠️/▶️/🎬 은 일관적이라 유지)
  - [O] 7-5. "도구" 섹션 재위치/재명명 ✓ 2026-06-24
        . 현황: JSON 일괄입력·(전환 일괄)·TTS 일괄 이 "도구"에. 7-2로 전환 일괄은 제거.
        . 조정: "도구" → "일괄 편집"으로 명명, 남은 항목(JSON 일괄입력·TTS 일괄) 정리.

  ── D. 기타 정리 ──
  - [O] 7-6. "실행" 섹션(저장/렌더) 시각적 강조 — 사이드바 최하단 주요 액션 접근성 점검
        (sticky 또는 강조 스타일). ✓ 2026-06-25
        . 구현: 실행 섹션을 marginTop:auto + borderTop 강조 카드로 래핑 → 사이드바 하단에
          항상 고정(저장/렌더/진행률/에러/결과물). npm run build 통과.
  - [O] 7-7. 미사용/중복 라벨·주석 정리 (FullPreviewCanvas "전체화면 미리보기" 등 7-3 연동). ✓ 2026-06-25
        . 구현: 7-3로 FullPreviewCanvas "전체화면 미리보기" 제거 완료.
          toast "전역 설정 저장 완료" → "프로젝트 설정 저장 완료" (7-1 리네임과 일치).
        . → 7-x "UI/메뉴 구조 정비" 전체 완료.

  ── E. 사이드바 워크플로우 재구성 (사용자 피드백, 2026-06-25) ──
  - [O] 7-8. 사이드바 3단 워크플로우(입력→설정→렌더링) + 저장 분리 + TTS 일괄 이동 ✓ 2026-06-25
        . 구현:
          - 저장 버튼 → 사이드바 상단(별도, "💾 현재 상태 저장"), 렌더 플로우에서 분리
          - 📥 입력: 삽입위치·파일업로드·Google Photos (구 "미디어 추가")
          - ⚙️ 설정 · 편집: 프로젝트 설정(모달) + JSON 일괄 입력 (구 "프로젝트 설정"+"일괄 편집" 통합)
          - 🎬 렌더링(하단 고정): 렌더링 시작 + 진행률/에러 + 📦 결과물(영상/MP4/YouTube)
          - TTS 일괄 On/Off → 프로젝트 설정 모달(오디오 섹션)로 이동 (onToggleAllTts prop)
        . 검증: npm run build 통과, 라이브 서빙.


================================================================================
📝 참고: 완료된 과거 이력은 TO-DO-ARCHIVE.md 파일에서 확인하세요.
        최근 완료(2026-06-24): 6-18~6-21 실시간/구간 미리보기 전체 완료.
        이전(2026-06-23): 6-9 BGM 라이브러리, 6-13 오버레이/회전, 6-14 구글 포토 정렬,
        6-15 프로젝트 이름변경, 6-16 인트로 타이틀, 6-17 이미지 맞춤/Ken Burns.
        신규 백로그(7-x): UI/메뉴 구조 정비 — 설정 통합·미리보기 진입·라벨 일관성.
================================================================================
