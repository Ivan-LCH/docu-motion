// DocuMotion - API Client
const BASE = '/api/v1'

// API 오류 — detail 이 문자열 또는 {code, message} 객체일 수 있음
export class ApiError extends Error {
  code?: string
  status: number
  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function toApiError(res: Response, detail: unknown): ApiError {
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const d = detail as { code?: unknown; message?: unknown }
    const msg = typeof d.message === 'string' && d.message ? d.message : (res.statusText || 'API Error')
    return new ApiError(msg, res.status, typeof d.code === 'string' ? d.code : undefined)
  }
  if (typeof detail === 'string' && detail) return new ApiError(detail, res.status)
  // FastAPI 검증 오류(detail 배열) 등은 첫 항목 메시지 사용
  if (Array.isArray(detail) && detail.length && typeof detail[0]?.msg === 'string') {
    return new ApiError(detail[0].msg, res.status)
  }
  return new ApiError(res.statusText || 'API Error', res.status)
}

async function request<T>(method: string, path: string, body?: unknown, isForm = false): Promise<T> {
  const opts: RequestInit = { method }
  if (body) {
    if (isForm) {
      opts.body = body as FormData
    } else {
      opts.headers = { 'Content-Type': 'application/json' }
      opts.body = JSON.stringify(body)
    }
  }
  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw toApiError(res, err?.detail)
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json()
}

// ─── YouTube ────────────────────────────────
export type YouTubePrivacy = 'private' | 'unlisted' | 'public'
export type YouTubeAuthReason = 'no_token' | 'reauth_required' | 'scope_missing' | 'error'
export interface YouTubeAuthStatus {
  authenticated: boolean
  reason?: YouTubeAuthReason | string | null
  scopes_ok?: boolean
  channel?: string | null
}
export interface YouTubeMetaFields {
  title: string
  description: string
  tags: string[]
  privacy: YouTubePrivacy
  thumbnail: string          // 'none' | 'upload' | 'slide:<n>'
}
export interface YouTubeThumbCandidate { index: number; slide_id: string; url: string }
export interface YouTubeMeta extends YouTubeMetaFields {
  source: 'ai' | 'template' | 'edited' | 'saved'
  credit: string
  thumbnails: YouTubeThumbCandidate[]
  uploaded_thumbnail: string | null
  tags_limit: number
}
export type YouTubeUploadStatus = 'idle' | 'queued' | 'uploading' | 'processing' | 'done' | 'error'
export interface YouTubeUploadState {
  status: YouTubeUploadStatus
  progress: number
  url: string | null
  video_id: string | null
  privacy: YouTubePrivacy | null
  error: string | null
  warning: string | null
  started_at: string | null
  finished_at: string | null
}
export interface YouTubeUploadPayload {
  title?: string
  description?: string
  tags?: string[] | string
  privacy?: YouTubePrivacy
  thumbnail?: string
}

// ─── Types ──────────────────────────────────
export interface SavedLocation {
  id: string
  name: string      // 표시명: 집/회사/...
  query: string     // 주소 또는 장소명
  lat: number
  lng: number
}

export interface GeocodeResult {
  provider: string
  name: string
  display_name: string
  lat: number
  lng: number
  overseas: boolean
}

export interface Slide {
  id: string
  order_index: number
  image_filename: string
  label: string
  text: string
  // Video Slide
  slide_type: 'image' | 'video' | 'route' | 'place'
  video_filename: string
  volume: number
  subtitles: string  // JSON array string
  use_tts: number   // 1=TTS on, 0=subtitle only
  trim_start?: number
  trim_end?: number
  transition?: string  // TRANSITION_PRESETS 의 value (backend/services/presets.py)
  tts_volume?: number  // TTS 볼륨 (0.0 ~ 2.0)
  rotation?: number    // 0, 90, 180, 270
  overlays?: string    // JSON array of overlay objects
  image_fit?: 'cover' | 'fit'  // cover: 전체화면, fit: 이미지 위쪽+하단 자막 영역
  ken_burns?: number   // Ken Burns 강도 0~100 (0=정적, 100=최대 줌)
  photo_effect?: PhotoEffect  // none | sketch | cartoon | sketch_soft | watercolor | sketch_color
  effect_reveal?: number      // 0~1, 슬라이드 길이 중 원본→효과 크로스페이드 비율
  meta?: string        // 타입별 추가 데이터 JSON (route/place, hero_text/punch_text)
  text_style?: TextStyle
}

