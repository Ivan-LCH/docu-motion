import { useState, useEffect, useRef, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  api, ApiError, Slide, ProjectDetail, EasyEstimate, EasyMusicCandidates, EasyCandidate, BgmHit,
  getEasyCaption, withEasyCaption, PHOTO_EFFECT_SHORT, TRANSITION_PRESETS, EasyStatus,
} from '../api/client'
import { useToast } from '../components/ToastContext'
import PhotosPickerModal from '../components/PhotosPickerModal'
import DrivePickerButton from '../components/DrivePickerButton'

// ─── 공통 상수·헬퍼 ───────────────────────────
const SLIDE_DURATION = 3.5
type Step = 1 | 2 | 3 | 4
type RenderState = 'idle' | 'running' | 'error' | 'done'

const PHASE_LABEL: Record<string, string> = {
  curating: '흐림·중복 컷 정리 중',
  planning: '효과·전환 배치 중',
  fitting: '음악 길이 맞추는 중',
  rendering: '렌더링 중',
}

function fmt(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !isFinite(sec) || sec < 0) return '-:--'
  const s = Math.round(sec)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const isMedia = (s: Slide) => s.slide_type === 'image' || s.slide_type === 'video'

// 영상 클립 길이: 트림 구간 우선, 없으면 메타데이터 길이
function clipLength(s: Slide, metaDur?: number): number | null {
  const start = s.trim_start || 0
  if (s.trim_end && s.trim_end > start) return s.trim_end - start
  if (metaDur && isFinite(metaDur)) return Math.max(0, metaDur - start)
  return null
}

// CC 라이선스 URL → 짧은 표기 (예: CC BY-NC-SA)
function licenseShort(url: string): string {
  const m = url?.match(/licenses\/([a-z-]+)\//i)
  return m ? `CC ${m[1].toUpperCase()}` : '라이선스'
}

function metaObj(s: Slide): Record<string, unknown> {
  try { const m = s.meta ? JSON.parse(s.meta) : {}; return m && typeof m === 'object' ? m : {} } catch { return {} }
}

function errMsg(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

// Jamendo 오류 코드별 안내
function musicErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === 'jamendo_error') return '음악 서버 오류 — 잠시 후 다시 시도해 주세요'
    if (e.code === 'jamendo_key_missing') return 'Jamendo 키가 설정되지 않았어요 (관리자 설정 필요)'
  }
  return errMsg(e, '음악을 불러오지 못했어요')
}

const panel: React.CSSProperties = {
  background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '1.5rem',
}
const muted: React.CSSProperties = { color: 'var(--text-muted)', fontSize: '0.8rem', lineHeight: 1.6 }
const h1: React.CSSProperties = { fontSize: '2rem', fontWeight: 700, margin: '0 0 0.5rem' }
const bigBtn: React.CSSProperties = { padding: '0.8rem 1.6rem', fontSize: '1rem', fontWeight: 700 }

// ─── 미디어 썸네일 (사진 / 영상) ─────────────────
function MediaThumb({ projectId, slide, duration, onDuration, style, showBadge = true, controls = false }: {
  projectId: string; slide: Slide; duration?: number
  onDuration?: (id: string, d: number) => void
  style?: React.CSSProperties; showBadge?: boolean; controls?: boolean
}) {
  const isVideo = slide.slide_type === 'video'
  const base: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover', display: 'block', background: '#000' }
  const len = isVideo ? clipLength(slide, duration) : null
  return (
    <div style={{ position: 'relative', overflow: 'hidden', borderRadius: 'var(--radius-md)', background: 'var(--bg-secondary)', aspectRatio: '16/9', ...style }}>
      {isVideo && slide.video_filename ? (
        <video
          src={`${api.assetUrl(projectId, slide.video_filename)}#t=${(slide.trim_start || 0) + 0.1}`}
          preload="metadata" muted playsInline controls={controls} style={base}
          onLoadedMetadata={e => {
            const d = (e.target as HTMLVideoElement).duration
            if (d && isFinite(d)) onDuration?.(slide.id, d)
          }}
        />
      ) : slide.image_filename ? (
        <img src={api.assetUrl(projectId, slide.image_filename)} alt="" loading="lazy" style={base} />
      ) : null}
      {isVideo && showBadge && (
        <span style={{
          position: 'absolute', right: 8, bottom: 8, fontSize: '0.72rem', color: '#fff', padding: '0.1rem 0.45rem',
          background: 'rgba(0,0,0,0.6)', borderRadius: 999,
        }}>▶ {fmt(len)}</span>
      )}
    </div>
  )
}

// ─── 단계 표시 ──────────────────────────────
const STEP_LABELS = ['가져오기', '자막', '음악', '렌더']
const CIRCLED = ['①', '②', '③', '④']

function StepPills({ step, maxStep, done, onGo }: { step: Step; maxStep: Step; done: boolean; onGo: (s: Step) => void }) {
  return (
    <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
      {STEP_LABELS.map((label, i) => {
        const n = (i + 1) as Step
        const current = n === step
        const completed = n < step || (n === 4 && done) || (n < maxStep && !current)
        const clickable = n <= maxStep && !current
        return (
          <button key={n} onClick={() => clickable && onGo(n)} disabled={!clickable && !current}
            style={{
              padding: '0.35rem 0.9rem', borderRadius: 999, fontSize: '0.85rem', fontWeight: current ? 700 : 500,
              border: '1px solid ' + (current ? 'var(--accent)' : 'var(--border)'),
              background: current ? 'var(--accent)' : 'var(--bg-card)',
              color: current ? '#fff' : completed ? 'var(--text-secondary)' : 'var(--text-muted)',
              cursor: clickable ? 'pointer' : 'default',
            }}>
            {CIRCLED[i]} {n === 4 && done ? '렌더 완료' : label}{completed && !current ? ' ✓' : ''}
          </button>
        )
      })}
    </div>
  )
}

