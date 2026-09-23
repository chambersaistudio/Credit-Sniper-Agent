import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Empty, ErrorBox, Loading, PageHeader, StatusBadge, bureauName, shortDate, useAsync } from '../components/ui'

const GROUPS = [
  ['you', 'Needs you', c => c.needs_user],
  ['waiting', 'In progress', c => !c.needs_user && c.status !== 'resolved'],
  ['done', 'Resolved', c => c.status === 'resolved'],
]

export default function Cases() {
  const { data, error, loading, reload } = useAsync(() => api.listCases(), [])
  const [group, setGroup] = useState('you')
  const [, , match] = GROUPS.find(g => g[0] === group)

  return (
    <div className="content">
      <PageHeader title="Cases" subtitle="Each case is one dispute to one recipient" />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && (
        <>
          <div className="segmented" style={{ marginBottom: 12 }}>
            {GROUPS.map(([key, label, fn]) => (
              <button key={key} className={group === key ? 'active' : ''} onClick={() => setGroup(key)}>
                {label} ({data.filter(fn).length})
              </button>
            ))}
          </div>
          {!data.length && (
            <Empty icon="⚖" title="No cases yet">
              Open an account, evaluate it, and a case can be opened only where the evidence supports a dispute.
            </Empty>
          )}
          <div className="list">
            {data.filter(match).map(c => (
              <Link key={c.id} to={`/cases/${c.id}`} className="card tappable">
                <div className="row-between">
                  <div className="card-title truncate">{c.creditor_name}</div>
                  <StatusBadge status={c.status} overdue={c.overdue} />
                </div>
                <div className="small muted">
                  {c.recipient_type === 'bureau' ? bureauName(c.recipient_name) : `${c.recipient_name} (creditor)`}
                  {c.response_due_at && !['resolved', 'corrected', 'deleted', 'verified', 'monitoring'].includes(c.status) && ` · due ${shortDate(c.response_due_at)}`}
                </div>
                <div className="small" style={{ marginTop: 6 }}>{c.next_action}</div>
              </Link>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
