import { useParams } from 'react-router-dom'
import { api } from '../api'
import { ErrorBox, Loading, PageHeader, bureauName, humanize, money, shortDate, useAsync } from '../components/ui'

const MONEY = new Set(['balance', 'past_due_amount', 'high_balance', 'credit_limit', 'original_amount', 'monthly_payment'])
const SKIP = new Set(['id', 'creditor_name'])

export default function ReportDetail() {
  const { id } = useParams()
  const { data, error, loading, reload } = useAsync(() => api.getReport(id), [id])
  return (
    <div className="content">
      <PageHeader title={data ? `${bureauName(data.bureau)} report` : 'Report'} subtitle={data && `Report date ${shortDate(data.report_date || data.pull_date)}`} back="/reports?view=uploads" />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && (
        <>
          {data.extraction_method === 'ai_verified' && (
            <div className="alert alert-warn" style={{ marginBottom: 12 }}>
              This layout wasn't recognized, so AI read it and every value was checked against the document text. Compare with your PDF.
            </div>
          )}
          <p className="small muted" style={{ marginBottom: 12 }}>
            Exactly what was read from this file. Blank means the report didn't show it — nothing is filled in or guessed.
          </p>
          <div className="section-title">Accounts ({data.accounts.length})</div>
          <div className="list">
            {data.accounts.map(a => (
              <details key={a.id} className="card">
                <summary>{a.creditor_name || 'Unnamed account'}</summary>
                <dl className="compare" style={{ '--cols': 1 }}>
                  {Object.entries(a).filter(([k]) => !SKIP.has(k)).map(([k, v]) => (
                    <FieldRow key={k} name={k} value={MONEY.has(k) ? (v === null ? '—' : money(v)) : (v ?? '—')} />
                  ))}
                </dl>
              </details>
            ))}
          </div>
          <div className="section">
            <div className="section-title">Inquiries ({data.inquiries.length})</div>
            <div className="list">
              {data.inquiries.map(i => (
                <div key={i.id} className="card row-between">
                  <span>{i.creditor_name}</span>
                  <span className="small muted">{i.inquiry_date || '—'} · {i.inquiry_type}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

const FieldRow = ({ name, value }) => (
  <>
    <dt className="label">{humanize(name)}</dt>
    <dd>{String(value)}</dd>
  </>
)
