import { useState, useEffect, useCallback } from 'react'
import { api, DriveConfig, DriveSortOrder } from '../api/client'
import { useToast } from './ToastContext'
import { extractCodeFromUrl } from './GoogleAuthSection'

// ─── Google Picker 최소 타입 (npm 타입 패키지 없이 사용) ──────────
/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    gapi?: any
    google?: any
  }
}

// Drive Picker 에서 고를 수 있는 형식 (사진 + 영상)
const PICKER_MIME_TYPES = 'image/jpeg,image/png,image/heic,image/heif,image/webp,video/mp4,video/quicktime,video/webm,video/x-matroska'

// api.js 는 한 번만 로드 (promise 캐시)
let gapiPromise: Promise<void> | null = null
function loadGapi(): Promise<void> {
  if (window.gapi?.load) return Promise.resolve()
  if (gapiPromise) return gapiPromise
  gapiPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = 'https://apis.google.com/js/api.js'
    script.async = true
    script.onload = () => resolve()
    script.onerror = () => { gapiPromise = null; reject(new Error('Google API 스크립트 로드 실패')) }
    document.head.appendChild(script)
  })
  return gapiPromise
}

// picker 모듈 로드 (gapi.load 는 콜백 방식)
let pickerPromise: Promise<void> | null = null
function loadPicker(): Promise<void> {
  if (window.google?.picker) return Promise.resolve()
  if (pickerPromise) return pickerPromise
  pickerPromise = loadGapi().then(() => new Promise<void>((resolve, reject) => {
    window.gapi.load('picker', {
      callback: () => resolve(),
      onerror: () => { pickerPromise = null; reject(new Error('Google Picker 로드 실패')) },
    })
  }))
  return pickerPromise
}

const SKIP_REASON_LABEL: Record<string, string> = {
  unsupported: '지원하지 않는 형식',
  too_large: '파일이 너무 큼',
  download_failed: '다운로드 실패',
}

// ─── Google Drive 가져오기 버튼 ────────────────────────────
export default function DrivePickerButton({ projectId, onImported, sortOrder = 'captured', compact = false }: {
  projectId: string
  onImported: () => void
  sortOrder?: DriveSortOrder
  compact?: boolean   // true: 버튼만 (카드 레이아웃은 부모가 담당)
}) {
  const { toast } = useToast()
  const [config, setConfig] = useState<DriveConfig | null>(null)
  const [busy, setBusy] = useState<'' | 'opening' | 'importing'>('')
  const [showCode, setShowCode] = useState(false)
  const [codeInput, setCodeInput] = useState('')
  const [exchanging, setExchanging] = useState(false)

  const loadConfig = useCallback(() => {
    api.driveConfig().then(setConfig).catch((e: unknown) => setConfig({
      enabled: false, client_id: '', api_key: '', app_id: '', authenticated: false,
      reason: e instanceof Error ? e.message : '설정 확인 실패',
    }))
  }, [])
  useEffect(() => { loadConfig() }, [loadConfig])

  const handleAuth = async () => {
    try {
      const { auth_url } = await api.driveAuthUrl()
      window.open(auth_url, '_blank')
      setShowCode(true)
      setCodeInput('')
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '인증 URL 생성 실패', 'error')
    }
  }

  const handleExchange = async () => {
    const code = extractCodeFromUrl(codeInput)
    if (!code) { toast('인증 코드를 입력해주세요', 'error'); return }
    setExchanging(true)
    try {
      await api.driveExchangeCode(code)
      toast('Google Drive 인증 완료!', 'success')
      setShowCode(false)
      setCodeInput('')
      loadConfig()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '코드 교환 실패', 'error')
    } finally {
      setExchanging(false)
    }
  }

  const doImport = async (ids: string[]) => {
    setBusy('importing')
    try {
      const res = await api.driveImport(projectId, ids, sortOrder)
      const skipped = res.skipped || []
      if (skipped.length) {
        const reasons = skipped.slice(0, 3).map(s => `${s.name} (${SKIP_REASON_LABEL[s.reason] || s.reason})`).join(', ')
        toast(`${res.count}개 가져옴 · ${skipped.length}개 건너뜀: ${reasons}${skipped.length > 3 ? ' 외' : ''}`, res.count ? 'info' : 'error')
      } else {
        toast(`${res.count}개 가져오기 완료!`, 'success')
      }
      onImported()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '드라이브 가져오기 실패', 'error')
    } finally {
      setBusy('')
    }
  }

  const openPicker = async () => {
    if (!config || busy) return
    setBusy('opening')
    try {
      const [{ access_token }] = await Promise.all([api.driveToken(), loadPicker()])
      const gp = window.google.picker
      const view = new gp.DocsView(gp.ViewId.DOCS)
        .setMimeTypes(PICKER_MIME_TYPES)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(false)
      const picker = new gp.PickerBuilder()
        .addView(view)
        .enableFeature(gp.Feature.MULTISELECT_ENABLED)
        .setOAuthToken(access_token)
        .setDeveloperKey(config.api_key)
        .setAppId(config.app_id)
        .setTitle('사진·영상 고르기')
        .setCallback((data: any) => {
          const action = data?.[gp.Response.ACTION] ?? data?.action
          if (action === gp.Action.PICKED) {
            const docs: any[] = data?.[gp.Response.DOCUMENTS] ?? data?.docs ?? []
            const ids = docs.map(d => d?.[gp.Document.ID] ?? d?.id).filter((x: unknown): x is string => typeof x === 'string')
            if (ids.length) doImport(ids)
            else setBusy('')
          } else if (action === gp.Action.CANCEL) {
            setBusy('')
          }
        })
        .build()
      picker.setVisible(true)
    } catch (e: unknown) {
      const status = (e as { status?: number })?.status
      if (status === 401) {
        toast('Google Drive 인증이 필요해요', 'error')
        loadConfig()
      } else {
        toast(e instanceof Error ? e.message : '드라이브 Picker 열기 실패', 'error')
      }
      setBusy('')
    }
  }

  // ── 렌더 ──
  if (config === null) {
    return <button className="btn btn-ghost btn-sm" disabled><span className="spinner" /></button>
  }

  if (!config.enabled) {
    return (
      <button className="btn btn-ghost btn-sm" disabled title={config.reason}
        style={{ whiteSpace: 'normal', textAlign: 'left', maxWidth: compact ? 220 : undefined }}>
        관리자 설정 필요: {config.reason}
      </button>
    )
  }

  if (!config.authenticated) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', alignItems: 'stretch', minWidth: showCode ? 260 : undefined }}>
        {!showCode ? (
          <button className="btn btn-secondary btn-sm" onClick={handleAuth}>인증</button>
        ) : (
          <div style={{ padding: '0.5rem', background: 'var(--bg-secondary)', borderRadius: 'var(--radius-sm)', fontSize: '0.78rem' }}>
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
              onKeyDown={e => e.key === 'Enter' && handleExchange()}
              autoFocus
            />
            <div style={{ display: 'flex', gap: '0.3rem' }}>
              <button className="btn btn-primary btn-sm" style={{ flex: 1 }} onClick={handleExchange} disabled={exchanging}>
                {exchanging ? '처리 중...' : '인증 완료'}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setShowCode(false)}>취소</button>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <button className="btn btn-secondary btn-sm" onClick={openPicker} disabled={!!busy}>
      {busy === 'importing' ? <><span className="spinner" /> 가져오는 중...</> : busy === 'opening' ? <><span className="spinner" /> 여는 중...</> : '선택'}
    </button>
  )
}
