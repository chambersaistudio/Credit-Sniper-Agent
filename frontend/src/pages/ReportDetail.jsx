import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api'
import PaymentHistory from '../components/PaymentHistory'
import { ErrorBox, Loading, PageHeader, bureauName, humanize, money, shortDate, useAsync } from '../components/ui'

const MONEY = new Set(['balance', 'past_due_amount', 'high_balance', 'credit_limit', 'original_amount', 'monthly_payment'])
// Rendered on their own, not as plain rows.
const SKIP = new Set(['id', 'creditor_name', 'payment_history', 'field_evidence', 'source_pages', 'contact'])

// What the extraction state means for the consumer, and whether this report
// can be used for dispute analysis at all.
const STATUS = {
  verified: ['ok', 'Verified against your original PDF', 'Two independent passes read your uploaded document and agreed.'],
  // The document WAS read — only the verification pass disagreed. Never tell
  // the consumer to re-upload here; the file was fine.
  needs_audit: ['warn', 'Read, not yet verified', 'This report was read successfully, but the verification pass found unresolved extraction differences. Dispute analysis is paused until those differences are reconciled. Nothing was auto-corrected.'],
  extraction_incomplete: ['error', 'Incomplete', "Your document wasn't read completely, so this report can't be used for dispute analysis yet. Re-uploading a text-based PDF may help."],
  // The document itself is the problem — the only case that should tell the
  // consumer their PDF couldn't be read.
  failed: ['error', "We couldn't reliably read this PDF", 'Your original is stored safely, but nothing could be extracted from it. A text-based PDF (rather than a scan or photo) usually works.'],
  // Our service failed, not their file. Never suggest re-uploading here.
  provider_unavailable: ['warn', 'Extraction unavailable', 'Your report was stored safely, but AI extraction is temporarily unavailable. No report data was analyzed. Retry extraction once the service is available.'],
}

export default function ReportDetail() {
  const { id } = useParams()
  const { data, error, loading, reload } = useAsync(() => api.getReport(id), [id])
  const [tone, title, detail] = (data && STATUS[data.extraction_status]) || []

  return (
    <div className="content">
      <PageHeader
        title={data ? `${bureauName(data.bureau)} report` : 'Report'}
        subtitle={data && `Report date ${shortDate(data.report_date || data.pull_date)}`}
        back="/reports?view=uploads"
      />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && (
        <>
          {title && (
            <div className={`alert alert-${tone}`} style={{ marginBottom: 12 }}>
              <strong>{title}.</strong> {detail}
              {data.extraction_reasons?.length > 0 && (
                <ul className="small" style={{ margin: '6px 0 0 16px' }}>
                  {data.extraction_reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              )}
              {/* Re-runs against the original already in storage — the
                  consumer never has to find and upload the same file again. */}
              {data.extraction_retryable && <RetryExtraction id={id} onDone={reload} />}
            </div>
          )}
          {data.score_type && data.credit_score != null && (
            <div className="card row-between" style={{ marginBottom: 12 }}>
              <span>{data.score_type}</span><strong>{data.credit_score}</strong>
            </div>
          )}

          <p className="small muted" style={{ marginBottom: 12 }}>
            Exactly what was read from your file. Blank means the report didn't show it — nothing is filled in or guessed.
          </p>

          <div className="section-title">Accounts ({data.accounts.length})</div>
          <div className="list">
            {data.accounts.map(a => (
              <details key={a.id} className="card">
                <summary>
                  {a.creditor_name || 'Unnamed account'}
                  {a.original_creditor && <span className="small muted"> · orig. {a.original_creditor}</span>}
                </summary>
                <dl className="compare" style={{ '--cols': 1 }}>
                  {Object.entries(a).filter(([k]) => !SKIP.has(k)).map(([k, v]) => (
                    <FieldRow key={k} name={k} value={MONEY.has(k) ? (v === null ? '—' : money(v)) : (v ?? '—')} />
                  ))}
                </dl>
                <TradelinePaymentHistory entries={a.payment_history} />
                <Evidence pages={a.source_pages} evidence={a.field_evidence} />
              </details>
            ))}
          </div>

          <div className="section">
            <div className="section-title">Inquiries ({data.inquiries.length})</div>
            <div className="list">
              {data.inquiries.map(i => (
                <div key={i.id} className="card row-between">
                  <span>
                    {i.creditor_name}
                    {i.inquiry_category && <span className="small muted"> · {humanize(i.inquiry_category)}</span>}
                  </span>
                  <span className="small muted">
                    {i.inquiry_date || '—'}
                    {' · '}
                    {/* Never say "hard" when the document didn't. */}
                    {i.inquiry_type ? i.inquiry_type : 'type not stated'}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {data.audit_findings?.length > 0 && (
            <div className="section">
              <div className="section-title">Review findings ({data.audit_findings.length})</div>
              <p className="small muted">What the second pass disagreed with. These are recorded, never applied automatically.</p>
              <div className="list">
                {data.audit_findings.map((f, i) => (
                  <div key={i} className="card">
                    <div className="card-title">{humanize(f.kind)}{f.account_name ? ` · ${f.account_name}` : ''}</div>
                    <div className="small">{f.explanation}</div>
                    {f.field && (
                      <div className="small muted">
                        {humanize(f.field)}: read “{f.extracted_value ?? '—'}”, document shows “{f.correct_value ?? '—'}”
                        {f.page ? ` (page ${f.page})` : ''}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function RetryExtraction({ id, onDone }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(null)
  const retry = async () => {
    setBusy(true); setFailed(null)
    try {
      await api.retryExtraction(id)
      onDone()
    } catch (e) {
      setFailed(e.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div style={{ marginTop: 10 }}>
      <button className="btn btn-sm" disabled={busy} onClick={retry}>
        {busy ? <><span className="spinner" /> Retrying…</> : 'Retry extraction'}
      </button>
      {failed && <div className="small" style={{ marginTop: 6 }}>{failed}</div>}
    </div>
  )
}

const FieldRow = ({ name, value }) => (
  <>
    <dt className="label">{humanize(name)}</dt>
    <dd>{String(value)}</dd>
  </>
)

function TradelinePaymentHistory({ entries }) {
  if (!entries?.length) return null
  return (
    <div style={{ marginTop: 10 }}>
      <div className="label">Payment history</div>
      {/* Same grid as the account view; dense here since this is the
          source-document audit view, not the primary profile. */}
      <PaymentHistory entries={entries} dense />
    </div>
  )
}

function Evidence({ pages, evidence }) {
  if (!pages?.length && !evidence?.length) return null
  return (
    <div style={{ marginTop: 10 }}>
      <div className="label">Where this came from</div>
      {pages?.length > 0 && <div className="small muted">Page{pages.length > 1 ? 's' : ''} {pages.join(', ')}</div>}
      {evidence?.map((e, i) => (
        <div key={i} className="small muted">
          {humanize(e.field)}: “{e.excerpt}”{e.page ? ` (p. ${e.page})` : ''}
        </div>
      ))}
    </div>
  )
}
