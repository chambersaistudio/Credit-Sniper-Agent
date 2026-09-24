import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { PageHeader, bureauName } from '../components/ui'

const BUREAUS = [
  ['auto_detect', 'Detect automatically'],
  ['equifax', 'Equifax'],
  ['experian', 'Experian'],
  ['transunion', 'TransUnion'],
]

export default function UploadReport() {
  const [file, setFile] = useState(null)
  const [bureau, setBureau] = useState('auto_detect')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const input = useRef()

  const choose = f => {
    setError(null)
    if (!f) return
    if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
      setError('Choose a PDF file.')
      return
    }
    setFile(f)
  }

  const upload = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.uploadReport(file, bureau))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    // Never present a clean "added" result for a report we couldn't read and
    // verify — the state of the extraction leads.
    const verified = result.extraction_status === 'verified'
    return (
      <div className="content">
        <PageHeader title={verified ? 'Report added' : 'Report needs review'} back="/reports" />
        <div className="card stack">
          <div className="row-between">
            <div className="card-title">{bureauName(result.bureau)}</div>
            <div className="card-title">{result.credit_score ?? '—'}</div>
          </div>
          <div className="grid-2">
            <div className="stat"><div className="stat-label">Accounts read</div><div className="stat-value">{result.total_accounts}</div></div>
            <div className="stat"><div className="stat-label">Inquiries read</div><div className="stat-value">{result.total_inquiries}</div></div>
          </div>
          {result.extraction_status === 'needs_audit' && (
            <div className="alert alert-warn">
              This report was read successfully, but the verification pass found unresolved extraction
              differences. Dispute analysis is paused until those differences are reconciled.
            </div>
          )}
          {!verified && result.extraction_status !== 'needs_audit' && (
            <div className="alert alert-error">
              Your original PDF is stored safely, but it couldn't be read completely, so dispute analysis
              is on hold for this report.
            </div>
          )}
          {result.warnings.map(w => <div key={w} className="alert alert-warn">{w}</div>)}
          <p className="small muted">
            Each account is matched to the same account on your other bureaus' reports. Upload all three to compare them.
          </p>
          {verified && <Link to="/reports?view=accounts" className="btn btn-primary btn-block">Review accounts</Link>}
          <Link to={`/reports/${result.report_id}`} className={`btn btn-block${verified ? '' : ' btn-primary'}`}>
            See exactly what was read
          </Link>
          <button className="btn btn-ghost btn-block" onClick={() => { setResult(null); setFile(null) }}>Upload another</button>
        </div>
      </div>
    )
  }

  return (
    <div className="content">
      <PageHeader title="Upload a report" back="/reports" />
      <div className="stack">
        <button type="button" className={`dropzone${file ? ' has-file' : ''}`} onClick={() => input.current?.click()}
          onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); choose(e.dataTransfer.files[0]) }}>
          <input ref={input} type="file" accept="application/pdf,.pdf" hidden onChange={e => choose(e.target.files[0])} />
          <div style={{ fontSize: 32 }}>{file ? '✓' : '⇪'}</div>
          <div className="card-title">{file ? file.name : 'Choose your report PDF'}</div>
          <div className="small muted">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · tap to change` : 'From a bureau site or AnnualCreditReport.com'}</div>
        </button>

        <div className="field">
          <label htmlFor="bureau">Which bureau is this from?</label>
          <select id="bureau" value={bureau} onChange={e => setBureau(e.target.value)}>
            {BUREAUS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <span className="hint">Upload one bureau per file. Combined three-bureau reports aren't supported yet.</span>
        </div>

        {error && <div className="alert alert-error">{error}</div>}

        <button className="btn btn-primary btn-block" disabled={!file || busy} onClick={upload}>
          {busy ? <><span className="spinner" /> Reading report…</> : 'Upload and read'}
        </button>
        <p className="tiny muted">
          The file is stored on this server so every account can be traced back to the page it came from. Nothing is sent to a
          bureau or creditor unless you approve it.
        </p>
      </div>
    </div>
  )
}