// 사진 효과 — backend/services/presets.py 의 PHOTO_EFFECTS 와 동기화 유지
export type PhotoEffect = 'none' | 'sketch' | 'cartoon' | 'sketch_soft' | 'watercolor' | 'sketch_color'
export const PHOTO_EFFECT_PRESETS: { value: PhotoEffect; label: string }[] = [
  { value: 'none', label: '없음 (원본)' },
  { value: 'sketch', label: '스케치 (연필)' },
  { value: 'sketch_soft', label: '부드러운 스케치' },
  { value: 'cartoon', label: '만화' },
  { value: 'watercolor', label: '부드러운 수채 애니풍' },
  { value: 'sketch_color', label: '파스텔 채색 스케치' },
]
export const PHOTO_EFFECT_SHORT: Record<PhotoEffect, string> = {
  none: '', sketch: '스케치', sketch_soft: '부드러운 스케치', cartoon: '만화', watercolor: '부드러운 수채 애니풍',
  sketch_color: '파스텔 채색 스케치',
}
export const DEFAULT_EFFECT_REVEAL = 0.45
// 스케치 계열 부드러움 (meta.sketch_softness 0~1). sketch 는 미설정 = 0(기존), sketch_soft·sketch_color 는 0.75
export const DEFAULT_SKETCH_SOFTNESS: Partial<Record<PhotoEffect, number>> = { sketch: 0, sketch_soft: 0.75, sketch_color: 0.75 }
export function getSketchSoftness(s: Slide, effect: PhotoEffect): number {
  const fallback = DEFAULT_SKETCH_SOFTNESS[effect] ?? 0
  try {
    const v = s.meta ? JSON.parse(s.meta)?.sketch_softness : undefined
    return typeof v === 'number' && isFinite(v) ? Math.max(0, Math.min(1, v)) : fallback
  } catch { return fallback }
}
export function withSketchSoftness(s: Slide, v: number | null): string {
  let m: Record<string, unknown> = {}
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) m = parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return JSON.stringify({ ...m, sketch_softness: v === null ? '' : v })
}
// 파스텔 채색 스케치 진하기 (meta.color_strength 0~1). 미설정 = 0.70 (backend DEFAULT_COLOR_STRENGTH)
export const DEFAULT_COLOR_STRENGTH = 0.7
export function getColorStrength(s: Slide): number {
  try {
    const v = s.meta ? JSON.parse(s.meta)?.color_strength : undefined
    return typeof v === 'number' && isFinite(v) ? Math.max(0, Math.min(1, v)) : DEFAULT_COLOR_STRENGTH
  } catch { return DEFAULT_COLOR_STRENGTH }
}
export function withColorStrength(s: Slide, v: number | null): string {
  let m: Record<string, unknown> = {}
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) m = parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return JSON.stringify({ ...m, color_strength: v === null ? '' : v })
}

// CinemaFlow 프리셋 — backend/services/presets.py 의 TRANSITIONS / TEXT_STYLES 와 동기화 유지
export const TRANSITION_PRESETS: { value: string; label: string; short: string }[] = [
  { value: 'none', label: '없음 (Hard Cut)', short: '없음' },
  { value: 'crossfade', label: '크로스페이드', short: '크로스페이드' },
  { value: 'fade_black', label: '페이드 투 블랙', short: '페이드 투 블랙' },
  { value: 'slide_left', label: '슬라이드 왼쪽', short: '슬라이드 ←' },
  { value: 'slide_right', label: '슬라이드 오른쪽', short: '슬라이드 →' },
  { value: 'whip_left', label: '휩 팬 왼쪽 (0.35s, 모션 블러)', short: '휩 ←' },
  { value: 'whip_right', label: '휩 팬 오른쪽 (0.35s, 모션 블러)', short: '휩 →' },
  { value: 'zoom_through', label: '줌 스루 (0.45s)', short: '줌 스루' },
  { value: 'light_leak', label: '라이트 릭 (0.4s)', short: '라이트 릭' },
  { value: 'flash', label: '화이트 플래시 (0.3s)', short: '플래시' },
]

export type TextStyle = 'caption' | 'hero' | 'punch' | 'kinetic' | 'none'
export const TEXT_STYLE_PRESETS: { value: TextStyle; label: string }[] = [
  { value: 'caption', label: '자막 (하단, 문장별)' },
  { value: 'hero', label: '히어로 (큰 제목)' },
  { value: 'punch', label: '펀치 (중앙 한 단어, 8자)' },
  { value: 'kinetic', label: '잡지식 타이포 (좌/우 하단, 차분한 화이트)' },
  { value: 'none', label: '텍스트 없음' },
]
export const PUNCH_MAX_CHARS = 8
export const KINETIC_MAX_CHARS = 20

