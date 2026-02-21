import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, Project } from '../api/client'
import { useToast } from '../components/ToastContext'

// ─── Status helpers ──────────────────────────
function statusBadge(p: Project) {
  const map: Record<string, [string, string]> = {
    DRAFT:      ['badge-draft',      '📝 초안'],
    QUEUED:     ['badge-queued',     '⏳ 대기 중'],
    PROCESSING: ['badge-processing', '🔄 렌더링 중'],
    COMPLETED:  ['badge-completed',  '✅ 완료'],
    ERROR:      ['badge-error',      '❌ 오류'],
  }
  const [cls, label] = map[p.status] ?? ['badge-draft', p.status]
  return <span className={`badge ${cls}`}>{label}</span>
}

function stageBadge(p: Project) {
  if (p.status !== 'DRAFT') return null
  const map: Record<string, string> = {
    initialized: '— 초기화',
    uploaded:    '📂 업로드됨',
    scripted:    '📝 자막 입력됨',
  }
  return <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{map[p.stage] ?? p.stage}</span>
}

// ─── Confirm Dialog ──────────────────────────
function ConfirmModal({ name, onConfirm, onCancel }: { name: string; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" style={{ maxWidth: 380 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">🗑️ 프로젝트 삭제</span>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
          <strong style={{ color: 'var(--error)' }}>"{name}"</strong> 프로젝트와 모든 영상 파일이<br />
          영구 삭제됩니다. 계속하시겠습니까?
        </p>
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onCancel}>취소</button>
          <button className="btn btn-danger" onClick={onConfirm}>삭제</button>
        </div>
      </div>
    </div>
  )
}

