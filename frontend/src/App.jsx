import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api'
import { RequireAuth } from './auth'
import { notFoundMessage } from './lib/build'
import Home from './pages/Home'
import Reports from './pages/Reports'
import UploadReport from './pages/UploadReport'
import ReportDetail from './pages/ReportDetail'
import AccountDetail from './pages/AccountDetail'
import Cases from './pages/Cases'
import CaseDetail from './pages/CaseDetail'
import Activity from './pages/Activity'
import Profile from './pages/Profile'
import Operator from './pages/Operator'

const NAV = [
  { to: '/', icon: '⌂', label: 'Home', end: true },
  { to: '/reports', icon: '▤', label: 'Reports' },
  { to: '/cases', icon: '⚖', label: 'Cases', badge: true },
  { to: '/activity', icon: '◷', label: 'Activity' },
  { to: '/profile', icon: '◉', label: 'Profile' },
]

function Shell() {
  const location = useLocation()
  const [needsYou, setNeedsYou] = useState(0)

  useEffect(() => {
    window.scrollTo(0, 0)
    api.dashboard().then(d => setNeedsYou(d.cases.needs_user)).catch(() => {})
  }, [location.pathname])

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Main">
        <div className="brand">Credit Sniper<small>Credit intelligence</small></div>
        {NAV.map(item => (
          <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `side-link${isActive ? ' active' : ''}`}>
            <span aria-hidden>{item.icon}</span>{item.label}
            {item.badge && needsYou > 0 && <span className="dot">{needsYou}</span>}
          </NavLink>
        ))}
      </nav>

      <div className="main">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/reports/upload" element={<UploadReport />} />
          <Route path="/reports/:id" element={<ReportDetail />} />
          <Route path="/accounts/:id" element={<AccountDetail />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="/cases/:id" element={<CaseDetail />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/profile" element={<Profile />} />
          {/* Internal only: no nav entry, reachable by URL. */}
          <Route path="/operator" element={<Operator />} />
          {/* Without this, an unmatched path renders an empty content area —
              indistinguishable from a crash, and silent about the usual cause. */}
          <Route path="*" element={<NotFound />} />
        </Routes>
      </div>

      <nav className="tabbar" aria-label="Main">
        {NAV.map(item => (
          <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `tab${isActive ? ' active' : ''}`}>
            <span className="tab-icon" aria-hidden>{item.icon}</span>
            {item.label}
            {item.badge && needsYou > 0 && <span className="dot">{needsYou}</span>}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}

/**
 * An unmatched route.
 *
 * It names the path and the build on purpose. A path that should exist and
 * renders nothing almost always means the deployment serving the page is older
 * than the commit that added the route — which is invisible unless the bundle
 * says which commit it is.
 */
function NotFound() {
  const location = useLocation()
  const { title, path, stamp, hint } = notFoundMessage(location.pathname)
  return (
    <div className="content">
      <div className="card stack">
        <div className="card-title">{title}</div>
        <div className="small muted"><code>{path}</code> is not a route in this build.</div>
        <div className="small muted">Build: <code>{stamp}</code></div>
        <p className="tiny muted" style={{ margin: 0 }}>{hint}</p>
        <a className="btn btn-sm btn-primary" href="/">Home</a>
      </div>
    </div>
  )
}


export default function App() {
  return (
    <BrowserRouter>
      <RequireAuth>
        <Shell />
      </RequireAuth>
    </BrowserRouter>
  )
}
