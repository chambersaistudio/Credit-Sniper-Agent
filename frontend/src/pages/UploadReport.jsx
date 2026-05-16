import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'

const CHECKS = [
  'FCRA violations (15 U.S.C. § 1681)',
  'Metro 2 format field errors',
  'Accounts past 7-year reporting limit',
  'Re-aged debts — manipulated DOFD',
  'Balance mismatches across bureaus',
  'Charge-offs still reporting after sale',
  'Inquiries without permissible purpose',
  'Duplicate accounts',
]

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
    if (f && (f.type === 'application/pdf' || f.name.endsWith('.pdf'))) {
      setFile(f); setError(null)
    } else {
      setError('Please drop a PDF file')
    }
  }

  const handleUpload = async () => {
    if (!file) return
    setUploading(true); setError(null)
    const fd = new FormData()
    fd.append('file', file)
    fd.append('bureau', bureau)
    try {
      const data = await api.uploadReport(fd)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }

  if (result) return <AnalysisResult result={result} onReset={() => { setResult(null); setFile(null) }} navigate={navigate} />

  return (
    <div style={{ padding: '36px 40px 60px', maxWidth: 720 }}>
      <div style={{ marginBottom: 32 }}>
        <h1 className="page-title">Upload Credit Report</h1>
        <p className="page-subtitle">AI scans every account for FCRA violations and Metro 2 errors</p>
      </div>

      {/* Drop zone */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]) }}
          style={{
            border: `2px dashed ${dragOver ? 'var(--violet)' : file ? 'var(--emerald)' : 'var(--border)'}`,
            borderRadius: 12,
            padding: '52px 32px',
            textAlign: 'center',
            cursor: 'pointer',
            transition: 'all 0.2s',
            background: dragOver ? 'rgba(124,58,237,0.04)' : file ? 'rgba(16,185,129,0.04)' : 'var(--bg-1)',
            marginBottom: 20,
          }}
        >
          <input ref={fileRef} type="file" accept=".pdf" style={{ display: 'none' }} onChange={e => handleFile(e.target.files[0])} />
          {file ? (
            <>
              <div style={{ fontSize: 36, marginBottom: 12 }}>✅</div>
              <div style={{ fontWeight: 700, color: 'var(--emerald)', fontSize: 15 }}>{file.name}</div>
              <div style={{ color: 'var(--text-4)', fontSize: 12, marginTop: 4 }}>
                {(file.size / 1024 / 1024).toFixed(2)} MB · Click to change
              </div>
            </>
          ) : (
            <>
              <div style={{
                width: 56, height: 56,
                background: 'var(--bg-3)',
                border: '1px solid var(--border)',
                borderRadius: 14,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 24,
                margin: '0 auto 16px',
              }}>📄</div>
              <div style={{ fontWeight: 600, color: 'var(--text-1)', fontSize: 15, marginBottom: 6 }}>
                Drop your credit report here
              </div>
              <div style={{ color: 'var(--text-4)', fontSize: 13 }}>or click to browse · PDF only</div>
              <div style={{ color: 'var(--text-4)', fontSize: 11, marginTop: 10 }}>
                Supports: AnnualCreditReport.com, Equifax, Experian, TransUnion, tri-merge
              </div>
            </>
          )}
        </div>

        <div className="form-group">
          <label>Bureau</label>
          <select value={bureau} onChange={e => setBureau(e.target.value)}>
            <option value="auto_detect">Auto-detect from report</option>
            <option value="equifax">Equifax</option>
            <option value="experian">Experian</option>
            <option value="transunion">TransUnion</option>
            <option value="tri_merge">Tri-Merge (all three)</option>
          </select>
        </div>

        {error && <div className="alert alert-error" style={{ marginBottom: 16 }}>{error}</div>}

        <button
          className="btn btn-primary btn-full btn-lg"
          onClick={handleUpload}
          disabled={!file || uploading}
        >
          {uploading ? (
            <><span className="spinner" /> Analyzing with AI — this takes 30–60 seconds...</>
          ) : '⚡ Analyze Report'}
        </button>

        {uploading && (
          <div style={{ marginTop: 16, color: 'var(--text-4)', fontSize: 12, textAlign: 'center', lineHeight: 1.6 }}>
            Scanning for FCRA violations, Metro 2 field errors, 7-year rule violations,<br/>
            and every other disputable item on your report...
          </div>
        )}
      </div>

      {/* What the AI checks */}
      <div className="card">
        <div className="section-title">What the AI checks for</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          {CHECKS.map(c => (
            <div key={c} style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 13, color: 'var(--text-3)', padding: '6px 0' }}>
              <span style={{ color: 'var(--violet-light)', fontSize: 12, flexShrink: 0 }}>✓</span>
              {c}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function AnalysisResult({ result, onReset, navigate }) {
  const disp = result.analysis?.disputable_accounts || []

  return (
    <div style={{ padding: '36px 40px 60px', maxWidth: 800 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 className="page-title">Analysis Complete</h1>
        <p className="page-subtitle">
          {result.disputable_accounts} disputable item{result.disputable_accounts !== 1 ? 's' : ''} found
          {result.estimated_score_gain > 0 ? ` · up to +${result.estimated_score_gain} points possible` : ''}
        </p>
      </div>

      <div className="alert alert-success" style={{ marginBottom: 24 }}>
        ✅ Report parsed and analyzed. Review the findings below, then start disputes on the items you want to fight.
      </div>

      {/* Summary chips */}
      <div className="stat-grid stat-grid-3" style={{ marginBottom: 24 }}>
        <div className="stat-chip">
          <div className="stat-label">Disputable Accounts</div>
          <div className="stat-value" style={{ color: 'var(--rose)' }}>{result.disputable_accounts}</div>
        </div>
        <div className="stat-chip">
          <div className="stat-label">Disputable Inquiries</div>
          <div className="stat-value" style={{ color: 'var(--amber)' }}>{result.disputable_inquiries}</div>
        </div>
        <div className="stat-chip">
          <div className="stat-label">Est. Score Gain</div>
          <div className="stat-value" style={{ color: 'var(--emerald)' }}>+{result.estimated_score_gain}</div>
        </div>
      </div>

      {result.overall_strategy && (
        <div className="alert alert-info" style={{ marginBottom: 20 }}>
          <strong>AI Strategy:</strong> {result.overall_strategy}
        </div>
      )}

      {/* Disputable accounts */}
      {disp.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <div className="section-title">Disputable Accounts</div>
          {disp.map((item, i) => (
            <div key={i} className="card card-hover" style={{ marginBottom: 10, borderColor: 'rgba(244,63,94,0.2)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                <div>
                  <div style={{ fontWeight: 700, fontSize: 15, color: 'var(--text-0)' }}>{item.creditor_name}</div>
                  <div style={{ color: 'var(--text-4)', fontSize: 12, marginTop: 2 }}>
                    {item.account_number && `${item.account_number} · `}
                    {item.primary_strategy?.replace(/_/g, ' ')}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div style={{
                    fontSize: 22, fontWeight: 800, letterSpacing: -0.5,
                    color: item.priority_score >= 8 ? 'var(--rose)' : item.priority_score >= 5 ? 'var(--amber)' : 'var(--emerald)',
                  }}>
                    {item.priority_score}<span style={{ fontSize: 13, fontWeight: 400, color: 'var(--text-4)' }}>/10</span>
                  </div>
                  {item.estimated_score_impact > 0 && (
                    <div style={{ color: 'var(--emerald)', fontSize: 12, fontWeight: 600 }}>+{item.estimated_score_impact} pts</div>
                  )}
                </div>
              </div>
              {item.violations?.slice(0, 2).map((v, vi) => (
                <div key={vi} className="violation-item">
                  <div className="violation-type">{v.violation_type} violation</div>
                  <div className="violation-desc">{v.description}</div>
                  {v.legal_citation && <div className="violation-citation">{v.legal_citation}</div>}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', gap: 12 }}>
        <button className="btn btn-primary" onClick={() => navigate(`/reports/${result.report_id}`)}>
          View Full Report & Start Disputes →
        </button>
        <button className="btn btn-secondary" onClick={onReset}>Upload Another</button>
      </div>
    </div>
  )
}
