import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { ACTION_LABELS, Empty, ErrorBox, Loading, PageHeader, SeverityBadge, bureauName, shortDate, useAsync } from '../components/ui'

const FILTERS = [
  ['attention', 'Needs a look'],
  ['negative', 'Negative'],
  ['all', 'All'],
]

export default function Reports() {
  const [params, setParams] = useSearchParams()
  const view = params.get('view') || 'accounts'
  return (
    <div className="content">
      <PageHeader
        title="Reports"
        subtitle="Accounts across all your bureaus"
        actions={<Link to="/reports/upload" className="btn btn-primary btn-sm">Upload</Link>}
      />
      <div className="segmented" role="tablist">
        <button role="tab" aria-selected={view === 'accounts'} className={view === 'accounts' ? 'active' : ''} onClick={() => setParams({ view: 'accounts' })}>Accounts</button>
        <button role="tab" aria-selected={view === 'uploads'} className={view === 'uploads' ? 'active' : ''} onClick={() => setParams({ view: 'uploads' })}>Uploaded reports</button>
      </div>
      <div style={{ marginTop: 12 }}>{view === 'accounts' ? <Accounts /> : <Uploads />}</div>
    </div>
  )
}

function Accounts() {
  const { data, error, loading, reload } = useAsync(() => api.listAccounts(), [])
  const [filter, setFilter] = useState('attention')
  if (loading && !data) return <Loading />
  if (error) return <ErrorBox error={error} onRetry={reload} />
  if (!data.length) return <Empty icon="▤" title="No accounts yet">Upload a report to see your accounts here.</Empty>

  const shown = data.filter(a =>
    filter === 'all' ? true : filter === 'negative' ? a.is_negative : a.findings.some(f => f.severity !== 'difference'),
  )
  return (
    <>
      <div className="segmented" style={{ marginBottom: 12 }}>
        {FILTERS.map(([key, label]) => (
          <button key={key} className={filter === key ? 'active' : ''} onClick={() => setFilter(key)}>{label}</button>
        ))}
      </div>
      {!shown.length && <Empty icon="✓" title="Nothing here">No accounts match this filter.</Empty>}
      <div className="list">
        {shown.map(account => <AccountCard key={account.id} account={account} />)}
      </div>
    </>
  )
}

function AccountCard({ account }) {
  const evaluation = account.evaluation
  return (
    <Link to={`/accounts/${account.id}`} className="card tappable">
      <div className="row-between">
        <div className="grow">
          <div className="card-title truncate">{account.creditor_name}</div>
          <div className="small muted truncate">
            {account.account_type || 'Account'}{account.account_number_last_four ? ` · …${account.account_number_last_four}` : ''}
          </div>
        </div>
        {account.strongest_severity && account.strongest_severity !== 'difference' && <SeverityBadge severity={account.strongest_severity} />}
      </div>
      <div className="row wrap" style={{ marginTop: 8 }}>
        {account.bureaus_reporting.map(b => <span key={b} className="badge">{bureauName(b)}</span>)}
        {account.is_negative && <span className="badge red">Negative</span>}
        {evaluation && (
          <span className={`badge ${evaluation.has_dispute_ground ? 'violet' : 'green'}`}>{ACTION_LABELS[evaluation.recommended_action]}</span>
        )}
      </div>
    </Link>
  )
}

function Uploads() {
  const { data, error, loading, reload } = useAsync(() => api.listReports(), [])
  if (loading && !data) return <Loading />
  if (error) return <ErrorBox error={error} onRetry={reload} />
  if (!data.length) return <Empty icon="▤" title="No reports uploaded">Upload a PDF from any bureau to get started.</Empty>
  return (
    <div className="list">
      {data.map(r => (
        <Link key={r.id} to={`/reports/${r.id}`} className="card tappable row-between">
          <div>
            <div className="card-title">{bureauName(r.bureau)}</div>
            <div className="small muted">Report date {shortDate(r.report_date || r.pull_date)}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="card-title">{r.credit_score ?? '—'}</div>
            {r.extraction_method === 'ai_verified' && <span className="badge amber">AI-read</span>}
          </div>
        </Link>
      ))}
    </div>
  )
}
