import { Link } from 'react-router-dom'
import { api } from '../api'
import { Empty, ErrorBox, Loading, PageHeader, StatusBadge, bureauName, humanize, shortDate, useAsync } from '../components/ui'

const BUREAUS = ['equifax', 'experian', 'transunion']

function scoreTone(score) {
  if (!score) return 'var(--text-3)'
  if (score >= 740) return 'var(--emerald)'
  if (score >= 670) return 'var(--sky)'
  if (score >= 580) return 'var(--amber)'
  return 'var(--rose)'
}

export default function Home() {
  const { data, error, loading, reload } = useAsync(() => api.dashboard(), [])

  return (
    <div className="content">
      <PageHeader title="Home" subtitle="Your credit profile at a glance" />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && !data.has_reports && (
        <div className="card">
          <Empty icon="▤" title="Start with your credit reports">
            Upload your Equifax, Experian, and TransUnion reports. Credit Sniper lines up each account across bureaus and shows
            what doesn't match.
          </Empty>
          <Link to="/reports/upload" className="btn btn-primary btn-block">Upload a report</Link>
        </div>
      )}
      {data && data.has_reports && <Dashboard data={data} />}
    </div>
  )
}

function Dashboard({ data }) {
  const { accounts, cases, findings_by_severity: sev } = data
  const strong = (sev.supported_dispute_ground || 0) + (sev.likely_inaccuracy || 0)
  return (
    <>
      <div className="grid-3">
        {BUREAUS.map(b => {
          const s = data.scores[b]
          return (
            <div className="stat" key={b}>
              <div className="stat-label">{bureauName(b)}</div>
              <div className="score-value" style={{ color: scoreTone(s?.score) }}>{s?.score ?? '—'}</div>
              <div className="tiny muted">{s ? shortDate(s.report_date) : 'No report'}</div>
            </div>
          )
        })}
      </div>

      {data.waiting_on_you.length > 0 && (
        <div className="section">
          <div className="section-title">Waiting on you</div>
          <div className="list">
            {data.waiting_on_you.map(item => (
              <Link key={item.case_id} to={`/cases/${item.case_id}`} className="card tappable">
                <div className="row-between">
                  <div className="card-title truncate">{item.creditor_name}</div>
                  <StatusBadge status={item.status} />
                </div>
                <div className="small muted">{bureauName(item.recipient_name)} · {item.next_action}</div>
              </Link>
            ))}
          </div>
        </div>
      )}

      <div className="section">
        <div className="section-title">Accounts</div>
        <div className="grid-2 grid-md-4">
          <Stat label="Accounts" value={accounts.total} />
          <Stat label="Negative items" value={accounts.negative} />
          <Stat label="Strong findings" value={strong} tone={strong ? 'var(--amber)' : undefined} />
          <Stat label="Dispute grounds" value={accounts.dispute_ground} tone={accounts.dispute_ground ? 'var(--rose)' : undefined} />
        </div>
        <Link to="/reports" className="btn btn-block" style={{ marginTop: 12 }}>Review accounts</Link>
      </div>

      <div className="section">
        <div className="section-title">Cases</div>
        <div className="grid-2 grid-md-4">
          <Stat label="Open" value={cases.open} />
          <Stat label="Awaiting response" value={cases.awaiting_response} />
          <Stat label="Overdue" value={cases.overdue} tone={cases.overdue ? 'var(--rose)' : undefined} />
          <Stat label="Resolved" value={cases.resolved} />
        </div>
      </div>

      {Object.keys(sev).length > 0 && (
        <p className="small muted" style={{ marginTop: 16 }}>
          Findings are deterministic checks — {Object.entries(sev).map(([k, v]) => `${v} ${humanize(k)}`).join(', ')}.
          A finding isn't a dispute until an evaluation says the evidence supports one.
        </p>
      )}
    </>
  )
}

const Stat = ({ label, value, tone }) => (
  <div className="stat">
    <div className="stat-label">{label}</div>
    <div className="stat-value" style={tone ? { color: tone } : undefined}>{value}</div>
  </div>
)