// ─── ① 가져오기 ──────────────────────────────
function ImportStep({ project, media, durations, onDuration, reload, autoCurate, setAutoCurate, onNext }: {
  project: ProjectDetail; media: Slide[]; durations: Record<string, number>
  onDuration: (id: string, d: number) => void
  reload: () => Promise<void>
  autoCurate: boolean; setAutoCurate: (v: boolean) => void
  onNext: () => void
}) {
  const { toast } = useToast()
  const [title, setTitle] = useState(project.name)
  const [showPhotos, setShowPhotos] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  useEffect(() => { setTitle(project.name) }, [project.name])

  const saveTitle = async () => {
    const name = title.trim()
    if (!name || name === project.name) { setTitle(project.name); return }
    try { await api.renameProject(project.id, name); await reload() }
    catch (e) { toast(errMsg(e, '제목 변경 실패'), 'error') }
  }

  const handleFiles = async (files: FileList | null) => {
    if (!files || !files.length) return
    setUploading(true)
    try {
      await api.uploadSlides(project.id, Array.from(files))
      toast(`${files.length}개 올리기 완료`, 'success')
      await reload()
    } catch (e) { toast(errMsg(e, '업로드 실패'), 'error') }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = '' }
  }

  const photos = media.filter(s => s.slide_type === 'image').length
  const videos = media.length - photos

  const source = (icon: string, name: string, sub: string, action: React.ReactNode) => (
    <div style={{ ...panel, padding: '1rem 1.25rem', display: 'flex', alignItems: 'center', gap: '1rem' }}>
      <span style={{ fontSize: '1.6rem', width: 36, textAlign: 'center' }}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600 }}>{name}</div>
        <div style={muted}>{sub}</div>
      </div>
      {action}
    </div>
  )

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(360px, 5fr) 7fr', gap: '2.5rem', alignItems: 'start' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div>
          <h1 style={h1}>새 영상 만들기</h1>
          <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            사진과 영상을 고르면 자막만 적고 바로 완성해요.<br />효과·음악·전환은 자동으로 들어가요.
          </p>
        </div>
        <div>
          <label className="label">제목</label>
          <input className="input" value={title} onChange={e => setTitle(e.target.value)} onBlur={saveTitle}
            onKeyDown={e => e.key === 'Enter' && (e.target as HTMLInputElement).blur()} />
        </div>
        <label className="label" style={{ marginBottom: 0 }}>어디서 가져올까요?</label>
        {source('🖼️', 'Google 포토에서 고르기', 'Picker 새 창 · 사진+영상 · 촬영 시간순 정렬',
          <button className="btn btn-primary btn-sm" onClick={() => setShowPhotos(true)}>열기</button>)}
        {source('📁', 'Google 드라이브', '드라이브에서 사진·영상 여러 개 고르기',
          <DrivePickerButton projectId={project.id} onImported={() => { reload() }} sortOrder="captured" compact />)}
        {source('⬆️', '내 컴퓨터에서 올리기', '사진·영상 파일 여러 개',
          <>
            <input ref={fileRef} type="file" multiple accept="image/*,video/*" style={{ display: 'none' }}
              onChange={e => handleFiles(e.target.files)} />
            <button className="btn btn-secondary btn-sm" onClick={() => fileRef.current?.click()} disabled={uploading}>
              {uploading ? <><span className="spinner" /> 올리는 중</> : '파일'}
            </button>
          </>)}
      </div>

      <div style={{ ...panel, minHeight: 520, display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <h2 style={{ fontSize: '1.3rem', fontWeight: 700, margin: 0 }}>가져온 항목 {media.length}개</h2>
          <span style={muted}>사진 {photos} · 영상 {videos}</span>
        </div>
        {media.length === 0 ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', ...muted, fontSize: '0.9rem' }}>
            왼쪽에서 사진이나 영상을 가져와 주세요
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '0.6rem', maxHeight: '60vh', overflowY: 'auto' }}>
            {media.map(s => (
              <MediaThumb key={s.id} projectId={project.id} slide={s} duration={durations[s.id]} onDuration={onDuration} />
            ))}
          </div>
        )}
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          <input type="checkbox" checked={autoCurate} onChange={e => setAutoCurate(e.target.checked)} />
          흐림·중복 컷은 자동으로 빼기
          <span style={{
            fontSize: '0.72rem', padding: '0.05rem 0.5rem', borderRadius: 999,
            background: autoCurate ? 'var(--accent-light)' : 'var(--bg-secondary)', color: autoCurate ? 'var(--accent)' : 'var(--text-muted)',
          }}>{autoCurate ? '켜짐' : '꺼짐'}</span>
        </label>
        <div style={{ marginTop: 'auto', display: 'flex', justifyContent: 'flex-end' }}>
          <button className="btn btn-primary" style={bigBtn} disabled={media.length === 0} onClick={onNext}>다음 · 자막 적기 →</button>
        </div>
      </div>

      {showPhotos && (
        <PhotosPickerModal projectId={project.id} onClose={() => setShowPhotos(false)} onImported={() => { reload() }} />
      )}
    </div>
  )
}

