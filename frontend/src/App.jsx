import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import UploadReport from './pages/UploadReport'
import ReportDetail from './pages/ReportDetail'
import Disputes from './pages/Disputes'
import DisputeDetail from './pages/DisputeDetail'
import UserProfile from './pages/UserProfile'

const NAV = [
  { to: '/', icon: '⚡', label: 'Command Center', exact: true },
  { to: '/upload', icon: '📄', label: 'Upload Report' },
  { to: '/disputes', icon: '⚔️', label: 'Disputes' },
  { to: '/profile', icon: '👤', label: 'My Profile' },
]

export default function App() {
  return (
    <BrowserRouter>
      <div className="layout">
        <nav className="sidebar">
          <div className="sidebar-logo">
            <div className="logo-mark">⚡</div>
            <div className="logo-name">Credit Sniper</div>
            <div className="logo-tagline">Autonomous Dispute Agent</div>
          </div>

          <div className="sidebar-nav">
            <div className="nav-section-label">Navigation</div>
            {NAV.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.exact}
                className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              >
                <span className="nav-icon">{item.icon}</span>
                {item.label}
              </NavLink>
            ))}

            <div className="nav-section-label" style={{ marginTop: 12 }}>Roadmap</div>
            <div className="nav-item" style={{ opacity: 0.4, cursor: 'default', pointerEvents: 'none' }}>
              <span className="nav-icon">🤖</span>
              Auto-Submit
              <span style={{ marginLeft: 'auto', fontSize: 10, color: '#475569' }}>Phase 2</span>
            </div>
            <div className="nav-item" style={{ opacity: 0.4, cursor: 'default', pointerEvents: 'none' }}>
              <span className="nav-icon">📡</span>
              Live Monitoring
              <span style={{ marginLeft: 'auto', fontSize: 10, color: '#475569' }}>Phase 3</span>
            </div>
          </div>

          <div className="sidebar-footer">
            <div className="phase-pill">⚡ Phase 1 Active</div>
            <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 8, lineHeight: 1.5 }}>
              Analysis · Letters · Tracking
            </div>
          </div>
        </nav>

        <main className="main-content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/upload" element={<UploadReport />} />
            <Route path="/reports/:id" element={<ReportDetail />} />
            <Route path="/disputes" element={<Disputes />} />
            <Route path="/disputes/:id" element={<DisputeDetail />} />
            <Route path="/profile" element={<UserProfile />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
