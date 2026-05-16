import { useState } from 'react'
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import UploadReport from './pages/UploadReport'
import ReportDetail from './pages/ReportDetail'
import Disputes from './pages/Disputes'
import DisputeDetail from './pages/DisputeDetail'
import UserProfile from './pages/UserProfile'

const NavItem = ({ to, children }) => (
  <NavLink
    to={to}
    style={({ isActive }) => ({
      display: 'block',
      padding: '8px 16px',
      borderRadius: '6px',
      color: isActive ? '#6366f1' : '#94a3b8',
      background: isActive ? 'rgba(99,102,241,0.1)' : 'transparent',
      fontWeight: isActive ? 600 : 400,
      transition: 'all 0.15s',
      fontSize: '14px',
    })}
  >
    {children}
  </NavLink>
)

export default function App() {
  return (
    <BrowserRouter>
      <div style={{ display: 'flex', minHeight: '100vh' }}>
        {/* Sidebar */}
        <nav style={{
          width: 220,
          background: '#111118',
          borderRight: '1px solid #2a2a3a',
          padding: '24px 12px',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          flexShrink: 0,
        }}>
          <div style={{ padding: '0 4px 20px', borderBottom: '1px solid #2a2a3a', marginBottom: 12 }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#6366f1' }}>⚡ Credit Sniper</div>
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>Autonomous Dispute Agent</div>
          </div>

          <NavItem to="/">Dashboard</NavItem>
          <NavItem to="/upload">Upload Report</NavItem>
          <NavItem to="/disputes">Disputes</NavItem>
          <NavItem to="/profile">My Profile</NavItem>

          <div style={{ marginTop: 'auto', padding: '16px 4px 0', borderTop: '1px solid #2a2a3a' }}>
            <div style={{ fontSize: 11, color: '#64748b' }}>Phase 1 MVP</div>
            <div style={{ fontSize: 10, color: '#475569', marginTop: 2 }}>Analysis + Letter Gen</div>
          </div>
        </nav>

        {/* Main content */}
        <main style={{ flex: 1, overflow: 'auto' }}>
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
