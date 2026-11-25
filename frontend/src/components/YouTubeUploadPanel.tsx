import { useState, useEffect, useRef, useCallback } from 'react'
import {
  api, ApiError, YouTubeAuthStatus, YouTubeMeta, YouTubeMetaFields, YouTubePrivacy, YouTubeUploadState,
} from '../api/client'
import { useToast } from './ToastContext'
import { extractCodeFromUrl } from './GoogleAuthSection'

// ─── 헬퍼 ────────────────────────────────────
const TAGS_LIMIT = 500
const TITLE_LIMIT = 100
const DESC_LIMIT = 5000
const ACTIVE = ['queued', 'uploading', 'processing']

// YouTube 태그 길이 규칙: 공백 있는 태그는 따옴표 2자 추가, 쉼표 포함
export function tagsLength(tags: string[]): number {
  return tags.reduce((n, t) => n + t.length + (t.includes(' ') ? 2 : 0), 0) + Math.max(0, tags.length - 1)
}

function byteLength(s: string): number {
  return new TextEncoder().encode(s).length
}

const AUTH_TEXT: Record<string, string> = {
  no_token: 'YouTube 연결이 필요해요',
  reauth_required: 'YouTube 인증이 만료됐어요',
  scope_missing: '업로드 권한이 없어요 — 다시 인증해 주세요',
}

const PRIVACY_OPTIONS: { value: YouTubePrivacy; label: string }[] = [
  { value: 'private', label: '비공개' },
  { value: 'unlisted', label: '일부 공개' },
]

const PRIVACY_DONE: Record<string, string> = {
  private: '비공개로 올렸어요',
  unlisted: '일부 공개로 올렸어요',
  public: '공개로 올렸어요',
}

const muted: React.CSSProperties = { color: 'var(--text-muted)', fontSize: '0.78rem' }

function Segmented<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void
}) {
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--border)', borderRadius: 999, padding: 2, background: 'var(--bg-secondary)' }}>
      {options.map(o => (
        <button key={o.value} type="button" onClick={() => onChange(o.value)}
          style={{
            padding: '0.3rem 0.9rem', borderRadius: 999, border: 'none', fontSize: '0.85rem', cursor: 'pointer',
            background: value === o.value ? 'var(--accent)' : 'transparent',
            color: value === o.value ? '#fff' : 'var(--text-secondary)', fontWeight: value === o.value ? 600 : 400,
          }}>{o.label}</button>
      ))}
    </div>
  )
}

// ─── 인증 안내 (한 줄 + 코드 붙여넣기) ───────────
function AuthNotice({ status, onDone }: { status: YouTubeAuthStatus; onDone: () => void }) {
  const { toast } = useToast()
  const [codeMode, setCodeMode] = useState(false)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)

  const start = async () => {
    try {
      const { auth_url } = await api.youtubeAuthUrl()
      window.open(auth_url, '_blank')
      setCodeMode(true)
      setCode('')
    } catch (e) {
      toast(e instanceof Error ? e.message : '인증 URL 생성 실패', 'error')
    }
  }

  const finish = async () => {
    const c = extractCodeFromUrl(code)
    if (!c) { toast('주소창 URL을 붙여넣어 주세요', 'error'); return }
    setBusy(true)
    try {
      await api.youtubeExchangeCode(c)
      toast('YouTube 연결됨', 'success')
      setCodeMode(false)
      onDone()
    } catch (e) {
      toast(e instanceof Error ? e.message : '인증 실패', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ padding: '0.6rem 0.8rem', borderRadius: 'var(--radius-sm)', border: '1px solid var(--warning)', background: 'rgba(245,158,11,0.08)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.6rem' }}>
        <span style={{ fontSize: '0.875rem', color: 'var(--warning)' }}>
          {AUTH_TEXT[status.reason || ''] || 'YouTube 인증을 확인해 주세요'}
        </span>
        {!codeMode && <button className="btn btn-primary btn-sm" onClick={start}>인증</button>}
      </div>
      {codeMode && (
        <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.5rem' }}>
          <input className="input" style={{ fontSize: '0.8rem' }} autoFocus
            placeholder="로그인 후 주소창 URL 붙여넣기"
            value={code} onChange={e => setCode(e.target.value)} onKeyDown={e => e.key === 'Enter' && finish()} />
          <button className="btn btn-primary btn-sm" onClick={finish} disabled={busy}>{busy ? '...' : '완료'}</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setCodeMode(false)}>취소</button>
        </div>
      )}
    </div>
  )
}