// ─── Main Component ──────────────────────────
export default function Dashboard() {
  const navigate = useNavigate()
  const { toast } = useToast()

  const [projects, setProjects]   = useState<Project[]>([])
  const [loading, setLoading]     = useState(true)
  const [newName, setNewName]     = useState('')
  const [creating, setCreating]   = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)

  const hasActiveRender = projects.some(p => ['QUEUED', 'PROCESSING'].includes(p.status))

  const load = useCallback(async () => {
    try {
      const data = await api.listProjects()
      setProjects(data)
    } catch (e: unknown) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [])

  // 초기 로드 + 5초 폴링 (렌더링 중일 때만)
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!hasActiveRender) return
    const timer = setInterval(load, 4000)
    return () => clearInterval(timer)
  }, [hasActiveRender, load])

  const handleCreate = async () => {
    const name = newName.trim() || '무제 프로젝트'
    setCreating(true)
    try {
      const p = await api.createProject(name)
      toast(`"${p.name}" 프로젝트 생성 완료`, 'success')
      navigate(`/project/${p.id}`)
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '생성 실패', 'error')
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (p: Project) => {
    try {
      await api.deleteProject(p.id)
      setProjects(prev => prev.filter(x => x.id !== p.id))
      toast('프로젝트 삭제됨', 'info')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '삭제 실패', 'error')
    } finally {
      setDeleteTarget(null)
    }
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '260px 1fr', minHeight: 'calc(100vh - 64px)' }}>
      {/* Sidebar */}
      <aside style={{ background: 'var(--bg-secondary)', borderRight: '1px solid var(--border)', padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem', position: 'sticky', top: '64px', height: 'calc(100vh - 64px)', overflowY: 'auto' }}>
        <button className="btn btn-primary btn-full" onClick={() => {
          const el = document.getElementById('new-project-input')
          if (el) el.focus()
        }}>
          ✨ 새 프로젝트 시작
        </button>

        <hr className="divider" />

        <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          <div style={{ marginBottom: '0.75rem', fontWeight: 600, color: 'var(--text-primary)' }}>📊 대시보드 통계</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.4rem' }}>
            <span>총 프로젝트</span>
            <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{projects.length}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.4rem' }}>
            <span>완료된 영상</span>
            <span style={{ fontWeight: 600, color: 'var(--success)' }}>{projects.filter(p => p.status === 'COMPLETED').length}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span>작업 중 (초안)</span>
            <span style={{ fontWeight: 600, color: 'var(--warning)' }}>{projects.filter(p => p.status === 'DRAFT').length}</span>
          </div>
        </div>

        <hr className="divider" />

        <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', lineHeight: 1.6 }}>
          <strong style={{ color: 'var(--text-secondary)' }}>DocuMotion Studio</strong><br />
          PDF 문서나 이미지를 업로드하고 대사를 입력하면 AI TTS와 함께 자동으로 영상을 제작해주는 문서 영상화 도구입니다.
        </div>
      </aside>

      {/* Main Content */}
      <main className="page" style={{ padding: '1.5rem' }}>
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '2rem', gap: '1rem', flexWrap: 'wrap' }}>
          <div>
            <h1 style={{ fontSize: '1.8rem', fontWeight: 700, marginBottom: '0.25rem' }}>📂 프로젝트</h1>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>{projects.length}개 프로젝트</p>
          </div>

          {/* New Project */}
          <div className="card" style={{ padding: '1rem 1.25rem', display: 'flex', gap: '0.6rem', alignItems: 'center', minWidth: 320 }}>
            <input
              id="new-project-input"
              className="input"
              placeholder="새 프로젝트 이름..."
              value={newName}
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleCreate()}
              style={{ flex: 1 }}
            />
            <button className="btn btn-primary" onClick={handleCreate} disabled={creating}>
              {creating ? <span className="spinner" /> : '✨ 생성'}
            </button>
          </div>
        </div>

        {/* Project List */}
        {loading ? (
          <div style={{ textAlign: 'center', padding: '4rem' }}>
            <span className="spinner spinner-lg" />
          </div>
        ) : projects.length === 0 ? (
          <div className="empty-state">
            <div className="icon">🎬</div>
            <h3>프로젝트가 없습니다</h3>
            <p>위에서 새 프로젝트를 생성해보세요</p>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
            {/* Table Header */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px 130px 120px auto', gap: '1rem', padding: '0.4rem 1.25rem', color: 'var(--text-muted)', fontSize: '0.75rem', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              <span>프로젝트명</span>
              <span>상태</span>
              <span>슬라이드</span>
              <span>수정일</span>
              <span>액션</span>
            </div>

            {projects.map(p => (
              <div key={p.id} className="card" style={{ padding: '1rem 1.25rem' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px 130px 120px auto', gap: '1rem', alignItems: 'center' }}>
                  {/* Name */}
                  <div>
                    <div style={{ fontWeight: 600, marginBottom: '0.2rem' }}>📁 {p.name}</div>
                    {stageBadge(p)}
                    {p.status === 'PROCESSING' && (
                      <div style={{ marginTop: '0.4rem' }}>
                        <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: '0.3rem' }}>
                          {p.message} ({p.progress}%)
                        </div>
                        <div className="progress-bar-wrap">
                          <div className="progress-bar-fill" style={{ width: `${p.progress}%` }} />
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Status */}
                  <div>{statusBadge(p)}</div>

                  {/* Slides count */}
                  <div style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>
                    {p.slide_count} 슬라이드
                  </div>

                  {/* Date */}
                  <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
                    {new Date(p.updated_at).toLocaleDateString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                  </div>

                  {/* Actions */}
                  <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
                    <button
                      className="btn btn-ghost btn-icon"
                      title="편집"
                      onClick={() => navigate(`/project/${p.id}`)}
                    >✏️</button>

                    {p.has_video && (
                      <button
                        className="btn btn-ghost btn-icon"
                        title={previewId === p.id ? '미리보기 닫기' : '영상 미리보기'}
                        onClick={() => setPreviewId(prev => prev === p.id ? null : p.id)}
                      >{previewId === p.id ? '🎬' : '▶️'}</button>
                    )}

                    {p.has_video && (
                      <a
                        className="btn btn-ghost btn-icon"
                        href={api.downloadUrl(p.id)}
                        download={`${p.name}.mp4`}
                        title="다운로드"
                      >💾</a>
                    )}

                    <button
                      className="btn btn-danger btn-icon"
                      title="삭제"
                      onClick={() => setDeleteTarget(p)}
                    >🗑️</button>
                  </div>
                </div>

                {/* Inline video preview */}
                {previewId === p.id && p.has_video && (
                  <div style={{ marginTop: '1rem', borderTop: '1px solid var(--border)', paddingTop: '1rem' }}>
                    <video
                      key={api.downloadUrl(p.id)}
                      controls
                      autoPlay
                      style={{ width: '100%', maxHeight: 360, borderRadius: 'var(--radius-md)', background: '#000' }}
                    >
                      <source src={api.downloadUrl(p.id)} type="video/mp4" />
                    </video>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Delete Confirm Modal */}
        {deleteTarget && (
          <ConfirmModal
            name={deleteTarget.name}
            onConfirm={() => handleDelete(deleteTarget)}
            onCancel={() => setDeleteTarget(null)}
          />
        )}
      </main>
    </div>
  )
}
