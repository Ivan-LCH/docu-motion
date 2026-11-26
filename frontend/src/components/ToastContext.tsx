import { createContext, useContext, useState, useCallback, ReactNode } from 'react'

interface ToastAction { label: string; onClick: () => void }
interface ToastOpts { action?: ToastAction; duration?: number }
interface Toast { id: number; type: 'success' | 'error' | 'info'; message: string; action?: ToastAction }

interface ToastCtx { toast: (msg: string, type?: Toast['type'], opts?: ToastOpts) => void }
const ToastContext = createContext<ToastCtx>({ toast: () => {} })

let nextId = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => setToasts(t => t.filter(x => x.id !== id)), [])

  const toast = useCallback((message: string, type: Toast['type'] = 'info', opts?: ToastOpts) => {
    const id = ++nextId
    setToasts(t => [...t, { id, type, message, action: opts?.action }])
    setTimeout(() => dismiss(id), opts?.duration ?? 3500)
  }, [dismiss])

  const icons = { success: '✅', error: '❌', info: 'ℹ️' }

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="toast-container">
        {toasts.map(t => (
          <div key={t.id} className={`toast toast-${t.type}${t.action ? ' toast-with-action' : ''}`}>
            <span>{icons[t.type]}</span>
            {t.message}
            {t.action && (
              <button className="toast-action" onClick={() => { t.action!.onClick(); dismiss(t.id) }}>
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export const useToast = () => useContext(ToastContext)
