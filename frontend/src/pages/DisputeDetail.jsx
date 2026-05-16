import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../api'

const RESPONSE_TYPES = [
  { value: 'removed', label: '✅ Removed — Item deleted from report' },
  { value: 'updated', label: '✏️ Updated — Information was corrected' },
  { value: 'verified', label: '❌ Verified — Bureau says it\'s accurate' },
  { value: 'denied', label: '🚫 Denied — Dispute rejected' },
  { value: 'no_response', label: '🔇 No Response — Deadline passed, no reply' },
  { value: 'frivolous', label: '⚠️ Flagged Frivolous — Need different approach' },
]

export default function DisputeDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [dispute, setDispute] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)
  const [activeTab, setActiveTab] = useState('letter')
  const [responseType, setResponseType] = useState('')
  const [responseDetails, setResponseDetails] = useState('')
  const [showResponseForm, setShowResponseForm] = useState(false)
  const [message, setMessage] = useState(null)

  const load = () => {
    api.getDispute(id)
      .then(setDispute)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [id])

  const handleApprove = async () => {
    setActionLoading(true)
    try {
      await api.approveDispute(id)
      setMessage({ type: 'success', text: 'Dispute approved! Now mark as submitted after sending the letter.' })
      load()
    } catch (e) {
      setMessage({ type: 'error', text: e.message })
    } finally {
      setActionLoading(false)
    }
  }

  const handleSubmit = async () => {
    setActionLoading(true)
    try {
      await api.submitDispute(id)
      setMessage({ type: 'success', text: 'Dispute marked as submitted. Monitor for bureau response within 30 days.' })
      load()
    } catch (e) {
      setMessage({ type: 'error', text: e.message })
    } finally {
      setActionLoading(false)
    }
  }

  const handleRecordResponse = async () => {
    if (!responseType) return
    setActionLoading(true)
    try {
      const result = await api.recordResponse(id, {
        response_type: responseType,
        response_details: responseDetails,
      })
      setMessage({ type: 'success', text: 'Response recorded.' })
      setShowResponseForm(false)
      load()
    } catch (e) {
      setMessage({ type: 'error', text: e.message })
    } finally {
      setActionLoading(false)
    }
  }

  const handleRegenerate = async (letterId, letterType) => {
    setActionLoading(true)
    try {
      await api.regenerateLetter(letterId, { letter_type: letterType })
      setMessage({ type: 'success', text: `Letter regenerated as ${letterType}` })
      load()
    } catch (e) {
      setMessage({ type: 'error', text: e.message })
    } finally {
      setActionLoading(false)
    }
  }

  if (loading) return <div style={{ padding: 40, textAlign: 'center' }}><div className="spinner" style={{ width: 32, height: 32, margin: '0 auto' }} /></div>
  if (error) return <div style={{ padding: 40 }}><div className="alert alert-error">{error}</div></div>
  if (!dispute) return null

  const currentLetter = dispute.letters?.find(l => l.round_number === dispute.current_round) || dispute.letters?.[0]
  const latestRound = dispute.rounds?.[dispute.rounds.length - 1]

  return (
    <div style={{ padding: 32, maxWidth: 900 }}>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ color: '#64748b', fontSize: 13, marginBottom: 8, cursor: 'pointer' }} onClick={() => navigate('/disputes')}>
          ← Back to Disputes
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 700 }}>{dispute.creditor_name || 'Unknown Creditor'}</h1>
            <div style={{ color: '#64748b', fontSize: 13, marginTop: 4 }}>
              {dispute.account_number && `Account: ${dispute.account_number} • `}
              {dispute.bureau && <span style={{ textTransform: 'capitalize' }}>{dispute.bureau}</span>}
              {` • Round ${dispute.current_round}`}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span className={`badge badge-${dispute.status === 'pending_approval' ? 'pending' :
              dispute.status === 'submitted' ? 'submitted' :
              dispute.status === 'resolved' ? 'resolved' :
              dispute.status === 'approved' ? 'approved' : 'draft'}`}>
              {dispute.status?.replace(/_/g, ' ')}
            </span>
          </div>
        </div>
      </div>

      {message && (
        <div className={`alert alert-${message.type}`} style={{ marginBottom: 16 }}>
          {message.text}
        </div>
      )}

      {/* Action Buttons */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 24 }}>
        {dispute.status === 'pending_approval' && (
          <button className="btn-success" onClick={handleApprove} disabled={actionLoading}>
            {actionLoading ? '...' : '✓ Approve Letter'}
          </button>
        )}
        {dispute.status === 'approved' && (
          <button className="btn-primary" onClick={handleSubmit} disabled={actionLoading}>
            {actionLoading ? '...' : '📤 Mark as Submitted'}
          </button>
        )}
        {['submitted', 'response_received'].includes(dispute.status) && !showResponseForm && (
          <button className="btn-secondary" onClick={() => setShowResponseForm(true)}>
            📥 Record Bureau Response
          </button>
        )}
        {dispute.status === 'pending_approval' && currentLetter && (
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            {['section_609', 'fcra_violation', 'metro2_compliance'].map(t => (
              <button
                key={t}
                className="btn-secondary"
                style={{ fontSize: 11, padding: '6px 10px' }}
                onClick={() => handleRegenerate(currentLetter.id, t)}
                disabled={actionLoading}
              >
                ↺ {t.replace(/_/g, ' ')}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Record Response Form */}
      {showResponseForm && (
        <div className="card" style={{ marginBottom: 20, borderColor: 'rgba(59,130,246,0.4)' }}>
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 16 }}>Record Bureau Response</h3>
          <div className="form-group">
            <label>Response Type</label>
            <select value={responseType} onChange={e => setResponseType(e.target.value)}>
              <option value="">Select response type...</option>
              {RESPONSE_TYPES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Response Details (paste bureau letter text or summary)</label>
            <textarea
              value={responseDetails}
              onChange={e => setResponseDetails(e.target.value)}
              rows={4}
              placeholder="Paste or summarize the bureau's response..."
            />
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn-primary" onClick={handleRecordResponse} disabled={!responseType || actionLoading}>
              Record Response
            </button>
            <button className="btn-secondary" onClick={() => setShowResponseForm(false)}>Cancel</button>
          </div>
        </div>
      )}

      {/* Next Step Recommendation */}
      {latestRound?.next_strategy && latestRound.next_strategy !== 'monitor' && (
        <div className="alert alert-info" style={{ marginBottom: 20 }}>
          <strong>Recommended Next Action:</strong> {latestRound.notes}
        </div>
      )}
      {latestRound?.next_strategy === 'monitor' && (
        <div className="alert alert-success" style={{ marginBottom: 20 }}>
          🎉 <strong>SUCCESS:</strong> {latestRound.notes}
        </div>
      )}

      {/* Strategy Info */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
        <div className="card">
          <h3 style={{ fontSize: 13, fontWeight: 600, color: '#64748b', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Dispute Strategy
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <InfoRow label="Type" value={dispute.dispute_type?.replace(/_/g, ' ')} />
            <InfoRow label="Strategy" value={dispute.strategy?.replace(/_/g, ' ')} />
            <InfoRow label="Priority" value={`${dispute.priority}/10`} />
            <InfoRow label="Est. Score Impact" value={`+${dispute.score_impact_estimate || 0} points`} color="#10b981" />
            {dispute.next_action_date && (
              <InfoRow label="Next Action Due" value={new Date(dispute.next_action_date).toLocaleDateString()} />
            )}
          </div>
        </div>

        <div className="card">
          <h3 style={{ fontSize: 13, fontWeight: 600, color: '#64748b', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Timeline
          </h3>
          {dispute.rounds?.length > 0 ? (
            dispute.rounds.map((r, i) => (
              <div key={i} style={{ borderLeft: '2px solid #2a2a3a', paddingLeft: 12, marginBottom: 12, marginLeft: 6 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>Round {r.round_number}</div>
                {r.sent_date && <div style={{ color: '#64748b', fontSize: 11 }}>Sent: {new Date(r.sent_date).toLocaleDateString()}</div>}
                {r.response_type && (
                  <div style={{ color: r.response_type === 'removed' ? '#10b981' : '#94a3b8', fontSize: 12, marginTop: 4 }}>
                    Response: {r.response_type}
                  </div>
                )}
              </div>
            ))
          ) : (
            <div style={{ color: '#64748b', fontSize: 13 }}>No rounds sent yet</div>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 16, borderBottom: '1px solid #2a2a3a' }}>
        {[
          { key: 'letter', label: `📝 Letter (Round ${dispute.current_round})` },
          { key: 'history', label: `📋 All Letters (${dispute.letters?.length || 0})` },
        ].map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            style={{
              padding: '8px 16px',
              background: 'transparent',
              color: activeTab === tab.key ? '#6366f1' : '#64748b',
              borderBottom: activeTab === tab.key ? '2px solid #6366f1' : '2px solid transparent',
              borderRadius: 0,
              fontSize: 13,
              fontWeight: activeTab === tab.key ? 600 : 400,
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Current Letter */}
      {activeTab === 'letter' && currentLetter && (
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div>
              <div style={{ fontWeight: 600 }}>{currentLetter.subject}</div>
              <div style={{ color: '#64748b', fontSize: 12, marginTop: 2 }}>
                To: {currentLetter.recipient} •
                Type: {currentLetter.letter_type?.replace(/_/g, ' ')} •
                <span className={`badge badge-${currentLetter.status === 'submitted' ? 'submitted' : 'draft'}`} style={{ marginLeft: 6 }}>
                  {currentLetter.status}
                </span>
              </div>
            </div>
          </div>

          {currentLetter.legal_citations?.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: '#64748b', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Legal Citations</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {currentLetter.legal_citations.map((c, i) => (
                  <span key={i} style={{
                    background: 'rgba(99,102,241,0.1)',
                    color: '#6366f1',
                    border: '1px solid rgba(99,102,241,0.3)',
                    borderRadius: 4,
                    padding: '2px 8px',
                    fontSize: 11,
                    fontFamily: 'monospace',
                  }}>{c}</span>
                ))}
              </div>
            </div>
          )}

          <div className="divider" />
          <pre style={{ fontSize: 13, lineHeight: 1.7, color: '#e2e8f0', maxHeight: 600 }}>
            {currentLetter.body}
          </pre>

          <div style={{ marginTop: 16, display: 'flex', gap: 8, alignItems: 'center' }}>
            <button
              className="btn-secondary"
              style={{ fontSize: 12 }}
              onClick={() => {
                const el = document.createElement('textarea')
                el.value = currentLetter.body
                document.body.appendChild(el)
                el.select()
                document.execCommand('copy')
                document.body.removeChild(el)
                setMessage({ type: 'success', text: 'Letter copied to clipboard!' })
                setTimeout(() => setMessage(null), 3000)
              }}
            >
              📋 Copy Letter
            </button>
            <span style={{ color: '#64748b', fontSize: 12 }}>
              Send via certified mail or submit through bureau portal
            </span>
          </div>
        </div>
      )}

      {/* Letter History */}
      {activeTab === 'history' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {dispute.letters?.length === 0 && (
            <div className="empty-state">No letters generated yet.</div>
          )}
          {dispute.letters?.map(letter => (
            <div key={letter.id} className="card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Round {letter.round_number} — {letter.letter_type?.replace(/_/g, ' ')}</div>
                  <div style={{ color: '#64748b', fontSize: 12 }}>To: {letter.recipient} • {letter.submitted_at ? `Submitted ${new Date(letter.submitted_at).toLocaleDateString()}` : 'Not yet submitted'}</div>
                </div>
                <span className={`badge badge-${letter.status === 'submitted' ? 'submitted' : 'draft'}`}>{letter.status}</span>
              </div>
              <pre style={{ fontSize: 12, maxHeight: 200, color: '#94a3b8' }}>
                {letter.body?.slice(0, 500)}...
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function InfoRow({ label, value, color }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
      <span style={{ color: '#64748b' }}>{label}</span>
      <span style={{ color: color || '#e2e8f0', textTransform: 'capitalize' }}>{value || '—'}</span>
    </div>
  )
}
