import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { PageHeader, ProcessingCard, bureauName, useReportProcessing } from '../components/ui'

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
  // The upload request only hands back a report id: reading the PDF is a
  // background job, so we follow it rather than hold a connection open.
  const [accepted, setAccepted] = useState(null)
  const [result, setResult] = useState(null)
  const input = useRef()
  const { status, processing, error: pollError } = useReportProcessing(accepted?.report_id, {
    enabled: Boolean(accepted) && !result,
  })

  useEffect(() => {
    if (!accepted || processing || result) return
    api.getReport(accepted.report_id).then(setResult).catch(e => setError(e.message))
  }, [accepted, processing, result])

  const reset = () => { setAccepted(null); setResult(null); setFile(null); setError(null) }

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
      // 202: accepted for processing. Uploading the same file twice resolves
      // to the same report rather than paying to read it again.
      setAccepted(await api.uploadReport(file, bureau))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (accepted && !result) {
    return (
      <div className="content">
        <PageHeader title="Reading your report" back="/reports" />
        <div className="stack">
          {accepted.duplicate && (
            <div className="alert alert-warn">
              You'd already sent us this exact file, so we're showing that report instead of reading it twice.
            </div>
          )}
          <ProcessingCard status={status} processing={processing} />
          {pollError && (
            <div className="alert alert-warn">
              We lost track of the progress here, but your report is still being read.{' '}
              <Link to={`/reports/${accepted.report_id}`}>Open the report</Link> to check on it.
            </div>
          )}
          {error && <div className="alert alert-error">{error}</div>}
          <Link to={`/reports/${accepted.report_id}`} className="btn btn-block">See the report</Link>
        </div>
      </div>
    )
  }

  if (result) {
    // Never present a clean "added" result for a report we couldn't read and
    // verify — the state of the extraction leads.
    const verified = result.extraction_status === 'verified'
    // Whether the document was analyzed at all. When the provider was
    // unavailable we know nothing about its contents.
    const analyzed = result.extraction_status !== 'provider_unavailable'
    return (
      <div className="content">
        <PageHeader
          title={!analyzed ? 'Report stored' : verified ? 'Report added' : 'Report needs review'}
          back="/reports"
        />
        <div className="card stack">
          <div className="row-between">
            <div className="card-title">{bureauName(result.bureau)}</div>
            <div className="card-title">{result.credit_score ?? '—'}</div>
          </div>
          <div className="grid-2">
            {/* Nothing was read, so "0" would be a claim about the document. */}
            <div className="stat">
              <div className="stat-label">Accounts read</div>
              <div className="stat-value">{analyzed ? result.total_accounts : '—'}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Inquiries read</div>
              <div className="stat-value">{analyzed ? result.total_inquiries : '—'}</div>
            </div>
          </div>
          {result.extraction_status === 'needs_audit' && (
            <div className="alert alert-warn">
              This report was read successfully, but the verification pass found unresolved extraction
              differences. Dispute analysis is paused until those differences are reconciled.
            </div>
          )}
          {/* Our service was down, not their document — and nothing was read,
              so we can't say the report contains no accounts. */}
          {result.extraction_status === 'provider_unavailable' && (
            <div className="alert alert-warn">
              Your report was stored safely, but AI extraction is temporarily unavailable. No report data
              was analyzed. Retry extraction once the service is available.
            </div>
          )}
          {!verified && !['needs_audit', 'provider_unavailable'].includes(result.extraction_status) && (
            <div className="alert alert-error">
              Your original PDF is stored safely, but it couldn't be read completely, so dispute analysis
              is on hold for this report.
            </div>
          )}
          {(result.warnings || []).map(w => <div key={w} className="alert alert-warn">{w}</div>)}
          <p className="small muted">
            Each account is matched to the same account on your other bureaus' reports. Upload all three to compare them.
          </p>
          {verified && <Link to="/reports?view=accounts" className="btn btn-primary btn-block">Review accounts</Link>}
          <Link to={`/reports/${result.report_id}`} className={`btn btn-block${verified ? '' : ' btn-primary'}`}>
            See exactly what was read
          </Link>
          <button className="btn btn-ghost btn-block" onClick={reset}>Upload another</button>
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