// ─── 태그 칩 ──────────────────────────────────
function TagChips({ tags, onChange }: { tags: string[]; onChange: (t: string[]) => void }) {
  const [draft, setDraft] = useState('')
  const add = (raw: string) => {
    const parts = raw.split(',').map(t => t.replace(/[<>]/g, '').trim()).filter(Boolean)
    if (!parts.length) return
    const next = [...tags]
    for (const p of parts) if (!next.some(t => t.toLowerCase() === p.toLowerCase())) next.push(p.slice(0, 30))
    onChange(next)
    setDraft('')
  }
  const len = tagsLength(tags)
  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', padding: '0.45rem', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', background: 'var(--bg-secondary)' }}>
        {tags.map(t => (
          <span key={t} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', padding: '0.15rem 0.3rem 0.15rem 0.6rem', borderRadius: 999, background: 'var(--accent-light)', color: 'var(--text-primary)', fontSize: '0.8rem' }}>
            {t}
            <button type="button" onClick={() => onChange(tags.filter(x => x !== t))} aria-label={`${t} 삭제`}
              style={{ border: 'none', background: 'transparent', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '0.8rem', padding: '0 0.2rem' }}>✕</button>
          </span>
        ))}
        <input value={draft} onChange={e => {
          const v = e.target.value
          if (v.includes(',')) add(v); else setDraft(v)
        }}
          onKeyDown={e => {
            if (e.key === 'Enter') { e.preventDefault(); add(draft) }
            else if (e.key === 'Backspace' && !draft && tags.length) onChange(tags.slice(0, -1))
          }}
          onBlur={() => draft && add(draft)}
          placeholder={tags.length ? '' : '태그 입력 후 Enter'}
          style={{ flex: 1, minWidth: 100, border: 'none', outline: 'none', background: 'transparent', color: 'var(--text-primary)', fontSize: '0.85rem' }} />
      </div>
      <div style={{ ...muted, textAlign: 'right', marginTop: '0.2rem', color: len > TAGS_LIMIT ? 'var(--error)' : 'var(--text-muted)' }}>
        {len}/{TAGS_LIMIT}
      </div>
    </div>
  )
}

