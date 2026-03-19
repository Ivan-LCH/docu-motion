import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Link } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Editor from './pages/Editor'
import { ToastProvider } from './components/ToastContext'

function ThemeToggle() {
  const [dark, setDark] = useState(() => {
    const saved = localStorage.getItem('theme')
    return saved ? saved === 'dark' : true
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])

  return (
    <button
      className="btn btn-ghost btn-icon"
      onClick={() => setDark(d => !d)}
      title={dark ? '라이트 모드' : '다크 모드'}
      style={{ fontSize: '1.2rem' }}
    >
      {dark ? '☀️' : '🌙'}
    </button>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <nav className="navbar">
          <Link to="/" className="navbar-brand">
            <span className="icon">🎬</span>
            DocuMotion Studio
            <span className="version">v5.0</span>
          </Link>
          <ThemeToggle />
        </nav>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/project/:id" element={<Editor />} />
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
