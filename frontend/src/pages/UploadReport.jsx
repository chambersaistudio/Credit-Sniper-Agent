import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'

export default function UploadReport() {
  const [file, setFile] = useState(null)
  const [bureau, setBureau] = useState('auto_detect')
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [dragOver, setDragOver] = useState(false)
  const fileRef = useRef()
  const navigate = useNavigate()

  const handleFile = (f) => {
    if (f && f.type === 'application/pdf') {
      setFile(f)
      setError(null)
    } else {
      setError('Please select a PDF file')
    }
  }

  const handleUpload = async () => {
    if (!file) return
    setUploading(true)
    setError(null)

    const formData = new FormData()
    formData.append('file', file)
    formData.append('bureau', bureau)

    try {
      const data = await api.uploadReport(formData)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }

  if (result) {
    return (
      <div style={{ padding: 32, maxWidth: 800 }}>
        <div className="alert alert-success">
          ✅ Report analyzed successfully!
        </div>

        <div className="card" style={{ marginBottom: 20 }}>
          <h2 style={{ fontSize: 18, fontWeight: 700, marginBottom: 20 }}>Analysis Complete</h2>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, marginBottom: 24 }}>
            <StatBox value={result.disputable_accounts} label="Disputable Accounts" color="#ef4444" />
            <StatBox value={result.disputable_inquiries} label="Disputable Inquiries" color="#f59e0b" />
            <StatBox value={`+${result.estimated_score_gain}`} label="Est. Score Gain" color="#10b981" />
          </div>

          {result.overall_strategy && (
            <div className="alert alert-info" style={{ marginBottom: 16 }}>
              <strong>AI Strategy:</strong> {result.overall_strategy}
            </div>
          )}

          {result.highest_priority_items?.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 12, color: '#64748b', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                Highest Priority Targets
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {result.highest_priority_items.map((item, i) => (
                  <span key={i} className="tag">🎯 {item}</span>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: 12, marginTop: 24 }}>
            <button
              className="btn-primary"
              onClick={() => navigate(`/reports/${result.report_id}`)}
            >
              View Full Report & Start Disputes →
            </button>
            <button
              className="btn-secondary"
              onClick={() => { setResult(null); setFile(null) }}
            >
              Upload Another
            </button>
          </div>
        </div>

        {result.analysis?.disputable_accounts?.length > 0 && (
          <div className="card">
            <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>Disputable Items Found</h3>
            {result.analysis.disputable_accounts.map((item, i) => (
              <div key={i} style={{
                padding: '14px 16px',
                background: '#16161f',
                border: '1px solid #2a2a3a',
                borderRadius: 8,
                marginBottom: 10,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 15 }}>{item.creditor_name}</div>
                    <div style={{ color: '#64748b', fontSize: 12, marginTop: 2 }}>
                      {item.account_number} • {item.primary_strategy}
                    </div>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div style={{
                      color: item.priority_score >= 8 ? '#ef4444' : item.priority_score >= 5 ? '#f59e0b' : '#10b981',
                      fontWeight: 700,
                      fontSize: 16,
                    }}>
                      {item.priority_score}/10
                    </div>
                    <div style={{ color: '#10b981', fontSize: 12 }}>+{item.estimated_score_impact} pts</div>
                  </div>
                </div>

                {item.violations?.slice(0, 2).map((v, vi) => (
                  <div key={vi} className="violation-item" style={{ marginTop: 10 }}>
                    <div className="violation-type">{v.violation_type} violation</div>
                    <div className="violation-desc">{v.description}</div>
                    <div className="violation-citation">{v.legal_citation}</div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div style={{ padding: 32, maxWidth: 600 }}>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700 }}>Upload Credit Report</h1>
        <p style={{ color: '#64748b', marginTop: 4 }}>
          Upload your credit report PDF and the AI will analyze every item for FCRA violations and Metro 2 inconsistencies.
        </p>
      </div>

      <div className="card">
        <div
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]) }}
          style={{
            border: `2px dashed ${dragOver ? '#6366f1' : file ? '#10b981' : '#2a2a3a'}`,
            borderRadius: 10,
            padding: '48px 24px',
            textAlign: 'center',
            cursor: 'pointer',
            transition: 'all 0.2s',
            background: dragOver ? 'rgba(99,102,241,0.05)' : 'transparent',
            marginBottom: 20,
          }}
        >
          <input
            ref={fileRef}
            type="file"
            accept=".pdf"
            style={{ display: 'none' }}
            onChange={(e) => handleFile(e.target.files[0])}
          />
          {file ? (
            <>
              <div style={{ fontSize: 40, marginBottom: 12 }}>✅</div>
              <div style={{ color: '#10b981', fontWeight: 600 }}>{file.name}</div>
              <div style={{ color: '#64748b', fontSize: 12, marginTop: 4 }}>
                {(file.size / 1024 / 1024).toFixed(2)} MB — Click to change
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 40, marginBottom: 12 }}>📄</div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Drop your credit report PDF here</div>
              <div style={{ color: '#64748b', fontSize: 13 }}>or click to browse</div>
              <div style={{ color: '#475569', fontSize: 11, marginTop: 12 }}>
                Supports: Annual Credit Report, Equifax, Experian, TransUnion, tri-merge PDFs
              </div>
            </>
          )}
        </div>

        <div className="form-group">
          <label>Bureau (or auto-detect)</label>
          <select value={bureau} onChange={(e) => setBureau(e.target.value)}>
            <option value="auto_detect">Auto-detect from report</option>
            <option value="equifax">Equifax</option>
            <option value="experian">Experian</option>
            <option value="transunion">TransUnion</option>
            <option value="tri_merge">Tri-Merge (all three)</option>
          </select>
        </div>

        {error && <div className="alert alert-error">{error}</div>}

        <button
          className="btn-primary"
          onClick={handleUpload}
          disabled={!file || uploading}
          style={{ width: '100%', padding: '12px', opacity: (!file || uploading) ? 0.6 : 1 }}
        >
          {uploading ? (
            <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10 }}>
              <span className="spinner" />
              Analyzing Report with AI... (may take 30-60 seconds)
            </span>
          ) : 'Analyze Report →'}
        </button>

        {uploading && (
          <div style={{ marginTop: 12, color: '#64748b', fontSize: 12, textAlign: 'center' }}>
            The AI is scanning for FCRA violations, Metro 2 inconsistencies, and disputable items...
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>What the AI checks for:</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {[
            'FCRA violations (15 U.S.C. § 1681)',
            'Metro 2 format reporting errors',
            'Accounts past 7-year reporting limit',
            'Mismatched data across bureaus',
            'Re-aged debts (manipulated DOFD)',
            'Duplicate accounts',
            'Inquiries without permissible purpose',
            'Charge-offs still reporting balance after sale',
          ].map((item, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13, color: '#94a3b8' }}>
              <span style={{ color: '#6366f1' }}>✓</span> {item}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function StatBox({ value, label, color }) {
  return (
    <div style={{
      background: '#0a0a0f',
      border: '1px solid #2a2a3a',
      borderRadius: 8,
      padding: '16px',
      textAlign: 'center',
    }}>
      <div style={{ fontSize: 28, fontWeight: 700, color }}>{value}</div>
      <div style={{ fontSize: 11, color: '#64748b', marginTop: 4, textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</div>
    </div>
  )
}
