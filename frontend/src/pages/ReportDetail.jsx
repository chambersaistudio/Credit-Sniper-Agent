import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../api'

export default function ReportDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [creating, setCreating] = useState(null)
  const [messages, setMessages] = useState({})

  useEffect(() => {
    api.getReport(id)
      .then(setReport)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [id])

  const handleCreateDispute = async (account) => {
    setCreating(account.id)
    try {
      const data = await api.createDispute({
        account_id: account.id,
        bureau: report.bureau === 'tri_merge' ? 'equifax' : report.bureau,
        dispute_type: 'bureau_dispute',
      })
      setMessages(m => ({ ...m, [account.id]: { type: 'success', text: `Dispute created! Letter ready for review.` } }))
      setTimeout(() => navigate(`/disputes/${data.dispute_id}`), 1500)
    } catch (e) {
      setMessages(m => ({ ...m, [account.id]: { type: 'error', text: e.message } }))
    } finally {
      setCreating(null)
    }
  }

  if (loading) return <div style={{ padding: 40, textAlign: 'center' }}><div className="spinner" style={{ width: 32, height: 32, margin: '0 auto' }} /></div>
  if (error) return <div style={{ padding: 40 }}><div className="alert alert-error">{error}</div></div>
  if (!report) return null

  const disputable = report.accounts.filter(a => a.is_disputable)
  const clean = report.accounts.filter(a => !a.is_disputable)

  return (
    <div style={{ padding: 32, maxWidth: 900 }}>
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, textTransform: 'capitalize' }}>
            {report.bureau} Credit Report
          </h1>
          <div style={{ color: '#64748b', fontSize: 13, marginTop: 4 }}>
            Pulled {new Date(report.pull_date).toLocaleDateString()}
            {report.report_date && ` • Report date: ${report.report_date}`}
          </div>
        </div>
        {report.credit_score && (
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 36, fontWeight: 700, color: scoreColor(report.credit_score) }}>
              {report.credit_score}
            </div>
            <div style={{ fontSize: 12, color: '#64748b' }}>Credit Score</div>
          </div>
        )}
      </div>

      {/* Summary */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 24 }}>
        <MiniStat value={report.accounts.length} label="Total Accounts" />
        <MiniStat value={disputable.length} label="Disputable" color="#ef4444" />
        <MiniStat value={clean.length} label="Clean" color="#10b981" />
        <MiniStat value={report.inquiries.filter(i => i.is_disputable).length} label="Bad Inquiries" color="#f59e0b" />
      </div>

      {/* Disputable Accounts */}
      {disputable.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: '#ef4444' }}>
            🎯 Disputable Accounts ({disputable.length})
          </h2>
          {disputable.map(account => (
            <div key={account.id} className="card" style={{ marginBottom: 12, borderColor: 'rgba(239,68,68,0.3)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 15 }}>{account.creditor_name || 'Unknown Creditor'}</div>
                  <div style={{ color: '#64748b', fontSize: 12, marginTop: 2 }}>
                    {account.account_number && `Account: ${account.account_number} • `}
                    {account.account_type && `${account.account_type} • `}
                    {account.account_status && <span style={{ color: statusColor(account.account_status) }}>{account.account_status}</span>}
                  </div>
                  {account.balance !== null && account.balance !== undefined && (
                    <div style={{ fontSize: 13, marginTop: 4 }}>
                      Balance: <strong style={{ color: '#e2e8f0' }}>${account.balance?.toLocaleString()}</strong>
                    </div>
                  )}
                </div>
                <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 16 }}>
                  <div style={{
                    fontSize: 20,
                    fontWeight: 700,
                    color: account.priority_score >= 8 ? '#ef4444' : account.priority_score >= 5 ? '#f59e0b' : '#10b981',
                  }}>
                    {account.priority_score}/10
                  </div>
                  <div style={{ fontSize: 11, color: '#64748b' }}>Priority</div>
                </div>
              </div>

              {/* Violations */}
              {account.metro2_violations?.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                  {account.metro2_violations.slice(0, 3).map((v, i) => (
                    <div key={i} className="violation-item">
                      <div className="violation-type">{v.violation_type || 'violation'}</div>
                      <div className="violation-desc">{v.description || v.specific_violation}</div>
                      {v.legal_citation && <div className="violation-citation">{v.legal_citation}</div>}
                    </div>
                  ))}
                </div>
              )}

              {/* Dispute Reasons */}
              {account.dispute_reasons?.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 12 }}>
                  {account.dispute_reasons.map((r, i) => <span key={i} className="tag">{r}</span>)}
                </div>
              )}

              {messages[account.id] && (
                <div className={`alert alert-${messages[account.id].type}`} style={{ marginBottom: 8 }}>
                  {messages[account.id].text}
                </div>
              )}

              <button
                className="btn-primary"
                onClick={() => handleCreateDispute(account)}
                disabled={creating === account.id}
                style={{ opacity: creating === account.id ? 0.7 : 1 }}
              >
                {creating === account.id ? '⚙️ Creating Dispute...' : '⚔️ Start Dispute →'}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Disputable Inquiries */}
      {report.inquiries.filter(i => i.is_disputable).length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: '#f59e0b' }}>
            ❓ Disputable Inquiries
          </h2>
          <div className="card">
            <table>
              <thead>
                <tr>
                  <th>Creditor</th>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {report.inquiries.filter(i => i.is_disputable).map(inq => (
                  <tr key={inq.id}>
                    <td style={{ fontWeight: 500 }}>{inq.creditor_name}</td>
                    <td style={{ color: '#64748b' }}>{inq.inquiry_date}</td>
                    <td><span className="badge badge-pending">{inq.inquiry_type}</span></td>
                    <td style={{ color: '#94a3b8', fontSize: 12 }}>{inq.dispute_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Clean Accounts */}
      {clean.length > 0 && (
        <div>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: '#10b981' }}>
            ✅ Clean Accounts ({clean.length})
          </h2>
          <div className="card">
            <table>
              <thead>
                <tr>
                  <th>Creditor</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Balance</th>
                </tr>
              </thead>
              <tbody>
                {clean.map(a => (
                  <tr key={a.id}>
                    <td style={{ fontWeight: 500 }}>{a.creditor_name}</td>
                    <td style={{ color: '#64748b' }}>{a.account_type || '—'}</td>
                    <td style={{ color: statusColor(a.account_status) }}>{a.account_status || '—'}</td>
                    <td>{a.balance != null ? `$${a.balance?.toLocaleString()}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

function MiniStat({ value, label, color = '#6366f1' }) {
  return (
    <div className="stat-card">
      <div className="value" style={{ fontSize: 24, color }}>{value}</div>
      <div className="label">{label}</div>
    </div>
  )
}

function scoreColor(score) {
  if (score >= 750) return '#10b981'
  if (score >= 700) return '#6366f1'
  if (score >= 650) return '#f59e0b'
  return '#ef4444'
}

function statusColor(status) {
  if (!status) return '#64748b'
  const s = status.toLowerCase()
  if (s.includes('current') || s.includes('open')) return '#10b981'
  if (s.includes('late') || s.includes('past')) return '#f59e0b'
  if (s.includes('charged') || s.includes('collection') || s.includes('derogatory')) return '#ef4444'
  return '#94a3b8'
}
