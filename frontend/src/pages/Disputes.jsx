import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

const STATUS_LABELS = {
  pending_approval: { label: 'Pending Approval', cls: 'badge-pending' },
  approved: { label: 'Approved', cls: 'badge-approved' },
  submitted: { label: 'Submitted', cls: 'badge-submitted' },
  response_received: { label: 'Response Received', cls: 'badge-approved' },
  resolved: { label: 'Resolved ✓', cls: 'badge-resolved' },
  escalated: { label: 'Escalated', cls: 'badge-escalated' },
  frivolous_flagged: { label: 'Flagged Frivolous', cls: 'badge-escalated' },
}

export default function Disputes() {
  const [disputes, setDisputes] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')

  const load = () => {
    api.listDisputes()
      .then(setDisputes)
      .catch(console.error)
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  const filtered = filter === 'all' ? disputes : disputes.filter(d => d.status === filter)

  const counts = disputes.reduce((acc, d) => {
    acc[d.status] = (acc[d.status] || 0) + 1
    return acc
  }, {})

  if (loading) return <div style={{ padding: 40, textAlign: 'center' }}><div className="spinner" style={{ width: 32, height: 32, margin: '0 auto' }} /></div>

  return (
    <div style={{ padding: 32 }}>
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700 }}>Disputes</h1>
          <p style={{ color: '#64748b', marginTop: 4, fontSize: 13 }}>
            {disputes.length} total dispute{disputes.length !== 1 ? 's' : ''}
            {counts.pending_approval ? ` • ${counts.pending_approval} awaiting approval` : ''}
          </p>
        </div>
        <button className="btn-secondary" onClick={load} style={{ fontSize: 12 }}>
          ↻ Refresh
        </button>
      </div>

      {/* Filter tabs */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, flexWrap: 'wrap' }}>
        {[
          { key: 'all', label: `All (${disputes.length})` },
          { key: 'pending_approval', label: `Pending (${counts.pending_approval || 0})` },
          { key: 'submitted', label: `Submitted (${counts.submitted || 0})` },
          { key: 'response_received', label: `Response (${counts.response_received || 0})` },
          { key: 'resolved', label: `Resolved (${counts.resolved || 0})` },
        ].map(tab => (
          <button
            key={tab.key}
            onClick={() => setFilter(tab.key)}
            style={{
              padding: '6px 14px',
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 500,
              background: filter === tab.key ? '#6366f1' : '#16161f',
              color: filter === tab.key ? 'white' : '#94a3b8',
              border: `1px solid ${filter === tab.key ? '#6366f1' : '#2a2a3a'}`,
              cursor: 'pointer',
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="empty-state">
          <div style={{ fontSize: 40, marginBottom: 12 }}>📭</div>
          <h3>No disputes {filter !== 'all' ? `with status "${filter}"` : 'yet'}</h3>
          <p style={{ color: '#64748b', marginTop: 8 }}>
            {filter === 'all'
              ? 'Upload a credit report to start identifying and disputing items.'
              : 'Try a different filter.'}
          </p>
          {filter === 'all' && (
            <Link to="/upload" style={{ display: 'inline-block', marginTop: 16 }}>
              <button className="btn-primary">Upload Report →</button>
            </Link>
          )}
        </div>
      ) : (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Creditor</th>
                <th>Bureau</th>
                <th>Round</th>
                <th>Strategy</th>
                <th>Priority</th>
                <th>Status</th>
                <th>Score Impact</th>
                <th>Next Action</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(d => {
                const statusConfig = STATUS_LABELS[d.status] || { label: d.status, cls: 'badge-draft' }
                const nextDate = d.next_action_date ? new Date(d.next_action_date) : null
                const isOverdue = nextDate && nextDate < new Date() && !['resolved', 'pending_approval'].includes(d.status)

                return (
                  <tr key={d.id}>
                    <td style={{ fontWeight: 500 }}>{d.creditor_name || 'Unknown'}</td>
                    <td style={{ textTransform: 'capitalize', color: '#94a3b8' }}>{d.bureau}</td>
                    <td style={{ color: '#64748b' }}>Round {d.current_round}</td>
                    <td>
                      <span style={{
                        background: 'rgba(99,102,241,0.1)',
                        color: '#6366f1',
                        padding: '2px 6px',
                        borderRadius: 4,
                        fontSize: 11,
                        fontFamily: 'monospace',
                      }}>
                        {d.strategy?.replace(/_/g, ' ')}
                      </span>
                    </td>
                    <td>
                      <span style={{
                        color: d.priority >= 8 ? '#ef4444' : d.priority >= 5 ? '#f59e0b' : '#10b981',
                        fontWeight: 600,
                        fontSize: 13,
                      }}>
                        {d.priority}/10
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${statusConfig.cls}`}>{statusConfig.label}</span>
                    </td>
                    <td style={{ color: '#10b981' }}>+{d.score_impact_estimate || 0} pts</td>
                    <td style={{ color: isOverdue ? '#ef4444' : '#64748b', fontSize: 12 }}>
                      {nextDate ? (
                        <>
                          {isOverdue ? '⚠️ ' : ''}{nextDate.toLocaleDateString()}
                        </>
                      ) : '—'}
                    </td>
                    <td>
                      <Link to={`/disputes/${d.id}`} style={{ color: '#6366f1', fontSize: 12 }}>
                        View →
                      </Link>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
