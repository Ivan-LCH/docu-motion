import { BrowserRouter, Routes, Route, Link } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Editor from './pages/Editor'
import { ToastProvider } from './components/ToastContext'

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
        </nav>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/project/:id" element={<Editor />} />
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
