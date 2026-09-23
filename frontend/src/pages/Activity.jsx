import { Link } from 'react-router-dom'
import { api } from '../api'
import { Empty, ErrorBox, Loading, PageHeader, bureauName, humanize, useAsync } from '../components/ui'

function describe(item) {
  if (item.type === 'report_uploaded') return { title: `${bureauName(item.bureau)} report uploaded`, to: `/reports/${item.report_id}` }
  const who = `${item.creditor_name} · ${bureauName(item.recipient_name)}`
  if (item.event_type === 'created') return { title: 'Case opened', sub: who, to: `/cases/${item.case_id}` }
  if (item.event_type === 'package_generated') return { title: 'Dispute package built', sub: who, to: `/cases/${item.case_id}` }
  if (item.to_status) return { title: `Case ${humanize(item.to_status)}`, sub: item.detail ? `${who} — ${item.detail}` : who, to: `/cases/${item.case_id}` }
  return { title: humanize(item.event_type), sub: item.detail || who, to: `/cases/${item.case_id}` }
}

export default function Activity() {
  const { data, error, loading, reload } = useAsync(() => api.activity(), [])
  return (
    <div className="content">
      <PageHeader title="Activity" subtitle="Everything that's happened, newest first" />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && !data.length && <Empty icon="◷" title="No activity yet" />}
      {data && data.length > 0 && (
        <ol className="timeline card">
          {data.map((item, i) => {
            const { title, sub, to } = describe(item)
            return (
              <li key={i}>
                <Link to={to}>
                  <div className="small"><strong>{title}</strong>{item.actor && item.actor !== 'user' ? ` · ${item.actor}` : ''}</div>
                  {sub && <div className="small muted">{sub}</div>}
                  <div className="tiny muted">{new Date(item.at).toLocaleString()}</div>
                </Link>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
