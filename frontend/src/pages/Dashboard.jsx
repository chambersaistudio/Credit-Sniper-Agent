import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

// ── Score gauge (SVG arc) ──────────────────────────────────────
function ScoreGauge({ score, bureau, size = 120 }) {
  const min = 300, max = 850
  const pct = score ? (score - min) / (max - min) : 0
  const r = 44, cx = 60, cy = 62
  const arc = 2 * Math.PI * r
  // 75% of the circle (270deg starting from bottom-left)
  const totalLen = arc * 0.75
  const fillLen = totalLen * Math.min(pct, 1)
  const gapLen = arc - totalLen

  const color = score >= 750 ? '#10b981'
    : score >= 700 ? '#22d3ee'
    : score >= 650 ? '#8b5cf6'
    : score >= 600 ? '#f59e0b'
    : '#f43f5e'

  const label = score >= 750 ? 'Excellent'
    : score >= 700 ? 'Good'
    : score >= 650 ? 'Fair'
    : score >= 600 ? 'Poor'
    : score ? 'Bad'
    : 'N/A'

  return (
    <div style={{ textAlign: 'center' }}>
      <svg width={size} height={size * 0.85} viewBox="0 0 120 105" style={{ overflow: 'visible' }}>
        {/* Track */}
        <circle
          cx={cx} cy={cy} r={r}
          fill="none"
          stroke="rgba(255,255,255,0.06)"
          strokeWidth="8"
          strokeDasharray={`${totalLen} ${gapLen}`}
          strokeDashoffset={arc * 0.375}
          strokeLinecap="round"
        />
        {/* Fill */}
        {score && (
          <circle
            cx={cx} cy={cy} r={r}
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeDasharray={`${fillLen} ${arc - fillLen}`}
            strokeDashoffset={arc * 0.375}
            strokeLinecap="round"
            style={{ filter: `drop-shadow(0 0 6px ${color}88)`, transition: 'stroke-dasharray 1s ease' }}
          />
        )}
        {/* Score text */}
        <text x={cx} y={cy - 4} textAnchor="middle" fill="white" fontSize="20" fontWeight="800" fontFamily="Inter, sans-serif">
          {score || '—'}
        </text>
        <text x={cx} y={cy + 14} textAnchor="middle" fill={color} fontSize="9.5" fontWeight="700" fontFamily="Inter, sans-serif" textTransform="uppercase" letterSpacing="0.5">
          {label}
        </text>
      </svg>
      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-3)', textTransform: 'capitalize', marginTop: 2 }}>
        {bureau}
      </div>
    </div>
  )
}

// ── Priority dots ──────────────────────────────────────────────
function PriorityDots({ level }) {
  const filled = Math.round(level / 2)
  const colorClass = level >= 8 ? 'active-high' : level >= 5 ? 'active-med' : 'active-low'
  return (
    <div className="priority-dots">
      {[1,2,3,4,5].map(i => (
        <div key={i} className={`priority-dot ${i <= filled ? colorClass : ''}`} />
      ))}
    </div>
  )
}

// ── Status badge ───────────────────────────────────────────────
const STATUS_CONFIG = {
  pending_approval: { cls: 'badge-amber', label: 'Needs Approval' },
  approved:         { cls: 'badge-violet', label: 'Approved' },
  submitted:        { cls: 'badge-sky', label: 'Submitted' },
  response_received:{ cls: 'badge-violet', label: 'Response In' },
  resolved:         { cls: 'badge-emerald', label: 'Resolved' },
  escalated:        { cls: 'badge-rose', label: 'Escalated' },
  frivolous_flagged:{ cls: 'badge-rose', label: 'Flagged' },
}

function StatusBadge({ status }) {
  const c = STATUS_CONFIG[status] || { cls: 'badge-muted', label: status }
  return <span className={`badge ${c.cls}`}>{c.label}</span>
}