export type StyleTextKey = 'hero_text' | 'punch_text' | 'kinetic_text'

// meta JSON 에서 hero_text / punch_text / kinetic_text 읽기 (파싱 실패 시 빈 문자열)
export function getStyleText(s: Slide, key: StyleTextKey): string {
  try {
    const m = s.meta ? JSON.parse(s.meta) : {}
    return typeof m?.[key] === 'string' ? m[key] : ''
  } catch { return '' }
}

// meta JSON 에 hero_text / punch_text / kinetic_text 병합한 새 meta 문자열 (빈 값도 키 유지 → 서버에서 삭제 처리)
export function withStyleText(s: Slide, key: StyleTextKey, value: string): string {
  let m: Record<string, unknown> = {}
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) m = parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return JSON.stringify({ ...m, [key]: value })
}

// 자동 편집 위치 카드 (meta.location_card) — 제목을 비우면 서버에서 카드 삭제
export interface LocationCard { title: string; sub?: string; source?: string }
export function getLocationCard(s: Slide): LocationCard | null {
  try {
    const c = s.meta ? JSON.parse(s.meta)?.location_card : null
    return c && typeof c === 'object' && typeof c.title === 'string' ? c : null
  } catch { return null }
}
export function withLocationCard(s: Slide, card: LocationCard): string {
  let m: Record<string, unknown> = {}
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) m = parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return JSON.stringify({ ...m, location_card: card })
}

// 2단계: 그림/만화 → 실사 전환 (meta.effect_to_real). '' = 기존 (실사 → 그림 크로스페이드)
export type ToRealStyle = '' | 'brush' | 'wipe' | 'dissolve'
export const TO_REAL_PRESETS: { value: ToRealStyle; label: string }[] = [
  { value: '', label: '실사 → 그림 (기존)' },
  { value: 'brush', label: '그림 → 실사 · 붓결' },
  { value: 'wipe', label: '그림 → 실사 · 와이프' },
  { value: 'dissolve', label: '그림 → 실사 · 디졸브' },
]
export function getToReal(s: Slide): ToRealStyle {
  try {
    const v = s.meta ? JSON.parse(s.meta)?.effect_to_real : ''
    return v === 'brush' || v === 'wipe' || v === 'dissolve' ? v : ''
  } catch { return '' }
}
export function withToReal(s: Slide, v: ToRealStyle): string {
  let m: Record<string, unknown> = {}
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) m = parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return JSON.stringify({ ...m, effect_to_real: v })
}

// Route/Place 슬라이드 메타 파서
export interface RouteMeta {
  type: 'route'
  origin: { name: string; display_name?: string }
  destination: { name: string; display_name?: string }
  profile: string
  distance_m: number
  duration_s: number
  frames: string[]
  duration: number
  n_frames?: number
}
export interface PlaceMeta {
  type: 'place'
  name: string
  address: string
  category: string
  opening_hours: string
}
export function parseSlideMeta(s: Slide): RouteMeta | PlaceMeta | null {
  try { return s.meta ? JSON.parse(s.meta) : null } catch { return null }
}

export interface Project {
  id: string
  name: string
  status: 'DRAFT' | 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'ERROR'
  stage: 'initialized' | 'uploaded' | 'scripted'
  progress: number
  message: string
  has_video: boolean
  slide_count: number
  created_at: string
  updated_at: string
  bgm_filename: string
  bgm_volume: number
  aspect_ratio: string
  tts_master_volume: number
  tts_voice: string
  default_transition: string
  default_slide_duration: number
  subtitle_font_size: number
  subtitle_font_color: string
  watermark_text: string
  watermark_opacity: number
  title_text: string
  resolution: string
  transition_duration: number
  style_preset: string
}

export interface ProjectDetail extends Project {
  slides: Slide[]
}

export interface RenderStatus {
  status: string
  progress: number
  message: string
}

// 원클릭 자동 영상 (11-1)
export interface AutoVideoOptions {
  narration_ratio: number  // 0~1: 나레이션(자막+TTS)을 달 슬라이드 비율
  tone: 'documentary' | 'vlog'
  style_preset: string
  auto_curate: boolean
}
export interface AutoVideoTask {
  status?: string
  phase?: string
  progress?: number
  message?: string
  narrated?: number
  silent?: number
  failed?: number
  deleted?: number
  remaining?: number
}
export interface AutoVideoStatus {
  task: AutoVideoTask
  render: { status?: string; progress?: number; message?: string }
}

