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
  slide_type: 'image' | 'video'
  video_filename: string
  volume: number
  subtitles: string  // JSON array string
  use_tts: number   // 1=TTS on, 0=subtitle only
  trim_start?: number
  trim_end?: number
  transition?: string  // 'none' | 'crossfade' | 'fade_black' | 'slide_left' | 'slide_right'
  tts_volume?: number  // TTS 볼륨 (0.0 ~ 2.0)
  rotation?: number    // 0, 90, 180, 270
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

// ─── Projects ───────────────────────────────
export const api = {
  // 프로젝트
  listProjects: () => request<Project[]>('GET', '/projects'),
  createProject: (name: string) => request<Project>('POST', '/projects', { name }),
  getProject: (id: string) => request<ProjectDetail>('GET', `/projects/${id}`),
  deleteProject: (id: string) => request<void>('DELETE', `/projects/${id}`),

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
  downloadUrl: (id: string) => `${BASE}/projects/${id}/download`,

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

  // 프로젝트 설정 (BGM 볼륨, 화면 비율, TTS 마스터 볼륨)
  updateSettings: (projectId: string, settings: Record<string, unknown>) =>
    request<Project>('PATCH', `/projects/${projectId}/settings`, settings),

  // 갤러리 이미지 콜라주
  createCollage: async (projectId: string, slideIds: string[], layout?: string): Promise<Slide> => {
    return request<Slide>('POST', `/projects/${projectId}/slides/collage`, { slide_ids: slideIds, layout: layout || 'auto' })
  },

  // Google Photos Picker
  photosAuthStatus: () => request<{ authenticated: boolean; reason?: string }>('GET', '/photos/auth-status'),
  photosAuthUrl: () => request<{ auth_url: string }>('GET', '/photos/auth-url'),
  photosExchangeCode: (code: string) => request<{ ok: boolean }>('POST', '/photos/exchange-code', { code }),
  photosCreateSession: () => request<PickerSession>('POST', '/photos/session'),
  photosPollSession: (sessionId: string) => request<PickerSession>('GET', `/photos/session/${sessionId}`),
  photosImport: (projectId: string, sessionId: string) =>
    request<PickerImportResult>('POST', `/photos/import/${projectId}`, { session_id: sessionId }),
}