// ── Main Dashboard ─────────────────────────────────────────────
export default function Dashboard() {
  const [disputes, setDisputes] = useState([])
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      api.listDisputes().catch(() => []),
      api.listReports().catch(() => []),
    ]).then(([d, r]) => {
      setDisputes(d || [])
      setReports(r || [])
    }).finally(() => setLoading(false))
  }, [])

  const pending    = disputes.filter(d => d.status === 'pending_approval')
  const active     = disputes.filter(d => ['submitted', 'approved', 'response_received'].includes(d.status))
  const resolved   = disputes.filter(d => d.status === 'resolved')
  const totalGain  = disputes.reduce((s, d) => s + (d.score_impact_estimate || 0), 0)

  // Latest scores from most recent reports per bureau
  const bureauScores = ['equifax', 'experian', 'transunion'].map(bureau => {
    const match = reports.find(r => r.bureau === bureau || r.bureau === 'tri_merge')
    return { bureau, score: match?.credit_score || null }
  })

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', gap: 12, color: 'var(--text-3)' }}>
        <div className="spinner" />
        Loading your credit command center...
      </div>
    )
  }

  const isEmpty = reports.length === 0 && disputes.length === 0

  return (
    <div style={{ padding: '36px 40px 60px' }}>

      {/* ── Header ── */}
      <div style={{ marginBottom: 36 }}>
        <h1 className="page-title">Command Center</h1>
        <p className="page-subtitle">Your autonomous credit dispute engine</p>
      </div>

      {isEmpty ? <EmptyState /> : (
        <>
          {/* ── Score gauges ── */}
          <div className="card card-glow-violet" style={{ marginBottom: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
              <div>
                <div className="section-title">Bureau Scores</div>
                <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
                  {reports.length > 0 ? `Last updated ${new Date(reports[0]?.pull_date).toLocaleDateString()}` : 'No reports uploaded yet'}
                </div>
              </div>
              <Link to="/upload">
                <button className="btn btn-secondary btn-sm">+ Upload New Report</button>
              </Link>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
              {bureauScores.map(b => (
                <div key={b.bureau} style={{
                  background: 'var(--bg-1)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 12,
                  padding: '24px 16px',
                  display: 'flex',
                  justifyContent: 'center',
                }}>
                  <ScoreGauge score={b.score} bureau={b.bureau} />
                </div>
              ))}
            </div>
          </div>

          {/* ── Stats row ── */}
          <div className="stat-grid stat-grid-4" style={{ marginBottom: 24 }}>
            <StatChip
              label="Pending Approval"
              value={pending.length}
              color="var(--amber)"
              delta={pending.length > 0 ? `${pending.length} need your review` : 'All clear'}
              deltaClass={pending.length > 0 ? 'delta-warn' : ''}
            />
            <StatChip
              label="Active Disputes"
              value={active.length}
              color="var(--sky)"
              delta={active.length > 0 ? 'In progress' : 'None active'}
            />
            <StatChip
              label="Resolved"
              value={resolved.length}
              color="var(--emerald)"
              delta={resolved.length > 0 ? 'Items removed ✓' : 'Fight ongoing'}
              deltaClass={resolved.length > 0 ? 'delta-up' : ''}
            />
            <StatChip
              label="Est. Score Gain"
              value={`+${totalGain}`}
              color="var(--violet-light)"
              delta="If all disputes win"
            />
          </div>

          {/* ── Two columns: pending + activity ── */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 20, marginBottom: 24 }}>

            {/* Pending approvals */}
            <div className="card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                <div className="section-title" style={{ marginBottom: 0 }}>
                  {pending.length > 0 ? `⚠️ Needs Your Approval (${pending.length})` : '📋 Active Disputes'}
                </div>
                <Link to="/disputes">
                  <button className="btn btn-ghost btn-sm">View all →</button>
                </Link>
              </div>

              {(pending.length > 0 ? pending : active).length === 0 ? (
                <div style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--text-4)' }}>
                  <div style={{ fontSize: 32, marginBottom: 10, opacity: 0.5 }}>📭</div>
                  <div style={{ fontSize: 13 }}>No disputes yet. Upload a report to start fighting.</div>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {(pending.length > 0 ? pending : active).slice(0, 4).map(d => (
                    <DisputeRow key={d.id} dispute={d} />
                  ))}
                </div>
              )}
            </div>

            {/* Right column: quick actions + mini activity */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {/* Quick actions */}
              <div className="card">
                <div className="section-title">Quick Actions</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <Link to="/upload" style={{ display: 'block' }}>
                    <button className="btn btn-primary btn-full">
                      📄 Upload Credit Report
                    </button>
                  </Link>
                  <Link to="/disputes" style={{ display: 'block' }}>
                    <button className="btn btn-secondary btn-full">
                      ⚔️ View Dispute Queue
                    </button>
                  </Link>
                  <Link to="/profile" style={{ display: 'block' }}>
                    <button className="btn btn-secondary btn-full">
                      👤 Update Profile
                    </button>
                  </Link>
                </div>
              </div>

              {/* System status */}
              <div className="card">
                <div className="section-title">System Status</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <SystemRow label="AI Analysis" status="live" />
                  <SystemRow label="Letter Generator" status="live" />
                  <SystemRow label="Dispute Tracker" status="live" />
                  <SystemRow label="Auto-Submit (Browser)" status="phase2" />
                  <SystemRow label="Email Monitoring" status="phase2" />
                  <SystemRow label="Live Credit Feed" status="phase3" />
                </div>
              </div>
            </div>
          </div>

          {/* ── All disputes table ── */}
          {disputes.length > 0 && (
            <div className="card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                <div className="section-title" style={{ marginBottom: 0 }}>All Disputes</div>
                <Link to="/disputes">
                  <button className="btn btn-ghost btn-sm">Full list →</button>
                </Link>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Creditor</th>
                    <th>Bureau</th>
                    <th>Priority</th>
                    <th>Round</th>
                    <th>Status</th>
                    <th>Score Impact</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {disputes.slice(0, 8).map(d => (
                    <tr key={d.id}>
                      <td style={{ color: 'var(--text-1)', fontWeight: 500 }}>{d.creditor_name || 'Unknown'}</td>
                      <td style={{ textTransform: 'capitalize' }}>{d.bureau}</td>
                      <td><PriorityDots level={d.priority || 5} /></td>
                      <td>R{d.current_round}</td>
                      <td><StatusBadge status={d.status} /></td>
                      <td style={{ color: 'var(--emerald)', fontWeight: 600 }}>+{d.score_impact_estimate || 0} pts</td>
                      <td>
                        <Link to={`/disputes/${d.id}`} style={{ color: 'var(--violet-light)', fontSize: 12 }}>View →</Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ── Sub-components ─────────────────────────────────────────────

function StatChip({ label, value, color, delta, deltaClass }) {
  return (
    <div className="stat-chip">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color }}>{value}</div>
      <div className={`stat-delta ${deltaClass || ''}`}>{delta}</div>
    </div>
  )
}

function DisputeRow({ dispute }) {
  return (
    <Link to={`/disputes/${dispute.id}`} style={{ textDecoration: 'none' }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '12px 14px',
        background: 'var(--bg-1)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 10,
        transition: 'border-color 0.15s',
        cursor: 'pointer',
      }}
        onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--border)'}
        onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border-subtle)'}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600, color: 'var(--text-1)', fontSize: 13 }}>
            {dispute.creditor_name || 'Unknown Creditor'}
          </div>
          <div style={{ color: 'var(--text-4)', fontSize: 11, marginTop: 2, textTransform: 'capitalize' }}>
            {dispute.bureau} · {dispute.strategy?.replace(/_/g, ' ')} · R{dispute.current_round}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
          <div style={{ textAlign: 'right' }}>
            <div style={{ color: 'var(--emerald)', fontSize: 12, fontWeight: 700 }}>+{dispute.score_impact_estimate || 0} pts</div>
          </div>
          <StatusBadge status={dispute.status} />
          <span style={{ color: 'var(--text-4)', fontSize: 16 }}>›</span>
        </div>
      </div>
    </Link>
  )
}

function SystemRow({ label, status }) {
  const config = {
    live:   { dot: 'var(--emerald)', text: 'Live', bg: 'rgba(16,185,129,0.1)', color: 'var(--emerald)' },
    phase2: { dot: '#374151', text: 'Phase 2', bg: 'var(--bg-3)', color: 'var(--text-4)' },
    phase3: { dot: '#1f2937', text: 'Phase 3', bg: 'var(--bg-3)', color: 'var(--text-4)' },
  }
  const c = config[status]
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%', background: c.dot, boxShadow: status === 'live' ? `0 0 6px var(--emerald)` : 'none' }} />
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{label}</span>
      </div>
      <span style={{ fontSize: 10, fontWeight: 700, color: c.color, background: c.bg, padding: '2px 8px', borderRadius: 99 }}>
        {c.text}
      </span>
    </div>
  )
}

function EmptyState() {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '60vh',
      textAlign: 'center',
    }}>
      {/* Hero graphic */}
      <div style={{
        width: 100, height: 100,
        background: 'linear-gradient(135deg, rgba(124,58,237,0.2), rgba(79,70,229,0.2))',
        border: '1px solid rgba(124,58,237,0.3)',
        borderRadius: 28,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 44,
        marginBottom: 28,
        boxShadow: '0 0 60px rgba(124,58,237,0.15)',
      }}>
        ⚡
      </div>

      <h2 style={{ fontSize: 28, fontWeight: 800, color: 'var(--text-0)', letterSpacing: -0.8, marginBottom: 12 }}>
        Ready to fight your credit
      </h2>
      <p style={{ fontSize: 15, color: 'var(--text-3)', maxWidth: 440, lineHeight: 1.7, marginBottom: 36 }}>
        Upload a credit report and the AI will scan every account for FCRA violations,
        Metro 2 errors, and disputable items — then draft the letters automatically.
      </p>

      <div style={{ display: 'flex', gap: 12, marginBottom: 48 }}>
        <Link to="/upload">
          <button className="btn btn-primary btn-lg">
            📄 Upload Credit Report
          </button>
        </Link>
        <Link to="/profile">
          <button className="btn btn-secondary btn-lg">
            👤 Set Up Profile First
          </button>
        </Link>
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, 1fr)',
        gap: 12,
        maxWidth: 600,
        width: '100%',
      }}>
        {[
          { icon: '🔍', title: 'AI Analysis', desc: 'Scans for FCRA violations & Metro 2 errors' },
          { icon: '✉️', title: 'Elite Letters', desc: 'Legally specific — not generic templates' },
          { icon: '📊', title: 'Track Everything', desc: '40-day timing rules enforced automatically' },
        ].map(f => (
          <div key={f.title} style={{
            background: 'var(--bg-2)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 12,
            padding: '18px',
            textAlign: 'center',
          }}>
            <div style={{ fontSize: 24, marginBottom: 8 }}>{f.icon}</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', marginBottom: 4 }}>{f.title}</div>
            <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5 }}>{f.desc}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