// 자동 편집 1단계 (12): 음악 인트로 + 위치 카드 + 키네틱 타이포
export interface AutoEditOptions {
  render: boolean
  resolution?: '720p' | '1080p'
  intro_seconds: number
  music_intro: boolean
  location_cards: boolean
  kinetic: boolean
  narration: boolean
  style_preset: string
  stylize_ratio?: number          // 2단계: 그림→실사 사진 비율 (0 = 끔)
  stylize_effect?: StylizeEffect
}
// 2단계 그림 스타일. auto = 수채 애니풍 → 파스텔 채색 스케치 → 부드러운 스케치 → 만화 → 스케치 순환 (backend _STYLIZE_CYCLE)
export type StylizeEffect = 'auto' | 'sketch' | 'cartoon' | 'sketch_soft' | 'watercolor' | 'sketch_color'
export const STYLIZE_EFFECT_PRESETS: { value: StylizeEffect; label: string }[] = [
  { value: 'auto', label: '자동 순환' },
  { value: 'watercolor', label: '부드러운 수채 애니풍' },
  { value: 'sketch_color', label: '파스텔 채색 스케치' },
  { value: 'sketch_soft', label: '부드러운 스케치' },
  { value: 'cartoon', label: '만화' },
  { value: 'sketch', label: '스케치' },
]
export interface AutoEditSummary {
  intro_seconds: number
  cards: { order_index: number; title: string; sub: string; source: 'gps' | 'fallback' }[]
  kinetic: { order_index: number; text: string }[]
  stylized?: { order_index: number; effect: string; to_real: string }[]
}
export interface AutoEditTask {
  status?: string
  phase?: string
  progress?: number
  message?: string
  summary?: AutoEditSummary | null
}
export interface AutoEditStatus {
  task: AutoEditTask
  render: { status?: string; progress?: number; message?: string }
}

export interface PickerSession {
  id: string
  pickerUri: string
  mediaItemsSet: boolean
}

export interface PickerImportResult {
  ok: boolean
  imported: { id: string | null; filename: string | null; error?: string; type?: string }[]
  count: number
}

export type PhotosSortOrder = 'selected' | 'oldest' | 'newest' | 'api'

// EXIF 자동 구성 제안 (Photo-Vlog F3)
export interface RouteSuggestion {
  after_slide_id: string
  after_image_filename: string
  from: { lat: number; lng: number; name?: string }
  to: { lat: number; lng: number; name?: string }
  distance_km: number
}
export interface OrganizeSuggestions {
  needs_sort: boolean
  has_time: boolean
  has_gps: boolean
  routes: RouteSuggestion[]
}

// 스마트 사진 선별 (Photo-Vlog F1)
export interface CurationSuggestion {
  slide_id: string
  action: 'duplicate' | 'blurry' | 'dark'
  reason: string
  cluster_id: number
  is_best: boolean
  image_filename: string
  label: string
}
export interface CurationStatus {
  status: 'running' | 'done' | 'error'
  progress: number
  message?: string
  result?: { suggestions: CurationSuggestion[]; total: number; clustered: number }
}

export interface BgmHit {
  id: number | string
  title: string
  artist: string
  tags: string
  duration: number
  preview_url: string
  page_url: string
  license_url: string
  audiodownload_allowed: boolean
  audiodownload: string
}
export type TranslateSource = 'gemini' | 'dict' | 'none'
export interface BgmSearchResult {
  total: number
  hits: BgmHit[]
  empty: boolean
  query_used: string
  translated: boolean
  translate_source: TranslateSource
}
export interface BgmSuggestResult {
  keyword: string
  keywords: string[]
  hits: BgmHit[]
  empty: boolean
}

// Google Drive Picker
export interface DriveConfig {
  enabled: boolean
  client_id: string
  api_key: string
  app_id: string
  authenticated: boolean
  reason: string
}
export type DriveSortOrder = 'captured' | 'selected'
export interface DriveImportResult {
  ok: boolean
  imported: { id: string | null; filename: string | null; type?: string; error?: string }[]
  count: number
  skipped: { file_id: string; name: string; reason: string }[]
}

