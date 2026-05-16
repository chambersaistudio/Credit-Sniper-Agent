import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [disputes, setDisputes] = useState([])
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      api.getStats().catch(() => null),
      api.listDisputes().catch(() => []),
      api.listReports().catch(() => []),
    ]).then(([s, d, r]) => {
      setStats(s)
      setDisputes(d || [])
      setReports(r || [])
    }).finally(() => setLoading(false))
  }, [])

  const pending = disputes.filter(d => d.status === 'pending_approval')
  const active = disputes.filter(d => ['submitted', 'approved'].includes(d.status))
  const resolved = disputes.filter(d => d.status === 'resolved')
  const totalScoreGain = disputes.reduce((sum, d) => sum + (d.score_impact_estimate || 0), 0)

  if (loading) return (
    <div style={{ padding: 40, textAlign: 'center' }}>
      <div className="spinner" style={{ width: 32, height: 32, margin: '0 auto 16px' }} />
      <div style={{ color: '#64748b' }}>Loading dashboard...</div>
    </div>
  )

  return (
    <div style={{ padding: 32 }}>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700 }}>Dashboard</h1>
        <p style={{ color: '#64748b', marginTop: 4 }}>Your autonomous credit dispute command center</p>
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 32 }}>
        <div className="stat-card">
          <div className="value">{reports.length}</div>
          <div className="label">Reports Analyzed</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ color: '#f59e0b' }}>{pending.length}</div>
          <div className="label">Awaiting Approval</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ color: '#3b82f6' }}>{active.length}</div>
          <div className="label">Active Disputes</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ color: '#10b981' }}>{resolved.length}</div>
          <div className="label">Items Resolved</div>
        </div>
      </div>

      {/* Quick Actions */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, marginBottom: 32 }}>
        <div className="card">
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>Quick Actions</h2>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <Link to="/upload">
              <button className="btn-primary" style={{ width: '100%', padding: '12px 16px', textAlign: 'left' }}>
                📄 Upload Credit Report for Analysis
              </button>
            </Link>
            <Link to="/disputes">
              <button className="btn-secondary" style={{ width: '100%', padding: '12px 16px', textAlign: 'left' }}>
                📋 View All Disputes
              </button>
            </Link>
            <Link to="/profile">
              <button className="btn-secondary" style={{ width: '100%', padding: '12px 16px', textAlign: 'left' }}>
                👤 Set Up Profile (for Letters)
              </button>
            </Link>
          </div>
        </div>

        <div className="card">
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>System Status</h2>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <StatusRow label="AI Analysis Engine" status="active" />
            <StatusRow label="Letter Generator" status="active" />
            <StatusRow label="Dispute Tracker" status="active" />
            <StatusRow label="Browser Automation" status="phase2" />
            <StatusRow label="Email Monitoring" status="phase2" />
            <StatusRow label="Real-time Credit Data" status="phase3" />
          </div>
        </div>
      </div>

      {/* Pending Approvals */}
      {pending.length > 0 && (
        <div className="card" style={{ marginBottom: 20, borderColor: 'rgba(245,158,11,0.4)' }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: '#f59e0b' }}>
            ⚠️ {pending.length} Dispute{pending.length > 1 ? 's' : ''} Awaiting Your Approval
          </h2>
          <table>
            <thead>
              <tr>
                <th>Creditor</th>
                <th>Bureau</th>
                <th>Priority</th>
                <th>Est. Score Impact</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {pending.map(d => (
                <tr key={d.id}>
                  <td style={{ fontWeight: 500 }}>{d.creditor_name || 'Unknown'}</td>
                  <td style={{ textTransform: 'capitalize' }}>{d.bureau}</td>
                  <td><PriorityBadge priority={d.priority} /></td>
                  <td style={{ color: '#10b981' }}>+{d.score_impact_estimate || '?'} pts</td>
                  <td>
                    <Link to={`/disputes/${d.id}`}>
                      <button className="btn-primary" style={{ padding: '4px 12px', fontSize: 12 }}>
                        Review & Approve →
                      </button>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Recent Reports */}
      {reports.length > 0 && (
        <div className="card">
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>Recent Credit Reports</h2>
          <table>
            <thead>
              <tr>
                <th>Bureau</th>
                <th>Score</th>
                <th>Source</th>
                <th>Date</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {reports.slice(0, 5).map(r => (
                <tr key={r.id}>
                  <td style={{ textTransform: 'capitalize', fontWeight: 500 }}>{r.bureau}</td>
                  <td style={{ color: scoreColor(r.credit_score) }}>{r.credit_score || '—'}</td>
                  <td style={{ color: '#64748b' }}>{r.source}</td>
                  <td style={{ color: '#64748b' }}>{new Date(r.pull_date).toLocaleDateString()}</td>
                  <td>
                    <Link to={`/reports/${r.id}`} style={{ fontSize: 12, color: '#6366f1' }}>View →</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {reports.length === 0 && disputes.length === 0 && (
        <div className="empty-state">
          <div style={{ fontSize: 48, marginBottom: 16 }}>🎯</div>
          <h3>Ready to Fight Your Credit</h3>
          <p style={{ marginBottom: 20, color: '#64748b' }}>
            Start by uploading a credit report. The AI will analyze it and identify every disputable item.
          </p>
          <Link to="/upload">
            <button className="btn-primary" style={{ padding: '12px 24px' }}>
              Upload Credit Report →
            </button>
          </Link>
        </div>
      )}
    </div>
  )
}

function StatusRow({ label, status }) {
  const config = {
    active: { color: '#10b981', text: '● Active', bg: 'rgba(16,185,129,0.1)' },
    phase2: { color: '#f59e0b', text: '○ Phase 2', bg: 'rgba(245,158,11,0.1)' },
    phase3: { color: '#64748b', text: '○ Phase 3', bg: 'rgba(100,116,139,0.1)' },
  }
  const c = config[status] || config.phase2
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <span style={{ color: '#94a3b8', fontSize: 13 }}>{label}</span>
      <span style={{
        fontSize: 11,
        color: c.color,
        background: c.bg,
        padding: '2px 8px',
        borderRadius: 4,
        fontWeight: 600,
      }}>{c.text}</span>
    </div>
  )
}

function PriorityBadge({ priority }) {
  const p = priority || 5
  const color = p >= 8 ? '#ef4444' : p >= 5 ? '#f59e0b' : '#10b981'
  return (
    <span style={{ color, fontWeight: 600, fontSize: 13 }}>
      {p >= 8 ? '🔴' : p >= 5 ? '🟡' : '🟢'} {p}/10
    </span>
  )
}

function scoreColor(score) {
  if (!score) return '#64748b'
  if (score >= 750) return '#10b981'
  if (score >= 700) return '#6366f1'
  if (score >= 650) return '#f59e0b'
  return '#ef4444'
}
