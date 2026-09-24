// Month-by-month payment history: a year-per-row grid like consumer credit
// products show, plus deterministic summary metrics above it.
//
// Two rules the UI must never break: a month the report doesn't cover renders
// as a blank (never as a missed payment), and a derogatory status is shown on
// its own rather than folded into a late-payment percentage.
import { MONTHS, buildGrid, codeInfo, summarize } from '../lib/paymentHistory'

const LEGEND_ORDER = ['ok', '30', '60', '90', '120', '150', '180', 'CO', 'COL', 'VS', 'CLS', 'ND']

export default function PaymentHistory({ entries, dense = false }) {
  const grid = buildGrid(entries)
  if (!grid.length) {
    return <div className="card small muted">This bureau didn't report a month-by-month payment history for this account.</div>
  }
  const summary = summarize(entries)
  const present = new Set(grid.flatMap(row => row.months.filter(Boolean).map(cell => cell.key)))

  return (
    <div className="stack">
      {!dense && <PaymentSummary summary={summary} />}
      <div className="ph-scroll">
        <table className="ph-grid">
          <thead>
            <tr>
              <th className="ph-year-head" scope="col"><span className="sr-only">Year</span></th>
              {MONTHS.map(m => <th key={m} scope="col">{m}</th>)}
            </tr>
          </thead>
          <tbody>
            {grid.map(row => (
              <tr key={row.year}>
                <th className="ph-year" scope="row">{row.year}</th>
                {row.months.map((cell, i) => {
                  const info = cell ? codeInfo(cell.key) : null
                  const label = cell ? `${MONTHS[i]} ${row.year}: ${info.label}` : `${MONTHS[i]} ${row.year}: not reported`
                  return (
                    <td key={i}>
                      <span className={`ph-cell ${info ? `ph-${info.tone}` : 'ph-empty'}`} title={label}>
                        <span aria-hidden="true">{cell ? (cell.raw || info.short) : ''}</span>
                        <span className="sr-only">{label}</span>
                      </span>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Legend present={present} />
    </div>
  )
}

function Legend({ present }) {
  const keys = LEGEND_ORDER.filter(k => present.has(k))
  if (!keys.length) return null
  return (
    <div className="ph-legend">
      {keys.map(k => {
        const info = codeInfo(k)
        return (
          <span key={k} className="ph-legend-item">
            <span className={`ph-cell ph-${info.tone}`} aria-hidden="true">{info.short}</span>
            {info.label}
          </span>
        )
      })}
      <span className="ph-legend-item">
        <span className="ph-cell ph-empty" aria-hidden="true" />
        Not reported
      </span>
    </div>
  )
}

export function PaymentSummary({ summary }) {
  if (!summary || !summary.monthsObserved) return null
  const worst = summary.worst ? codeInfo(summary.worst) : null
  const lateBreakdown = Object.entries(summary.lateCounts).filter(([, n]) => n > 0)

  return (
    <div className="stack">
      <div className="grid-2">
        {/* Only shown when actual reportable months back it up. */}
        {summary.onTimePct !== null && (
          <Stat label="On time" value={`${summary.onTimePct}%`} hint={`of ${summary.monthsReported} reported month${summary.monthsReported === 1 ? '' : 's'}`} />
        )}
        <Stat label="Months reported" value={summary.monthsReported || '—'} hint={summary.monthsObserved !== summary.monthsReported ? `${summary.monthsObserved} observations total` : undefined} />
        {summary.lateTotal > 0 && <Stat label="Late payments" value={summary.lateTotal} />}
        {worst && <Stat label="Most severe" value={worst.short === '✓' ? worst.label : worst.label} />}
      </div>
      {lateBreakdown.length > 0 && (
        <div className="row wrap">
          {lateBreakdown.map(([days, n]) => (
            <span key={days} className="badge amber">{n}×{days} days late</span>
          ))}
        </div>
      )}
      {summary.derogatory.length > 0 && (
        <div className="alert alert-error small">
          Reported separately from late payments:{' '}
          {summary.derogatory.map(d => `${d.label}${d.count > 1 ? ` (${d.count} months)` : ''}`).join(', ')}.
        </div>
      )}
      {summary.mostRecentLate && (
        <div className="small muted">
          Most recent late payment: {MONTHS[summary.mostRecentLate.month - 1]} {summary.mostRecentLate.year}
          {' · '}{codeInfo(summary.mostRecentLate.key).label}
        </div>
      )}
      {summary.firstDerogatory && (
        <div className="small muted">
          First derogatory observation: {MONTHS[summary.firstDerogatory.month - 1]} {summary.firstDerogatory.year}
          {summary.lastDerogatory && summary.lastDerogatory !== summary.firstDerogatory &&
            ` · most recent ${MONTHS[summary.lastDerogatory.month - 1]} ${summary.lastDerogatory.year}`}
        </div>
      )}
    </div>
  )
}

const Stat = ({ label, value, hint }) => (
  <div className="stat">
    <div className="stat-label">{label}</div>
    <div className="stat-value">{value}</div>
    {hint && <div className="tiny muted">{hint}</div>}
  </div>
)