// 간편 자동 모드
export interface EasyEstimate {
  length: number        // 초
  photos: number
  videos: number
  video_seconds: number
  slide_duration: number
}
export type EasyFitMode = 'adjust' | 'trim' | 'loop'
export interface EasyCandidate extends BgmHit {
  fit: { mode: EasyFitMode; scale: number; label: string }
}
export interface EasyMusicCandidates {
  length: number
  keywords: string[]
  keywords_source: 'gemini' | 'dict' | 'default' | 'user'
  query_used: string
  translated: boolean
  translate_source: TranslateSource
  duration_range: [number, number]
  broadened: boolean
  empty: boolean
  candidates: EasyCandidate[]
}
export interface BgmCredit {
  title: string
  artist: string
  license_url: string
  page_url: string
  jamendo_id: number | string
}
export interface EasyRenderOptions {
  resolution?: '720p' | '1080p'
  stylize_ratio?: number
  slide_duration?: number
  auto_curate?: boolean
}
export interface EasyStatus {
  task: { status?: 'running' | 'done' | 'error'; phase?: string; progress?: number; message?: string; summary?: unknown }
  render: { status?: string; progress?: number; message?: string }
}

// 간편 자동 모드 자막 (meta.easy_place / easy_feel) — 빈 문자열은 서버에서 삭제
function parseMetaObj(s: Slide): Record<string, unknown> {
  try {
    const parsed = s.meta ? JSON.parse(s.meta) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
  } catch { /* 손상된 meta 는 새로 작성 */ }
  return {}
}
export function getEasyCaption(s: Slide): { place: string; feel: string } {
  const m = parseMetaObj(s)
  return {
    place: typeof m.easy_place === 'string' ? m.easy_place : '',
    feel: typeof m.easy_feel === 'string' ? m.easy_feel : '',
  }
}
export function withEasyCaption(s: Slide, place: string, feel: string): string {
  return JSON.stringify({ ...parseMetaObj(s), easy_place: place, easy_feel: feel })
}
// 자동 설정된 필드 목록 (meta.auto_fields)
export function getAutoFields(s: Slide): string[] {
  const v = parseMetaObj(s).auto_fields
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}
// 사용자가 손대서 다시 섞기에서 빠지는 슬라이드 (meta.user_locked)
export function isUserLocked(s: Slide): boolean {
  return parseMetaObj(s).user_locked === true
}
// 편집기에서 자동 필드를 수정했을 때 서버의 자동 잠금을 로컬에도 반영한 meta
export function withAutoFieldsTouched(s: Slide, fields: string[]): string | null {
  const m = parseMetaObj(s)
  const auto = Array.isArray(m.auto_fields) ? (m.auto_fields as unknown[]).filter((x): x is string => typeof x === 'string') : []
  const hit = fields.filter(f => auto.includes(f))
  if (!hit.length) return null
  return JSON.stringify({ ...m, auto_fields: auto.filter(f => !hit.includes(f)), user_locked: true })
}
export const AUTO_FIELD_LABELS: Record<string, string> = {
  transition: '전환',
  photo_effect: '그림 효과',
  effect_reveal: '드러내기',
  effect_to_real: '그림→실사',
  ken_burns: 'Ken Burns',
  location_card: '장소 카드',
  easy_duration: '슬라이드 길이',
}

// 오타 검증 결과
export interface SpellcheckResult {
  slide_id: string
  original: string
  corrected: string
  has_error: boolean
}

