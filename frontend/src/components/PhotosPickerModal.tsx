import { useState, useEffect, useRef } from 'react'
import { api, PhotosSortOrder, importSummary } from '../api/client'
import { useToast } from './ToastContext'
import { extractCodeFromUrl } from './GoogleAuthSection'

// ─── Google Photos Picker Modal ───────────────
export default function PhotosPickerModal({ projectId, onClose, onImported }: {
  projectId: string; onClose: () => void; onImported: () => void
}) {
  const { toast } = useToast()
  const [authOk, setAuthOk] = useState<boolean | null>(null)
  const [phase, setPhase] = useState<'idle' | 'code_input' | 'picking' | 'importing' | 'done'>('idle')
  const [sessionId, setSessionId] = useState<string>('')
  const [codeInput, setCodeInput] = useState('')
  const [exchanging, setExchanging] = useState(false)
  const [sortOrder, setSortOrder] = useState<PhotosSortOrder>('selected')
  const sortOrderRef = useRef<PhotosSortOrder>('selected')
  useEffect(() => { sortOrderRef.current = sortOrder }, [sortOrder])
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const importingRef = useRef(false)

  // 인증 확인
  useEffect(() => {
    api.photosAuthStatus().then(res => setAuthOk(res.authenticated)).catch(() => setAuthOk(false))
  }, [])

  // 폴링 정리
  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const handleAuth = async () => {
    try {
      const { auth_url } = await api.photosAuthUrl()
      window.open(auth_url, '_blank')
      setPhase('code_input')
      setCodeInput('')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '인증 URL 생성 실패', 'error')
    }
  }

  const handleExchangeCode = async () => {
    const code = extractCodeFromUrl(codeInput)
    if (!code) { toast('인증 코드를 입력해주세요', 'error'); return }
    setExchanging(true)
    try {
      await api.photosExchangeCode(code)
      toast('Google Photos 인증 완료!', 'success')
      setAuthOk(true)
      setPhase('idle')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '코드 교환 실패', 'error')
    } finally {
      setExchanging(false)
    }
  }

  const handleOpenPicker = async () => {
    try {
      // 1. Picker 세션 생성
      const session = await api.photosCreateSession()
      setSessionId(session.id)
      setPhase('picking')

      // 2. Picker UI 새 탭에서 열기
      window.open(session.pickerUri, '_blank')

      // 3. 세션 폴링 — 사용자 선택 완료 대기
      pollRef.current = setInterval(async () => {
        try {
          const status = await api.photosPollSession(session.id)
          console.log('[Photos] poll:', status)
          if (status.mediaItemsSet) {
            if (pollRef.current) clearInterval(pollRef.current)
            pollRef.current = null
            await doImport(session.id)
          }
        } catch (err) {
          console.error('[Photos] poll error:', err)
        }
      }, 3000)

    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Picker 세션 생성 실패', 'error')
      setPhase('idle')
    }
  }

  // 수동으로 가져오기 실행 (폴링이 자동 감지 못할 때 대비)
  const handleManualImport = async () => {
    if (!sessionId) return
    // 폴링 정리
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    await doImport(sessionId)
  }

  const doImport = async (sid: string) => {
    if (importingRef.current) return
    importingRef.current = true
    setPhase('importing')
    try {
      const res = await api.photosImport(projectId, sid, sortOrderRef.current)
      toast(importSummary(res), res.count ? (res.failed ? 'info' : 'success') : 'error', { duration: res.failed ? 6000 : 3500 })
      setPhase('done')
      onImported()
      onClose()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '가져오기 실패', 'error')
      importingRef.current = false
      setPhase('idle')
    }
  }

  return (
    <div className="modal-overlay" onClick={phase === 'idle' || phase === 'done' ? onClose : undefined}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: '500px' }}>
        <div className="modal-header">
          <span className="modal-title">Google 포토에서 사진·영상 가져오기</span>
          <button className="btn btn-ghost btn-icon" onClick={onClose} disabled={phase === 'importing'}>✕</button>
        </div>

        {/* 인증 확인 중 */}
        {authOk === null && (
          <div style={{ textAlign: 'center', padding: '2rem' }}>
            <span className="spinner" /> 인증 확인 중...
          </div>
        )}

        {/* 인증 안됨 */}
        {authOk === false && phase !== 'code_input' && (
          <div style={{ textAlign: 'center', padding: '2rem' }}>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '1rem', fontSize: '0.9rem' }}>
              Google Photos 접근 권한이 필요합니다
            </p>
            <button className="btn btn-primary" onClick={handleAuth}>Google 계정 인증</button>
          </div>
        )}

        {/* 코드 입력 */}
        {phase === 'code_input' && (
          <div style={{ padding: '1.5rem' }}>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: '0.8rem', lineHeight: 1.6 }}>
              새 탭에서 Google 로그인 후,<br />
              주소창의 URL을 복사해서 아래에 붙여넣기하세요.
            </p>
            <input
              className="input"
              style={{ fontSize: '0.85rem', marginBottom: '0.6rem' }}
              placeholder="http://localhost?code=4/0A... 전체 URL"
              value={codeInput}
              onChange={e => setCodeInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleExchangeCode()}
              autoFocus
            />
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button className="btn btn-primary" style={{ flex: 1 }} onClick={handleExchangeCode} disabled={exchanging}>
                {exchanging ? '처리 중...' : '인증 완료'}
              </button>
              <button className="btn btn-secondary" onClick={() => setPhase('idle')}>취소</button>
            </div>
          </div>
        )}

        {/* 인증됨 */}
        {authOk === true && (
          <div style={{ textAlign: 'center', padding: '1.5rem 1rem' }}>
            {phase === 'idle' && (
              <>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', marginBottom: '1.2rem', textAlign: 'left', maxWidth: 320, marginLeft: 'auto', marginRight: 'auto' }}>
                  <label style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontWeight: 600 }}>정렬 순서</label>
                  <select
                    value={sortOrder}
                    onChange={e => setSortOrder(e.target.value as PhotosSortOrder)}
                    style={{ fontSize: '0.85rem', padding: '0.5rem 0.7rem', background: 'var(--bg-card)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)' }}
                  >
                    <option value="selected">선택 순서 (Picker 클릭 순)</option>
                    <option value="oldest">촬영 오래된 순</option>
                    <option value="newest">촬영 최신 순</option>
                    <option value="api">기본 (API 응답 순서)</option>
                  </select>
                </div>
                <button className="btn btn-primary" onClick={handleOpenPicker} style={{ fontSize: '1rem', padding: '0.7rem 2rem' }}>
                  사진·영상 고르기
                </button>
              </>
            )}

            {phase === 'picking' && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '1rem' }}>
                <span className="spinner spinner-lg" />
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                  새 창에서 사진·영상을 골라주세요
                </p>
                <button className="btn btn-primary" onClick={handleManualImport} style={{ marginTop: '0.5rem' }}>
                  선택 완료 — 가져오기
                </button>
              </div>
            )}

            {phase === 'importing' && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '1rem' }}>
                <span className="spinner spinner-lg" />
                <p style={{ color: 'var(--accent)', fontSize: '0.9rem', fontWeight: 600 }}>
                  가져오는 중... (영상은 조금 걸려요)
                </p>
              </div>
            )}
          </div>
        )}

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose} disabled={phase === 'importing'}>
            {phase === 'idle' ? '닫기' : '취소'}
          </button>
        </div>
      </div>
    </div>
  )
}
