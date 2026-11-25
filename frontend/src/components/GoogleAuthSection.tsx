import { useState, useEffect, useCallback } from 'react'
import { api, DriveConfig } from '../api/client'
import { useToast } from './ToastContext'

// ─── 코드 추출 유틸 ────────────────────────────
export function extractCodeFromUrl(input: string): string {
  try {
    const url = new URL(input.trim())
    const code = url.searchParams.get('code')
    if (code) return code
  } catch { /* not a URL — treat as raw code */ }
  return input.trim()
}

type AuthTarget = 'youtube' | 'photos' | 'drive'
const TARGET_LABEL: Record<AuthTarget, string> = { youtube: 'YouTube', photos: 'Google Photos', drive: 'Google Drive' }

// ─── Google 계정 인증 섹션 ──────────────────────
export default function GoogleAuthSection() {
  const { toast } = useToast()
  const [ytAuth, setYtAuth] = useState<boolean | null>(null)
  const [photosAuth, setPhotosAuth] = useState<boolean | null>(null)
  const [showCodeInput, setShowCodeInput] = useState(false)
  const [codeInput, setCodeInput] = useState('')
  const [drive, setDrive] = useState<DriveConfig | null>(null)
  const [codeTarget, setCodeTarget] = useState<AuthTarget>('youtube')
  const [exchanging, setExchanging] = useState(false)

  const checkStatus = useCallback(() => {
    api.youtubeAuthStatus().then(r => setYtAuth(r.authenticated)).catch(() => setYtAuth(false))
    api.photosAuthStatus().then(r => setPhotosAuth(r.authenticated)).catch(() => setPhotosAuth(false))
    api.driveConfig().then(setDrive).catch(() => setDrive({ enabled: false, client_id: '', api_key: '', app_id: '', authenticated: false, reason: '설정 확인 실패' }))
  }, [])

  useEffect(() => { checkStatus() }, [checkStatus])

  const handleAuth = async (target: AuthTarget) => {
    try {
      setCodeTarget(target)
      const { auth_url } = target === 'youtube'
        ? await api.youtubeAuthUrl()
        : target === 'photos' ? await api.photosAuthUrl() : await api.driveAuthUrl()
      window.open(auth_url, '_blank')
      setShowCodeInput(true)
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
      if (codeTarget === 'youtube') {
        await api.youtubeExchangeCode(code)
      } else if (codeTarget === 'photos') {
        await api.photosExchangeCode(code)
      } else {
        await api.driveExchangeCode(code)
      }
      toast(`${TARGET_LABEL[codeTarget]} 인증 완료!`, 'success')
      setShowCodeInput(false)
      setCodeInput('')
      checkStatus()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '코드 교환 실패', 'error')
    } finally {
      setExchanging(false)
    }
  }

  const allOk = ytAuth === true && photosAuth === true

  return (
    <div>
      <label className="label">Google 계정 연동</label>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem', fontSize: '0.78rem', marginBottom: '0.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          <span style={{ color: ytAuth === true ? 'var(--success)' : ytAuth === false ? 'var(--error)' : 'var(--text-muted)' }}>
            {ytAuth === null ? '...' : ytAuth ? '●' : '○'}
          </span>
          <span style={{ color: 'var(--text-secondary)' }}>YouTube</span>
          {ytAuth === false && (
            <button className="btn btn-ghost" style={{ fontSize: '0.7rem', padding: '0 0.3rem' }} onClick={() => handleAuth('youtube')}>인증</button>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          <span style={{ color: photosAuth === true ? 'var(--success)' : photosAuth === false ? 'var(--error)' : 'var(--text-muted)' }}>
            {photosAuth === null ? '...' : photosAuth ? '●' : '○'}
          </span>
          <span style={{ color: 'var(--text-secondary)' }}>Google Photos</span>
          {photosAuth === false && (
            <button className="btn btn-ghost" style={{ fontSize: '0.7rem', padding: '0 0.3rem' }} onClick={() => handleAuth('photos')}>인증</button>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', opacity: drive && !drive.enabled ? 0.5 : 1 }}
          title={drive && !drive.enabled ? drive.reason : undefined}>
          <span style={{ color: !drive || !drive.enabled ? 'var(--text-muted)' : drive.authenticated ? 'var(--success)' : 'var(--error)' }}>
            {drive === null ? '...' : drive.enabled && drive.authenticated ? '●' : '○'}
          </span>
          <span style={{ color: 'var(--text-secondary)' }}>Google Drive</span>
          {drive && !drive.enabled && <span style={{ color: 'var(--text-muted)' }}>(관리자 설정 필요)</span>}
          {drive && drive.enabled && !drive.authenticated && (
            <button className="btn btn-ghost" style={{ fontSize: '0.7rem', padding: '0 0.3rem' }} onClick={() => handleAuth('drive')}>인증</button>
          )}
        </div>
      </div>

      {/* 코드 입력 UI */}
      {showCodeInput && (
        <div style={{ marginBottom: '0.5rem', padding: '0.5rem', background: 'var(--bg-secondary)', borderRadius: 'var(--radius-sm)', fontSize: '0.78rem' }}>
          <p style={{ color: 'var(--text-secondary)', marginBottom: '0.4rem', lineHeight: 1.5 }}>
            새 탭에서 Google 로그인 후,<br />
            주소창의 URL을 복사해서 붙여넣기하세요.
          </p>
          <input
            className="input"
            style={{ fontSize: '0.75rem', marginBottom: '0.4rem' }}
            placeholder="http://localhost?code=4/0A... 전체 URL 붙여넣기"
            value={codeInput}
            onChange={e => setCodeInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleExchangeCode()}
          />
          <div style={{ display: 'flex', gap: '0.3rem' }}>
            <button className="btn btn-primary btn-sm" style={{ flex: 1 }} onClick={handleExchangeCode} disabled={exchanging}>
              {exchanging ? '처리 중...' : '인증 완료'}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => setShowCodeInput(false)}>취소</button>
          </div>
        </div>
      )}

      {!showCodeInput && !allOk && (
        <button className="btn btn-primary btn-sm btn-full" onClick={() => handleAuth(ytAuth === false ? 'youtube' : 'photos')}>
          Google 계정 인증
        </button>
      )}
      {!showCodeInput && allOk && (
        <button className="btn btn-ghost btn-sm btn-full" onClick={() => handleAuth('youtube')} style={{ fontSize: '0.75rem' }}>
          재인증
        </button>
      )}
    </div>
  )
}