// ─── ② 자막 ────────────────────────────────
function CaptionStep({ project, media, durations, onDuration, onCaption, onBack, onNext }: {
  project: ProjectDetail; media: Slide[]; durations: Record<string, number>
  onDuration: (id: string, d: number) => void
  onCaption: (slideId: string, place: string, feel: string) => void
  onBack: () => void; onNext: () => void
}) {
  const [active, setActive] = useState(0)
  const [estimate, setEstimate] = useState<EasyEstimate | null>(null)

  useEffect(() => {
    api.easyEstimate(project.id, SLIDE_DURATION).then(setEstimate).catch(() => setEstimate(null))
  }, [project.id, media.length])

  // 실제로 표시될 장소: 자기 장소 → 앞쪽 가장 가까운 장소 → 프로젝트 제목
  const effectivePlace = (idx: number): string => {
    for (let i = idx; i >= 0; i--) {
      const p = getEasyCaption(media[i]).place.trim()
      if (p) return p
    }
    return project.name
  }

  const cur = media[Math.min(active, media.length - 1)]
  const curCap = cur ? getEasyCaption(cur) : { place: '', feel: '' }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(520px, 3fr) 2fr', gap: '2.5rem', alignItems: 'start' }}>
      <div>
        <h1 style={h1}>자막만 적어주세요</h1>
        <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6, marginBottom: '1.5rem' }}>
          장소와 느낌 두 칸뿐이에요. 나레이션(TTS)은 넣지 않아요. 장소가 비면 앞 장소를 이어받고, GPS가 있으면 자동으로 채워요.
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: '28px 140px 70px 1fr 1fr', gap: '0.75rem', ...muted, marginBottom: '0.4rem' }}>
          <span /><span /><span /><span>장소</span><span>느낌 (한 줄)</span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {media.map((s, i) => {
            const cap = getEasyCaption(s)
            const isVideo = s.slide_type === 'video'
            return (
              <div key={s.id} onMouseEnter={() => setActive(i)} onFocusCapture={() => setActive(i)}
                style={{
                  display: 'grid', gridTemplateColumns: '28px 140px 70px 1fr 1fr', gap: '0.75rem', alignItems: 'center',
                  padding: '0.6rem 0', borderBottom: '1px solid var(--border)',
                  background: i === active ? 'var(--bg-hover)' : undefined, borderRadius: 'var(--radius-sm)',
                }}>
                <span style={{ ...muted, textAlign: 'center' }}>{i + 1}</span>
                <MediaThumb projectId={project.id} slide={s} duration={durations[s.id]} onDuration={onDuration} showBadge={false} />
                <span style={{
                  fontSize: '0.75rem', padding: '0.2rem 0.5rem', borderRadius: 'var(--radius-sm)', background: 'var(--bg-secondary)',
                  color: 'var(--text-secondary)', lineHeight: 1.4, justifySelf: 'start',
                }}>
                  {isVideo ? <>영상<br />{fmt(clipLength(s, durations[s.id]))}</> : '사진'}
                </span>
                <div style={{ position: 'relative' }}>
                  <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', fontSize: '0.85rem', pointerEvents: 'none' }}>📍</span>
                  <input className="input" style={{ paddingLeft: '2rem' }} value={cap.place}
                    placeholder={i === 0 ? '장소 (비우면 GPS·제목)' : '↑ 위와 같은 장소'}
                    onChange={e => onCaption(s.id, e.target.value, cap.feel)} />
                </div>
                <input className="input" value={cap.feel} placeholder="비워두면 자막 없이 통과"
                  onChange={e => onCaption(s.id, cap.place, e.target.value)} />
              </div>
            )
          })}
        </div>
      </div>

      <div style={{ position: 'sticky', top: 88, display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div style={muted}>미리보기 · {active + 1}번째 컷</div>
        {cur && (
          <div style={{ position: 'relative', borderRadius: 'var(--radius-lg)', overflow: 'hidden' }}>
            <MediaThumb projectId={project.id} slide={cur} duration={durations[cur.id]} showBadge={false} style={{ borderRadius: 0 }} />
            {/* 한쪽 정렬 잡지식 캡션 — 테두리 없이 부드러운 그림자만 */}
            <div style={{ position: 'absolute', inset: 0, background: 'linear-gradient(to top, rgba(0,0,0,0.28), rgba(0,0,0,0) 45%)', pointerEvents: 'none' }} />
            <div style={{ position: 'absolute', left: '6%', bottom: '10%', right: '30%', color: '#fff', textShadow: '0 1px 10px rgba(0,0,0,0.55)', pointerEvents: 'none' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: '#e5484d', flexShrink: 0, boxShadow: '0 0 6px rgba(0,0,0,0.4)' }} />
                <span style={{ fontSize: '1.7rem', fontWeight: 700, lineHeight: 1.2 }}>{effectivePlace(active)}</span>
              </div>
              {curCap.feel.trim() && (
                <div style={{ fontSize: '1rem', marginTop: '0.3rem', marginLeft: '1.05rem', opacity: 0.95 }}>{curCap.feel}</div>
              )}
            </div>
          </div>
        )}
        <div style={muted}>한쪽 정렬 잡지식 캡션 · 글자 테두리 없음 · 비네팅 최소</div>
        <div style={{ ...panel, padding: '1.1rem 1.4rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontWeight: 600 }}>예상 영상 길이</span>
            <span style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--accent)' }}>{estimate ? fmt(estimate.length) : '-:--'}</span>
          </div>
          {estimate && (
            <div style={{ ...muted, marginTop: '0.3rem' }}>
              사진 {estimate.photos}장 × {estimate.slide_duration}초 + 영상 {Math.round(estimate.video_seconds)}초 + 인트로·전환
            </div>
          )}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '1rem' }}>
          <button className="btn btn-secondary" style={bigBtn} onClick={onBack}>← 뒤로</button>
          <button className="btn btn-primary" style={bigBtn} onClick={onNext}>다음 · 음악 자동 고르기 →</button>
        </div>
      </div>
    </div>
  )
}