// ─── 패널 ────────────────────────────────────
export default function YouTubeUploadPanel({ projectId, layout = 'wide' }: { projectId: string; layout?: 'wide' | 'modal' }) {
  const { toast } = useToast()
  const [auth, setAuth] = useState<YouTubeAuthStatus | null>(null)
  const [meta, setMeta] = useState<YouTubeMeta | null>(null)
  const [fields, setFields] = useState<YouTubeMetaFields | null>(null)
  const [job, setJob] = useState<YouTubeUploadState | null>(null)
  const [showForm, setShowForm] = useState(true)
  const [regenerating, setRegenerating] = useState(false)
  const [starting, setStarting] = useState(false)
  const [thumbUploading, setThumbUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const fieldsRef = useRef<YouTubeMetaFields | null>(null)

  const checkAuth = useCallback(() => {
    api.youtubeAuthStatus().then(setAuth).catch(() => setAuth({ authenticated: false, reason: 'error' }))
  }, [])

  const applyMeta = (m: YouTubeMeta) => {
    setMeta(m)
    const f = { title: m.title, description: m.description, tags: m.tags, privacy: m.privacy, thumbnail: m.thumbnail || 'none' }
    fieldsRef.current = f
    setFields(f)
  }

  useEffect(() => {
    checkAuth()
    api.youtubeMeta(projectId).then(applyMeta).catch(e => toast(e instanceof Error ? e.message : '메타 불러오기 실패', 'error'))
    api.youtubeStatus(projectId).then(st => {
      setJob(st)
      if (st.status === 'done' || ACTIVE.includes(st.status)) setShowForm(false)
    }).catch(() => {})
  }, [projectId, checkAuth, toast])

  // 진행 폴링 (2초)
  const active = !!job && ACTIVE.includes(job.status)
  useEffect(() => {
    if (!active) return
    const t = setInterval(async () => {
      try {
        const st = await api.youtubeStatus(projectId)
        setJob(st)
        if (st.status === 'done') toast('YouTube에 올렸어요', 'success')
      } catch { /* 다음 폴링에서 재시도 */ }
    }, 2000)
    return () => clearInterval(t)
  }, [active, projectId, toast])

  // 편집 내용 자동 저장 (1초 디바운스)
  const flush = useCallback(async () => {
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null }
    if (fieldsRef.current) await api.saveYoutubeMeta(projectId, fieldsRef.current).catch(() => {})
  }, [projectId])

  useEffect(() => () => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current)
      if (fieldsRef.current) api.saveYoutubeMeta(projectId, fieldsRef.current).catch(() => {})
    }
  }, [projectId])

  const update = (patch: Partial<YouTubeMetaFields>) => {
    if (!fieldsRef.current) return
    const next = { ...fieldsRef.current, ...patch }
    fieldsRef.current = next
    setFields(next)
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => { saveTimer.current = null; flush() }, 1000)
  }

  const regen = async () => {
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null }
    setRegenerating(true)
    try {
      applyMeta(await api.youtubeMeta(projectId, { regen: true }))
    } catch (e) {
      toast(e instanceof Error ? e.message : '다시 만들기 실패', 'error')
    } finally {
      setRegenerating(false)
    }
  }

  const onThumbFile = async (file: File | undefined) => {
    if (!file) return
    setThumbUploading(true)
    try {
      const { url } = await api.uploadYoutubeThumbnail(projectId, file)
      setMeta(m => m ? { ...m, uploaded_thumbnail: url } : m)
      update({ thumbnail: 'upload' })
    } catch (e) {
      toast(e instanceof Error ? e.message : '썸네일 업로드 실패', 'error')
    } finally {
      setThumbUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const upload = async () => {
    const f = fieldsRef.current
    if (!f) return
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null }
    setStarting(true)
    try {
      const st = await api.uploadYouTube(projectId, f)
      setJob(st)
      setShowForm(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth({ authenticated: false, reason: e.code || 'no_token' })
      } else if (e instanceof ApiError && e.status === 409) {
        api.youtubeStatus(projectId).then(setJob).catch(() => {})
        setShowForm(false)
      } else {
        toast(e instanceof Error ? e.message : '업로드 시작 실패', 'error')
      }
    } finally {
      setStarting(false)
    }
  }

  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); toast('복사했어요', 'success') }
    catch { toast('복사 실패', 'error') }
  }

  // ── 진행 / 완료 / 오류 화면 ──
  if (job && !showForm && (active || job.status === 'done' || job.status === 'error')) {
    const pct = Math.round(job.progress || 0)
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.8rem', maxWidth: layout === 'wide' ? 640 : undefined }}>
        {active && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', fontWeight: 600 }}>
              <span className="spinner" /> {job.status === 'queued' ? '업로드 준비 중' : job.status === 'processing' ? '마무리 중' : 'YouTube에 올리는 중'}
            </div>
            <div className="progress-bar-wrap"><div className="progress-bar-fill" style={{ width: `${Math.max(3, pct)}%` }} /></div>
            <div style={muted}>{pct}% · 창을 닫아도 계속 올라가요</div>
          </>
        )}
        {job.status === 'done' && job.url && (
          <>
            <div style={{ fontWeight: 700, fontSize: '1.05rem', color: 'var(--success)' }}>✓ {PRIVACY_DONE[job.privacy || 'private']}</div>
            <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
              <a href={job.url} target="_blank" rel="noreferrer" className="input"
                style={{ flex: 1, textDecoration: 'none', color: 'var(--accent)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{job.url}</a>
              <button className="btn btn-secondary btn-sm" onClick={() => copy(job.url || '')}>복사</button>
            </div>
            {job.warning && <div style={{ ...muted, color: 'var(--warning)' }}>{job.warning}</div>}
            <div><button className="btn btn-ghost btn-sm" onClick={() => setShowForm(true)}>다시 올리기</button></div>
          </>
        )}
        {job.status === 'error' && (
          <>
            <div style={{ color: 'var(--error)', fontWeight: 600 }}>{job.error || '업로드하지 못했어요'}</div>
            <div><button className="btn btn-primary btn-sm" onClick={() => { setShowForm(true); checkAuth() }}>다시 시도</button></div>
          </>
        )}
      </div>
    )
  }

  if (!fields || !meta) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}><span className="spinner" /></div>
  }

  const creditFirst = (meta.credit || '').split('\n')[0]
  const creditMissing = !!creditFirst && !fields.description.includes(creditFirst)
  const descBytes = byteLength(fields.description)
  const tagsOver = tagsLength(fields.tags) > TAGS_LIMIT
  const canUpload = auth?.authenticated === true && !starting && !tagsOver && descBytes <= DESC_LIMIT

  const thumbBox = (selected: boolean): React.CSSProperties => ({
    width: 96, height: 54, borderRadius: 6, overflow: 'hidden', cursor: 'pointer', padding: 0, flex: '0 0 auto',
    border: selected ? '2px solid var(--accent)' : '1px solid var(--border)', background: 'var(--bg-secondary)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)', fontSize: '0.78rem',
  })

  const textFields = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
      <div>
        <label className="label">제목</label>
        <input className="input" value={fields.title} maxLength={TITLE_LIMIT} onChange={e => update({ title: e.target.value })} />
      </div>
      <div>
        <label className="label">설명</label>
        <textarea className="textarea" rows={8} value={fields.description} onChange={e => update({ description: e.target.value })}
          style={{ fontSize: '0.85rem', lineHeight: 1.55 }} />
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '0.2rem' }}>
          <span style={muted}>
            {meta.credit && (creditMissing
              ? <button className="btn btn-ghost" style={{ fontSize: '0.75rem', padding: 0, color: 'var(--warning)' }}
                  onClick={() => update({ description: `${fields.description.trimEnd()}\n\n${meta.credit}` })}>♪ 음악 크레딧 넣기</button>
              : '♪ 음악 크레딧 포함')}
          </span>
          <span style={{ ...muted, color: descBytes > DESC_LIMIT ? 'var(--error)' : 'var(--text-muted)' }}>{fields.description.length}</span>
        </div>
      </div>
      <div>
        <label className="label">태그</label>
        <TagChips tags={fields.tags} onChange={tags => update({ tags })} />
      </div>
    </div>
  )

  const sideFields = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>
      {auth && !auth.authenticated && <AuthNotice status={auth} onDone={checkAuth} />}
      {auth?.authenticated && auth.channel && <div style={muted}>채널 · {auth.channel}</div>}
      <div>
        <label className="label">공개 범위</label>
        <Segmented value={fields.privacy === 'public' ? 'unlisted' : fields.privacy} options={PRIVACY_OPTIONS}
          onChange={privacy => update({ privacy })} />
      </div>
      <div>
        <label className="label">썸네일 (선택)</label>
        <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
          <button type="button" style={thumbBox(fields.thumbnail === 'none')} onClick={() => update({ thumbnail: 'none' })}>없음</button>
          {meta.thumbnails.map(t => (
            <button type="button" key={t.slide_id} style={thumbBox(fields.thumbnail === `slide:${t.index}`)}
              onClick={() => update({ thumbnail: `slide:${t.index}` })} title={`슬라이드 ${t.index + 1}`}>
              <img src={t.url} alt="" loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            </button>
          ))}
          {meta.uploaded_thumbnail && (
            <button type="button" style={thumbBox(fields.thumbnail === 'upload')} onClick={() => update({ thumbnail: 'upload' })}>
              <img src={meta.uploaded_thumbnail} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            </button>
          )}
          <button type="button" style={thumbBox(false)} onClick={() => fileRef.current?.click()} disabled={thumbUploading}>
            {thumbUploading ? <span className="spinner" /> : '＋ 파일'}
          </button>
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={e => onThumbFile(e.target.files?.[0])} />
        </div>
      </div>
      <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.3rem' }}>
        <button className="btn btn-secondary" onClick={regen} disabled={regenerating}>
          {regenerating ? <><span className="spinner" /> 만드는 중</> : '↻ 다시 만들기'}
        </button>
        <button className="btn btn-primary" style={{ flex: 1, justifyContent: 'center' }} onClick={upload} disabled={!canUpload}>
          {starting ? <><span className="spinner" /> 시작 중</> : '업로드'}
        </button>
      </div>
    </div>
  )

  if (layout === 'modal') {
    return <div style={{ display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>{textFields}{sideFields}</div>
  }
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: '2.5rem', alignItems: 'start' }}>
      {textFields}
      {sideFields}
    </div>
  )
}