// ─── Projects ───────────────────────────────
export const api = {
  // 프로젝트
  listProjects: () => request<Project[]>('GET', '/projects'),
  createProject: (name: string) => request<Project>('POST', '/projects', { name }),
  getProject: (id: string) => request<ProjectDetail>('GET', `/projects/${id}`),
  deleteProject: (id: string) => request<void>('DELETE', `/projects/${id}`),
  renameProject: (id: string, name: string) => request<Project>('PATCH', `/projects/${id}/rename`, { name }),

  // 슬라이드
  getSlides: (id: string) => request<Slide[]>('GET', `/projects/${id}/slides`),
  uploadSlides: async (projectId: string, files: File[], insertAt?: number): Promise<Slide[]> => {
    const formData = new FormData()
    files.forEach(f => formData.append('files', f))
    if (insertAt !== undefined && insertAt !== -1) {
      formData.append('insert_at', String(insertAt))
    }
    const res = await fetch(`${BASE}/projects/${projectId}/slides/upload`, {
      method: 'POST',
      body: formData
    })
    if (!res.ok) throw new Error('업로드 실패')
    return res.json()
  },
  saveSlides: (id: string, slides: Slide[]) =>
    request<{ ok: boolean }>('PUT', `/projects/${id}/slides`, slides),
  deleteSlide: (projectId: string, slideId: string) =>
    request<void>('DELETE', `/projects/${projectId}/slides/${slideId}`),
  reorderSlides: (projectId: string, slideIds: string[]) =>
    request<Slide[]>('PUT', `/projects/${projectId}/slides/order`, { slide_ids: slideIds }),

  // AI 나레이션 (8-5/8-6)
  generateNarration: (projectId: string, slideId: string) =>
    request<Slide>('POST', `/projects/${projectId}/slides/${slideId}/narration`),
  generateAllNarrations: (projectId: string, overwrite = false, tone: 'documentary' | 'vlog' = 'documentary') =>
    request<{ status: string; total?: number; skipped?: number }>(
      'POST', `/projects/${projectId}/narration/generate-all`, { overwrite, tone }),
  getNarrationStatus: (projectId: string) =>
    request<{ status: string; progress: number; done?: number; total?: number; failed?: number; message?: string }>(
      'GET', `/projects/${projectId}/narration/generate-all/status`),
  splitScript: (projectId: string, script: string) =>
    request<{ ok: boolean; assigned: number }>(
      'POST', `/projects/${projectId}/narration/split`, { script }),

  // 오타 검증 (맞춤법/띄어쓰기)
  spellcheck: (projectId: string) =>
    request<{ results: SpellcheckResult[]; checked: number; errors: number }>(
      'POST', `/projects/${projectId}/spellcheck`),
  applySpellcheck: (projectId: string, fixes: { slide_id: string; corrected: string }[]) =>
    request<{ ok: boolean; applied: number }>(
      'POST', `/projects/${projectId}/spellcheck/apply`, { fixes }),

  // 렌더링
  startRender: (id: string) => request<{ ok: boolean }>('POST', `/projects/${id}/render`),
  getRenderStatus: (id: string) => request<RenderStatus>('GET', `/projects/${id}/render/status`),
  downloadUrl: (id: string, version?: string | number) =>
    `${BASE}/projects/${id}/download${version ? `?v=${encodeURIComponent(version)}` : ''}`,

  // 원클릭 자동 영상 (11-1): 큐레이션 → 스마트 나레이션 → 자동 연출 → 렌더
  startAutoVideo: (projectId: string, opts: AutoVideoOptions) =>
    request<{ ok: boolean }>('POST', `/projects/${projectId}/auto-video`, opts),
  getAutoVideoStatus: (projectId: string) =>
    request<AutoVideoStatus>('GET', `/projects/${projectId}/auto-video/status`),

  // 자동 편집 1단계 (12): 음악 인트로 + 위치 카드 + 키네틱 → 렌더
  startAutoEdit: (projectId: string, opts: AutoEditOptions) =>
    request<{ ok: boolean }>('POST', `/projects/${projectId}/auto-edit`, opts),
  getAutoEditStatus: (projectId: string) =>
    request<AutoEditStatus>('GET', `/projects/${projectId}/auto-edit/status`),

  // 슬라이드 미리보기 (구간 렌더) — 6-19/6-20
  requestPreview: (projectId: string, slideId: string,
                   opts: { include_neighbors?: boolean; force_tts?: boolean }) =>
    request<{ status: string; cached: boolean; hash: string }>(
      'POST', `/projects/${projectId}/slides/${slideId}/preview`, opts),
  previewVideoUrl: (projectId: string, slideId: string,
                    opts: { include_neighbors?: boolean; force_tts?: boolean },
                    cacheBust?: string | number) => {
    const p = new URLSearchParams()
    if (opts.include_neighbors) p.set('include_neighbors', 'true')
    if (opts.force_tts) p.set('force_tts', 'true')
    if (cacheBust !== undefined) p.set('v', String(cacheBust))
    const q = p.toString()
    return `${BASE}/projects/${projectId}/slides/${slideId}/preview${q ? `?${q}` : ''}`
  },
  // 미리보기 렌더 완료 여부 (본문 다운로드 없이 헤더로 판별)
  previewReady: async (url: string): Promise<boolean> => {
    try {
      const res = await fetch(url, { method: 'GET', headers: { Range: 'bytes=0-1' }, cache: 'no-store' })
      if (res.status === 202) return false
      const ct = res.headers.get('content-type') || ''
      try { await res.body?.cancel() } catch { /* ignore */ }
      return res.ok && ct.includes('video')
    } catch { return false }
  },

  // YouTube
  uploadYouTube: (id: string, payload: YouTubeUploadPayload) =>
    request<YouTubeUploadState & { ok: boolean }>('POST', `/projects/${id}/youtube/upload`, payload),
  youtubeAuthStatus: () => request<YouTubeAuthStatus>('GET', '/youtube/auth-status'),
  youtubeMeta: (id: string, opts: { regen?: boolean; ai?: boolean } = {}) => {
    const q = new URLSearchParams()
    if (opts.regen) q.set('regen', '1')
    if (opts.ai === false) q.set('ai', '0')
    const qs = q.toString()
    return request<YouTubeMeta>('GET', `/projects/${id}/youtube/meta${qs ? `?${qs}` : ''}`)
  },
  saveYoutubeMeta: (id: string, meta: YouTubeMetaFields) =>
    request<YouTubeMeta>('PUT', `/projects/${id}/youtube/meta`, meta),
  uploadYoutubeThumbnail: (id: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ ok: boolean; url: string }>('POST', `/projects/${id}/youtube/thumbnail`, fd, true)
  },
  youtubeStatus: (id: string) => request<YouTubeUploadState>('GET', `/projects/${id}/youtube/status`),
  youtubeAuthUrl: () => request<{ auth_url: string }>('GET', '/youtube/auth-url'),
  youtubeExchangeCode: (code: string) => request<{ ok: boolean }>('POST', '/youtube/exchange-code', { code }),

  assetUrl: (projectId: string, filename: string) =>
    `${BASE}/projects/${projectId}/assets/${filename}`,

  // BGM
  uploadBgm: async (projectId: string, file: File): Promise<Project> => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch(`${BASE}/projects/${projectId}/bgm`, { method: 'POST', body: formData })
    if (!res.ok) throw new Error('BGM 업로드 실패')
    return res.json()
  },
  deleteBgm: (projectId: string) => request<Project>('DELETE', `/projects/${projectId}/bgm`),
  searchBgm: (projectId: string, q: string, page = 1, instrumental = false) =>
    request<BgmSearchResult>('GET', `/projects/${projectId}/bgm/search?q=${encodeURIComponent(q)}&page=${page}&instrumental=${instrumental}`),
  suggestBgm: (projectId: string) =>
    request<BgmSuggestResult>('POST', `/projects/${projectId}/bgm/suggest`),
  downloadBgm: (projectId: string, url: string, filename: string) =>
    request<Project>('POST', `/projects/${projectId}/bgm/download`, { url, filename }),

  // 프로젝트 설정 (BGM 볼륨, 화면 비율, TTS 마스터 볼륨)
  updateSettings: (projectId: string, settings: Record<string, unknown>) =>
    request<Project>('PATCH', `/projects/${projectId}/settings`, settings),

  // 갤러리 이미지 콜라주
  createCollage: async (projectId: string, slideIds: string[], layout?: string): Promise<Slide> => {
    return request<Slide>('POST', `/projects/${projectId}/slides/collage`, { slide_ids: slideIds, layout: layout || 'auto' })
  },

  // 경로 슬라이드 자동 생성 (OSM/OSRM)
  createRouteSlide: (
    projectId: string,
    payload: { origin: string; destination: string; profile?: string; insert_at?: number; duration?: number; n_frames?: number },
  ) => request<Slide>('POST', `/projects/${projectId}/slides/route`, payload),

  // 경로 슬라이드 재생성 (좌표 유지, profile/n_frames/duration 갱신)
  regenerateRouteSlide: (
    projectId: string,
    slideId: string,
    payload: { profile?: string; n_frames?: number; duration?: number },
  ) => request<Slide>('POST', `/projects/${projectId}/slides/${slideId}/route/regenerate`, payload),

  // 자주 쓰는 장소 (전역: 집/회사)
  listLocations: () => request<SavedLocation[]>('GET', '/locations'),
  geocode: (q: string) => request<GeocodeResult>('GET', `/geocode?q=${encodeURIComponent(q)}`),
  createLocation: (payload: { name: string; query: string }) =>
    request<SavedLocation>('POST', '/locations', payload),
  updateLocation: (id: string, payload: { name?: string; query?: string }) =>
    request<SavedLocation>('PUT', `/locations/${id}`, payload),
  deleteLocation: (id: string) => request<void>('DELETE', `/locations/${id}`),

  // 장소 슬라이드 자동 생성 (Nominatim/Overpass)
  createPlaceSlide: (
    projectId: string,
    payload: { query: string; insert_at?: number },
  ) => request<Slide>('POST', `/projects/${projectId}/slides/place`, payload),

  // Google Photos Picker
  photosAuthStatus: () => request<{ authenticated: boolean; reason?: string }>('GET', '/photos/auth-status'),
  photosAuthUrl: () => request<{ auth_url: string }>('GET', '/photos/auth-url'),
  photosExchangeCode: (code: string) => request<{ ok: boolean }>('POST', '/photos/exchange-code', { code }),
  photosCreateSession: () => request<PickerSession>('POST', '/photos/session'),
  photosPollSession: (sessionId: string) => request<PickerSession>('GET', `/photos/session/${sessionId}`),
  photosImport: (projectId: string, sessionId: string, sortOrder: PhotosSortOrder = 'selected') =>
    request<PickerImportResult>('POST', `/photos/import/${projectId}`, { session_id: sessionId, sort_order: sortOrder }),

  // Google Drive Picker
  driveConfig: () => request<DriveConfig>('GET', '/drive/config'),
  driveToken: () => request<{ access_token: string; expires_at: number | null }>('GET', '/drive/token'),
  driveAuthUrl: () => request<{ auth_url: string }>('GET', '/drive/auth-url'),
  driveExchangeCode: (code: string) => request<{ ok: boolean }>('POST', '/drive/exchange-code', { code }),
  driveImport: (projectId: string, fileIds: string[], sortOrder: DriveSortOrder = 'captured') =>
    request<DriveImportResult>('POST', `/drive/import/${projectId}`, { file_ids: fileIds, sort_order: sortOrder }),

  // 간편 자동 모드
  easyEstimate: (id: string, slideDuration?: number) =>
    request<EasyEstimate>('GET', `/projects/${id}/easy/estimate${slideDuration !== undefined ? `?slide_duration=${slideDuration}` : ''}`),
  easyMusicCandidates: (id: string, body: { keywords?: string[]; query?: string; slide_duration?: number }) =>
    request<EasyMusicCandidates>('POST', `/projects/${id}/easy/music-candidates`, body),
  easyChooseMusic: (id: string, track: BgmHit) =>
    request<{ ok: boolean; bgm_filename: string; credit: BgmCredit; credit_text: string }>('POST', `/projects/${id}/easy/music`, { track }),
  easyCredit: (id: string) => request<{ text: string; credit: BgmCredit | null }>('GET', `/projects/${id}/easy/credit`),
  easyRender: (id: string, opts: EasyRenderOptions) =>
    request<{ ok: boolean }>('POST', `/projects/${id}/easy/render`, opts),
  easyStatus: (id: string) => request<EasyStatus>('GET', `/projects/${id}/easy/status`),
  easyReshuffle: (id: string, seed?: number) =>
    request<{ ok: boolean; seed: number; changed: number }>('POST', `/projects/${id}/easy/reshuffle`, seed !== undefined ? { seed } : {}),
  setSlideLock: (projectId: string, slideId: string, locked: boolean) =>
    request<{ ok: boolean; meta: string }>('POST', `/projects/${projectId}/slides/${slideId}/lock`, { locked }),

  // EXIF 자동 구성 (Photo-Vlog F3)
  scanExif: (projectId: string) =>
    request<{ scanned: number; with_time: number; with_gps: number }>('POST', `/projects/${projectId}/slides/exif/scan`),
  getOrganizeSuggestions: (projectId: string) =>
    request<OrganizeSuggestions>('GET', `/projects/${projectId}/organize/suggestions`),
  sortSlides: (projectId: string) =>
    request<{ ok: boolean; reordered: number }>('POST', `/projects/${projectId}/organize/sort`),
  insertRouteSuggestion: (
    projectId: string,
    payload: { after_slide_id: string; origin: { lat: number; lng: number; name?: string }; destination: { lat: number; lng: number; name?: string }; profile?: string },
  ) => request<{ ok: boolean; slide_id: string; label: string }>('POST', `/projects/${projectId}/organize/insert-route`, payload),

  // 스마트 사진 선별 (Photo-Vlog F1)
  runCuration: (projectId: string) =>
    request<{ status: string; total: number }>('POST', `/projects/${projectId}/curation/run`),
  getCurationStatus: (projectId: string) =>
    request<CurationStatus>('GET', `/projects/${projectId}/curation/status`),
  applyCuration: (projectId: string, slideIds: string[]) =>
    request<{ ok: boolean; deleted: number; remaining: number }>(
      'POST', `/projects/${projectId}/curation/apply`, { slide_ids: slideIds }),

  // TTS 음성 관리 (다중 음성)
  listVoices: () =>
    request<{ voices: string[]; count: number }>('GET', '/voices'),
  registerVoice: async (voiceName: string, refAudio: File, refText: string) => {
    const fd = new FormData()
    fd.append('voice_name', voiceName)
    fd.append('ref_audio', refAudio)
    fd.append('ref_text', refText)
    const res = await fetch(`${BASE}/voices`, { method: 'POST', body: fd })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || '음성 등록 실패')
    }
    return res.json()
  },
  deleteVoice: (voiceName: string) =>
    request<{ ok: boolean }>('DELETE', `/voices/${encodeURIComponent(voiceName)}`),
}
