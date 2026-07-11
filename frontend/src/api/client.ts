// DocuMotion - API Client
const BASE = '/api/v1'

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
    throw new Error(err.detail || 'API Error')
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json()
}

// ─── Types ──────────────────────────────────
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
  transition?: string  // 'none' | 'crossfade' | 'fade_black' | 'slide_left' | 'slide_right'
  tts_volume?: number  // TTS 볼륨 (0.0 ~ 2.0)
  rotation?: number    // 0, 90, 180, 270
  overlays?: string    // JSON array of overlay objects
  image_fit?: 'cover' | 'fit'  // cover: 전체화면, fit: 이미지 위쪽+하단 자막 영역
  ken_burns?: number   // Ken Burns 강도 0~100 (0=정적, 100=최대 줌)
  meta?: string        // 타입별 추가 데이터 JSON (route/place)
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
  default_transition: string
  default_slide_duration: number
  subtitle_font_size: number
  subtitle_font_color: string
  watermark_text: string
  watermark_opacity: number
  title_text: string
}

export interface ProjectDetail extends Project {
  slides: Slide[]
}

export interface RenderStatus {
  status: string
  progress: number
  message: string
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

export interface BgmHit {
  id: number
  title: string
  tags: string
  duration: number
  preview_url: string
  page_url: string
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

  // 렌더링
  startRender: (id: string) => request<{ ok: boolean }>('POST', `/projects/${id}/render`),
  getRenderStatus: (id: string) => request<RenderStatus>('GET', `/projects/${id}/render/status`),
  downloadUrl: (id: string, version?: string | number) =>
    `${BASE}/projects/${id}/download${version ? `?v=${encodeURIComponent(version)}` : ''}`,

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
  uploadYouTube: (id: string, payload: { title: string; description: string; tags?: string }) =>
    request<{ ok: boolean; url: string }>('POST', `/projects/${id}/youtube/upload`, payload),
  youtubeAuthStatus: () => request<{ authenticated: boolean; reason?: string }>('GET', '/youtube/auth-status'),
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
  searchBgm: (projectId: string, q: string, page = 1) =>
    request<{ total: number; hits: BgmHit[] }>('GET', `/projects/${projectId}/bgm/search?q=${encodeURIComponent(q)}&page=${page}`),
  suggestBgm: (projectId: string) =>
    request<{ keyword: string; hits: BgmHit[] }>('POST', `/projects/${projectId}/bgm/suggest`),
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
}
