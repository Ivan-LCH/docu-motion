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
}

export interface ProjectDetail extends Project {
  slides: Slide[]
}

export interface RenderStatus {
  status: string
  progress: number
  message: string
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

  assetUrl: (projectId: string, filename: string) =>
    `${BASE}/projects/${projectId}/assets/${filename}`,

  // 갤러리 이미지 콜라주 
  createCollage: async (projectId: string, slideIds: string[]): Promise<Slide> => {
    return request<Slide>('POST', `/projects/${projectId}/slides/collage`, { slide_ids: slideIds })
  }
}