// ─── ③ 음악 ────────────────────────────────
function MusicStep({ project, music, setMusic, selected, setSelected, resolution, setResolution, stylize, setStylize, onProject, onBack, onRender, rendering }: {
  project: ProjectDetail
  music: EasyMusicCandidates | null; setMusic: (m: EasyMusicCandidates | null) => void
  selected: EasyCandidate | null; setSelected: (c: EasyCandidate | null) => void
  resolution: '720p' | '1080p'; setResolution: (r: '720p' | '1080p') => void
  stylize: number; setStylize: (v: number) => void
  onProject: (patch: Partial<ProjectDetail>) => void
  onBack: () => void; onRender: () => void; rendering: boolean
}) {
  const { toast } = useToast()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [keywords, setKeywords] = useState<string[]>(music?.keywords ?? [])
  const [editIdx, setEditIdx] = useState<number | null>(null)
  const [editVal, setEditVal] = useState('')
  const [query, setQuery] = useState('')
  const [searched, setSearched] = useState('')    // 마지막으로 검색한 입력어
  const [playingId, setPlayingId] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  const [volume, setVolume] = useState(project.bgm_volume ?? 0.3)
  const volTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchCandidates = useCallback(async (body: { keywords?: string[]; query?: string }) => {
    setLoading(true)
    setError('')
    try {
      const res = await api.easyMusicCandidates(project.id, body)
      setMusic(res)
      setKeywords(res.keywords)
      // 첫 후보를 기본 선택 (이전 선택이 목록에 있으면 유지)
      setSelected(res.candidates.find(c => selected && String(c.id) === String(selected.id)) ?? res.candidates[0] ?? null)
    } catch (e) {
      setError(musicErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [project.id, selected, setMusic, setSelected])

  // 진입 시 한 번 후보 불러오기 (이미 있으면 재사용)
  useEffect(() => {
    if (!music) fetchCandidates({})
    return () => { audioRef.current?.pause() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const updateKeywords = (next: string[]) => {
    const clean = next.map(k => k.trim()).filter(Boolean)
    setKeywords(clean)
    setSearched('')
    if (clean.length) fetchCandidates({ keywords: clean })
    else fetchCandidates({})
  }

  const commitEdit = () => {
    if (editIdx === null) return
    const next = [...keywords]
    if (editIdx >= next.length) next.push(editVal)
    else next[editIdx] = editVal
    setEditIdx(null)
    const changed = JSON.stringify(next.map(k => k.trim()).filter(Boolean)) !== JSON.stringify(keywords)
    if (changed) updateKeywords(next)
  }

  const doSearch = () => {
    const q = query.trim()
    if (!q) return
    setSearched(q)
    fetchCandidates({ query: q })
  }

  const togglePlay = (c: BgmHit) => {
    const a = audioRef.current
    if (!a) return
    const id = String(c.id)
    if (playingId === id) { a.pause(); setPlayingId(null); return }
    a.src = c.preview_url
    a.play().then(() => setPlayingId(id)).catch(() => { setPlayingId(null); toast('미리듣기를 재생할 수 없어요', 'error') })
  }

  const changeVolume = (v: number) => {
    setVolume(v)
    if (volTimer.current) clearTimeout(volTimer.current)
    volTimer.current = setTimeout(async () => {
      try {
        // PATCH 는 누락 값을 기본값으로 되돌리므로 세 값을 함께 보낸다
        await api.updateSettings(project.id, {
          bgm_volume: v,
          aspect_ratio: project.aspect_ratio || '16:9',
          tts_master_volume: project.tts_master_volume ?? 1.0,
        })
        onProject({ bgm_volume: v })
      } catch { toast('볼륨 저장 실패', 'error') }
    }, 400)
  }

  const len = music?.length
  const range = music?.duration_range

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(520px, 2fr) 1fr', gap: '2.5rem', alignItems: 'start' }}>
      <audio ref={audioRef} onEnded={() => setPlayingId(null)} style={{ display: 'none' }} />
      <div>
        <h1 style={h1}>음악을 골라뒀어요</h1>
        <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6, marginBottom: '1.5rem' }}>
          자막(장소·느낌)을 읽고 영어 키워드로 바꿔 Jamendo에서 찾았어요.
          {len ? <> 영상 길이 <strong style={{ color: 'var(--accent)' }}>{fmt(len)}</strong>에 가까운 곡을 위에 놓았어요.</> : null}
        </p>

        <label className="label">AI 키워드 (눌러서 고치기)</label>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginBottom: '1rem', alignItems: 'center' }}>
          {[...keywords, ...(editIdx !== null && editIdx >= keywords.length ? [''] : [])].map((k, i) => (
            editIdx === i ? (
              <input key={`edit-${i}`} className="input" autoFocus value={editVal} onChange={e => setEditVal(e.target.value)}
                onBlur={commitEdit}
                onKeyDown={e => { if (e.key === 'Enter') commitEdit(); if (e.key === 'Escape') setEditIdx(null) }}
                style={{ width: 160, padding: '0.2rem 0.6rem', fontSize: '0.8rem', borderRadius: 999 }} />
            ) : (
              <span key={`${k}-${i}`} style={{
                display: 'inline-flex', alignItems: 'center', gap: '0.3rem', padding: '0.2rem 0.4rem 0.2rem 0.75rem', borderRadius: 999,
                background: 'var(--accent-light)', color: 'var(--accent)', fontSize: '0.8rem',
              }}>
                <span style={{ cursor: 'pointer' }} onClick={() => { setEditIdx(i); setEditVal(k) }} title="눌러서 고치기">{k}</span>
                <button onClick={() => updateKeywords(keywords.filter((_, j) => j !== i))} title="빼기"
                  style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', fontSize: '0.85rem', padding: '0 0.2rem' }}>×</button>
              </span>
            )
          ))}
          {editIdx === null && (
            <button className="btn btn-ghost btn-sm" style={{ borderRadius: 999, padding: '0.2rem 0.7rem', fontSize: '0.8rem' }}
              onClick={() => { setEditIdx(keywords.length); setEditVal('') }}>+ 추가</button>
          )}
        </div>

        <label className="label">직접 검색 (한국어도 OK · 자동으로 영어로 바꿔 검색)</label>
        <div className="input" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1.25rem' }}>
          <input value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && doSearch()}
            placeholder="예: 크리스마스 분위기, 잔잔한 피아노"
            style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: 'inherit', font: 'inherit' }} />
          {searched && searched === query.trim() && music?.translated && (
            <span style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}>→ {music.query_used}</span>
          )}
        </div>

        {loading && (
          <div style={{ padding: '2rem', textAlign: 'center' }}><span className="spinner spinner-lg" /></div>
        )}
        {!loading && error && (
          <div style={{ ...panel, color: 'var(--error)', fontSize: '0.9rem' }}>{error}</div>
        )}
        {!loading && !error && music && (music.empty || music.candidates.length === 0) && (
          <div style={{ ...panel, color: 'var(--text-secondary)', fontSize: '0.9rem' }}>검색 결과가 없어요. 다른 키워드로 찾아볼까요?</div>
        )}
        {!loading && !error && music && music.candidates.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
            {music.candidates.map(c => {
              const id = String(c.id)
              const sel = selected && String(selected.id) === id
              return (
                <div key={id} onClick={() => setSelected(c)}
                  style={{
                    ...panel, padding: '1rem 1.25rem', display: 'grid', gridTemplateColumns: '44px 1fr 70px 1.4fr 28px',
                    alignItems: 'center', gap: '1rem', cursor: 'pointer',
                    borderColor: sel ? 'var(--accent)' : 'var(--border)', background: sel ? 'var(--accent-light)' : 'var(--bg-card)',
                  }}>
                  <button onClick={e => { e.stopPropagation(); togglePlay(c) }} title="미리듣기"
                    style={{ width: 40, height: 40, borderRadius: '50%', border: 'none', cursor: 'pointer', background: 'var(--bg-hover)', color: 'var(--text-primary)', fontSize: '0.9rem' }}>
                    {playingId === id ? '❚❚' : '▶'}
                  </button>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      <strong>{c.title}</strong>
                      {c.artist && <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}> · {c.artist}</span>}
                    </div>
                    <div style={{ ...muted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {c.tags}
                      {c.license_url && (
                        <> · <a href={c.license_url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} style={{ color: 'var(--text-muted)' }}>라이선스</a></>
                      )}
                    </div>
                  </div>
                  <span style={{ fontSize: '1.15rem', fontWeight: 600 }}>{fmt(c.duration)}</span>
                  <span style={{ fontSize: '0.85rem', color: c.fit?.mode === 'adjust' ? 'var(--accent)' : 'var(--text-muted)' }}>{c.fit?.label}</span>
                  <input type="radio" checked={!!sel} onChange={() => setSelected(c)} onClick={e => e.stopPropagation()} style={{ accentColor: 'var(--accent)', transform: 'scale(1.2)' }} />
                </div>
              )
            })}
          </div>
        )}
        {music && range && (
          <div style={{ ...muted, marginTop: '0.75rem' }}>
            필터: 연주곡만 · 길이 {fmt(range[0])} ~ {fmt(range[1])} (durationbetween) · 다운로드 허용 곡만
            {music.broadened ? ' · 넓게 다시 검색함' : ''}
          </div>
        )}
      </div>

      <div style={{ position: 'sticky', top: 88, display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div style={panel}>
          <h3 style={{ fontSize: '1.1rem', fontWeight: 700, margin: '0 0 0.8rem' }}>자동으로 들어가는 효과</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', fontSize: '0.9rem', lineHeight: 1.6 }}>
            <div>🎬 전환 · CF 순환 (crossfade · whip · zoom · light leak)</div>
            <div>🎨 그림풍 → 실사 · 사진의 {Math.round(stylize * 100)}% · 수채 → 파스텔 스케치 → 부드러운 스케치 순환</div>
            <div>📍 장소 카드 · 한쪽 정렬 잡지식</div>
            <div>🎵 BGM 1초 페이드 인 · 끝 3초 페이드 아웃</div>
            <div>⏱ 사진 {SLIDE_DURATION}초 · {resolution} · 시네마틱 색감</div>
          </div>
          <p style={{ ...muted, marginTop: '0.8rem' }}>고르지 않아도 이 값으로 렌더돼요. 나중에 에디터에서 하나씩 바꿀 수 있어요.</p>
        </div>
        <div style={{ ...panel, display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <label className="label">BGM 볼륨</label>
              <span style={muted}>{Math.round(volume * 100)}%</span>
            </div>
            <input type="range" min={0} max={1} step={0.05} value={volume} onChange={e => changeVolume(parseFloat(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--accent)' }} />
          </div>
          <div>
            <label className="label">해상도</label>
            <div style={{ display: 'flex', gap: '0.4rem' }}>
              {(['720p', '1080p'] as const).map(r => (
                <button key={r} className={`btn btn-sm ${resolution === r ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setResolution(r)}>{r}</button>
              ))}
            </div>
          </div>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <label className="label">그림풍 → 실사 비율</label>
              <span style={muted}>{stylize <= 0 ? '끔' : `${Math.round(stylize * 100)}%`}</span>
            </div>
            <input type="range" min={0} max={0.5} step={0.05} value={stylize} onChange={e => setStylize(parseFloat(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--accent)' }} />
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '1rem' }}>
          <button className="btn btn-secondary" style={bigBtn} onClick={() => { audioRef.current?.pause(); onBack() }}>← 뒤로</button>
          <button className="btn btn-primary" style={bigBtn} disabled={!selected || rendering}
            onClick={() => { audioRef.current?.pause(); setPlayingId(null); onRender() }}>
            {rendering ? <><span className="spinner" /> 준비 중...</> : '▶ 바로 렌더'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── ④ 렌더 ────────────────────────────────
function RenderStep({ project, media, durations, onDuration, state, status, resolution, onRetry, onReshuffle, onOtherMusic, busy }: {
  project: ProjectDetail; media: Slide[]; durations: Record<string, number>
  onDuration: (id: string, d: number) => void
  state: RenderState; status: EasyStatus | null; resolution: string
  onRetry: () => void; onReshuffle: () => void; onOtherMusic: () => void; busy: boolean
}) {
  const navigate = useNavigate()
  const { toast } = useToast()
  const [credit, setCredit] = useState<{ text: string; credit: { title: string; artist: string; license_url: string } | null } | null>(null)
  const [videoLen, setVideoLen] = useState<number | null>(null)

  useEffect(() => {
    if (state !== 'done') return
    api.easyCredit(project.id).then(setCredit).catch(() => setCredit(null))
  }, [state, project.id, project.updated_at])

  if (state === 'running' || state === 'idle') {
    const task = status?.task ?? {}
    const phase = task.phase || ''
    const pct = phase === 'rendering' ? (status?.render?.progress ?? task.progress ?? 0) : (task.progress ?? 0)
    const msg = (phase === 'rendering' ? status?.render?.message : '') || task.message || ''
    return (
      <div style={{ maxWidth: 640, margin: '4rem auto', ...panel, padding: '2.5rem', textAlign: 'center' }}>
        <span className="spinner spinner-lg" />
        <h2 style={{ fontSize: '1.5rem', fontWeight: 700, margin: '1.2rem 0 0.4rem' }}>{PHASE_LABEL[phase] || '준비 중'}</h2>
        <p style={{ ...muted, fontSize: '0.9rem', minHeight: '1.5em' }}>{msg}</p>
        <div className="progress-bar-wrap" style={{ marginTop: '1rem' }}>
          <div className="progress-bar-fill" style={{ width: `${Math.max(3, Math.min(100, Math.round(pct)))}%` }} />
        </div>
        <div style={{ ...muted, marginTop: '0.4rem' }}>{Math.round(pct)}%</div>
        <p style={{ ...muted, marginTop: '1.2rem' }}>창을 닫아도 서버에서 계속 렌더돼요. 다시 열면 이어서 보여드려요.</p>
      </div>
    )
  }

  if (state === 'error') {
    return (
      <div style={{ maxWidth: 640, margin: '4rem auto', ...panel, padding: '2.5rem', textAlign: 'center' }}>
        <h2 style={{ fontSize: '1.4rem', fontWeight: 700, color: 'var(--error)', marginBottom: '0.6rem' }}>렌더하지 못했어요</h2>
        <p style={{ color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginBottom: '1.5rem' }}>
          {status?.task?.message || status?.render?.message || project.message || '알 수 없는 오류'}
        </p>
        <div style={{ display: 'flex', gap: '0.6rem', justifyContent: 'center' }}>
          <button className="btn btn-primary" onClick={onRetry} disabled={busy}>다시 시도</button>
          <button className="btn btn-secondary" onClick={onOtherMusic}>🎵 다른 곡으로</button>
          <button className="btn btn-ghost" onClick={() => navigate(`/project/${project.id}`)}>✎ 세부 편집 열기</button>
        </div>
      </div>
    )
  }

  const videoUrl = api.downloadUrl(project.id, project.updated_at)
  const c = credit?.credit
  const copy = async () => {
    try { await navigator.clipboard.writeText(credit?.text || ''); toast('복사했어요', 'success') }
    catch { toast('복사 실패', 'error') }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
      <div style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: '2.5rem', alignItems: 'start' }}>
        <video key={videoUrl} controls src={videoUrl}
          onLoadedMetadata={e => { const d = (e.target as HTMLVideoElement).duration; if (d && isFinite(d)) setVideoLen(d) }}
          style={{ width: '100%', borderRadius: 'var(--radius-lg)', background: '#000', display: 'block' }} />
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.8rem' }}>
          <h1 style={{ ...h1, marginBottom: 0 }}>완성됐어요</h1>
          <div style={{ color: 'var(--text-secondary)' }}>
            {fmt(videoLen)} · {project.resolution || resolution}
            {c && <> · BGM “{c.title}” ({c.artist}{c.license_url ? `, ${licenseShort(c.license_url)}` : ''})</>}
          </div>
          <a className="btn btn-primary btn-full" href={videoUrl} download={`${project.name}.mp4`} style={{ ...bigBtn, justifyContent: 'flex-start' }}>⬇ 다운로드</a>
          <button className="btn btn-secondary btn-full" style={{ ...bigBtn, justifyContent: 'flex-start' }} onClick={() => navigate(`/project/${project.id}`)}>✎ 세부 편집 열기</button>
          <button className="btn btn-secondary btn-full" style={{ ...bigBtn, justifyContent: 'flex-start' }} onClick={onReshuffle} disabled={busy}>
            {busy ? <><span className="spinner" /> 섞는 중...</> : '🎲 효과 다시 섞기'}
          </button>
          <button className="btn btn-secondary btn-full" style={{ ...bigBtn, justifyContent: 'flex-start' }} onClick={onOtherMusic}>🎵 다른 곡으로</button>
          <p style={muted}>세부 편집은 기존 에디터로 열려요. 자동으로 넣은 효과는 슬라이드마다 '자동' 표시가 붙고, 손댄 슬라이드는 다시 섞기에서 빠져요.</p>
          <div>
            <label className="label">음악 크레딧 (YouTube 설명에 붙여넣기)</label>
            <textarea className="textarea" readOnly value={credit?.text || ''} rows={4} style={{ fontSize: '0.8rem' }} />
            <button className="btn btn-ghost btn-sm" style={{ marginTop: '0.4rem' }} onClick={copy} disabled={!credit?.text}>복사</button>
          </div>
        </div>
      </div>

      <div style={panel}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '1rem' }}>
          <h3 style={{ fontSize: '1.1rem', fontWeight: 700, margin: 0 }}>자동으로 넣은 효과</h3>
          <span style={muted}>칸을 누르면 그 슬라이드 설정이 열려요</span>
        </div>
        <div style={{ display: 'grid', gridAutoFlow: 'column', gridAutoColumns: '200px', gap: '0.75rem', overflowX: 'auto', paddingBottom: '0.4rem' }}>
          {media.map((s, i) => {
            const m = metaObj(s)
            const eff = s.photo_effect && s.photo_effect !== 'none' ? s.photo_effect : null
            const badge = i === 0 && m.intro_kinetic ? '인트로'
              : eff ? `${PHOTO_EFFECT_SHORT[eff]}→실사`
              : s.slide_type === 'video' ? '영상' : '실사'
            const accentBadge = badge !== '실사'
            const tr = TRANSITION_PRESETS.find(t => t.value === (s.transition || 'crossfade'))
            return (
              <div key={s.id} onClick={() => navigate(`/project/${project.id}`)} style={{ cursor: 'pointer' }} title="에디터에서 열기">
                <MediaThumb projectId={project.id} slide={s} duration={durations[s.id]} onDuration={onDuration} showBadge={false} />
                <span style={{
                  display: 'inline-block', marginTop: '0.5rem', fontSize: '0.75rem', padding: '0.1rem 0.6rem', borderRadius: 999,
                  background: accentBadge ? 'var(--accent-light)' : 'var(--bg-secondary)', color: accentBadge ? 'var(--accent)' : 'var(--text-muted)',
                  maxWidth: '100%', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>{badge}</span>
                <div style={{ ...muted, marginTop: '0.25rem' }}>전환 · {tr?.short ?? s.transition}</div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ─── 메인 페이지 ─────────────────────────────
export default function EasyAuto() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const { toast } = useToast()
  const id = projectId || ''

  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [slides, setSlides] = useState<Slide[]>([])
  const slidesRef = useRef<Slide[]>([])
  const [step, setStep] = useState<Step>(1)
  const [maxStep, setMaxStep] = useState<Step>(1)
  const [durations, setDurations] = useState<Record<string, number>>({})
  const [autoCurate, setAutoCurateState] = useState<boolean>(() => localStorage.getItem(`easy_auto_curate:${projectId}`) !== '0')
  const [music, setMusic] = useState<EasyMusicCandidates | null>(null)
  const [selected, setSelected] = useState<EasyCandidate | null>(null)
  const [chosenId, setChosenId] = useState<string | null>(null)  // 서버에 이미 적용된 곡 (jamendo_id)
  const [resolution, setResolution] = useState<'720p' | '1080p'>('1080p')
  const [stylize, setStylize] = useState(0.3)
  const [renderState, setRenderState] = useState<RenderState>('idle')
  const [status, setStatus] = useState<EasyStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setAutoCurate = (v: boolean) => {
    setAutoCurateState(v)
    localStorage.setItem(`easy_auto_curate:${id}`, v ? '1' : '0')
  }

  const go = (s: Step) => {
    setStep(s)
    setMaxStep(m => (s > m ? s : m))
  }

  const reload = useCallback(async () => {
    if (!id) return
    const p = await api.getProject(id)
    setProject(p)
    setSlides(p.slides)
    slidesRef.current = p.slides
  }, [id])

  // 첫 진입: 프로젝트 + 진행 상태 + 적용된 곡 확인 후 시작 단계 결정
  useEffect(() => {
    if (!id) return
    let alive = true
    ;(async () => {
      try {
        const p = await api.getProject(id)
        if (!alive) return
        setProject(p)
        setSlides(p.slides)
        slidesRef.current = p.slides
        const [st, cr] = await Promise.all([
          api.easyStatus(id).catch(() => null),
          api.easyCredit(id).catch(() => null),
        ])
        if (!alive) return
        if (cr?.credit) setChosenId(String(cr.credit.jamendo_id))
        setStatus(st)
        if (st?.task?.status === 'running') {
          setRenderState('running'); setStep(4); setMaxStep(4)
        } else if (p.status === 'COMPLETED' && p.has_video) {
          setRenderState('done'); setStep(4); setMaxStep(4)
        } else if (st?.task?.status === 'error') {
          setRenderState('error'); setStep(4); setMaxStep(4)
        }
      } catch {
        toast('프로젝트를 불러올 수 없어요', 'error')
        navigate('/')
      }
    })()
    return () => { alive = false }
  }, [id, navigate, toast])

  // 렌더 진행 폴링 (2초)
  useEffect(() => {
    if (renderState !== 'running' || !id) return
    const timer = setInterval(async () => {
      try {
        const st = await api.easyStatus(id)
        setStatus(st)
        if (st.task?.status === 'done') {
          await reload()
          setRenderState('done')
          toast('영상이 완성됐어요', 'success')
        } else if (st.task?.status === 'error') {
          await reload().catch(() => {})
          setRenderState('error')
        }
      } catch { /* 일시 오류는 다음 폴링에서 재시도 */ }
    }, 2000)
    return () => clearInterval(timer)
  }, [renderState, id, reload, toast])

  // 자막 저장 — 전체 슬라이드 목록을 디바운스(600ms) 저장
  const flushSave = useCallback(async () => {
    if (!saveTimer.current) return
    clearTimeout(saveTimer.current)
    saveTimer.current = null
    try { await api.saveSlides(id, slidesRef.current.map((s, j) => ({ ...s, order_index: j }))) }
    catch { toast('자막 저장 실패', 'error') }
  }, [id, toast])

  const onCaption = (slideId: string, place: string, feel: string) => {
    const next = slidesRef.current.map(s => s.id === slideId ? { ...s, meta: withEasyCaption(s, place, feel) } : s)
    slidesRef.current = next
    setSlides(next)
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(async () => {
      saveTimer.current = null
      try { await api.saveSlides(id, slidesRef.current.map((s, j) => ({ ...s, order_index: j }))) }
      catch { toast('자막 저장 실패', 'error') }
    }, 600)
  }

  // 페이지를 떠날 때 남은 자막 저장
  useEffect(() => () => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current)
      api.saveSlides(id, slidesRef.current.map((s, j) => ({ ...s, order_index: j }))).catch(() => {})
    }
  }, [id])

  const onDuration = useCallback((sid: string, d: number) => {
    setDurations(prev => (prev[sid] === d ? prev : { ...prev, [sid]: d }))
  }, [])

  const startRender = async () => {
    await flushSave()
    try {
      await api.easyRender(id, { resolution, stylize_ratio: stylize, slide_duration: SLIDE_DURATION, auto_curate: autoCurate })
    } catch (e) {
      // 409: 이미 진행 중 → 진행 화면으로
      if (!(e instanceof ApiError && e.status === 409)) throw e
    }
    setStatus(null)
    setRenderState('running')
    go(4)
  }

  const handleRender = async () => {
    if (!selected) return
    setStarting(true)
    try {
      if (String(selected.id) !== chosenId) {
        await api.easyChooseMusic(id, selected)
        setChosenId(String(selected.id))
      }
      await startRender()
    } catch (e) {
      toast(errMsg(e, '렌더 시작 실패'), 'error')
    } finally {
      setStarting(false)
    }
  }

  const handleRetry = async () => {
    setStarting(true)
    try { await startRender() }
    catch (e) { toast(errMsg(e, '렌더 시작 실패'), 'error') }
    finally { setStarting(false) }
  }

  const handleReshuffle = async () => {
    setStarting(true)
    try {
      const res = await api.easyReshuffle(id)
      toast(`효과를 다시 섞었어요 (${res.changed}개 슬라이드)`, 'success')
      await startRender()
    } catch (e) {
      toast(errMsg(e, '다시 섞기 실패'), 'error')
    } finally {
      setStarting(false)
    }
  }

  if (!project) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: '6rem' }}><span className="spinner spinner-lg" /></div>
  }

  const media = slides.filter(isMedia)

  return (
    <main className="page" style={{ padding: '1.5rem 3rem 3rem', maxWidth: 1600, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem', gap: '1rem', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/')}>← 목록</button>
          <span style={{ fontWeight: 600, color: 'var(--accent)' }}>✦ 간편 자동</span>
          <span style={muted}>{project.name}</span>
        </div>
        <StepPills step={step} maxStep={maxStep} done={step === 4 && renderState === 'done'}
          onGo={s => { if (step === 2) flushSave(); go(s) }} />
      </div>

      {step === 1 && (
        <ImportStep project={project} media={media} durations={durations} onDuration={onDuration} reload={reload}
          autoCurate={autoCurate} setAutoCurate={setAutoCurate} onNext={() => go(2)} />
      )}
      {step === 2 && (
        <CaptionStep project={project} media={media} durations={durations} onDuration={onDuration} onCaption={onCaption}
          onBack={() => { flushSave(); go(1) }} onNext={() => { flushSave(); go(3) }} />
      )}
      {step === 3 && (
        <MusicStep project={project} music={music} setMusic={setMusic} selected={selected} setSelected={setSelected}
          resolution={resolution} setResolution={setResolution} stylize={stylize} setStylize={setStylize}
          onProject={patch => setProject(prev => prev ? { ...prev, ...patch } : prev)}
          onBack={() => go(2)} onRender={handleRender} rendering={starting} />
      )}
      {step === 4 && (
        <RenderStep project={project} media={media} durations={durations} onDuration={onDuration}
          state={renderState} status={status} resolution={resolution}
          onRetry={handleRetry} onReshuffle={handleReshuffle} onOtherMusic={() => go(3)} busy={starting} />
      )}
    </main>
  )
}
