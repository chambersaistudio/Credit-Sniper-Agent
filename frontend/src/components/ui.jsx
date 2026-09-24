import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export function useAsync(load, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const run = useCallback(() => {
    setState(s => ({ ...s, loading: true }))
    return load()
      .then(data => setState({ data, error: null, loading: false }))
      .catch(error => setState({ data: null, error: error.message, loading: false }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { run() }, [run])
  return { ...state, reload: run, setData: data => setState(s => ({ ...s, data })) }
}

export function PageHeader({ title, subtitle, back, actions }) {
  const navigate = useNavigate()
  return (
    <header className="page-header">
      {back && (
        <button className="back-btn" aria-label="Back" onClick={() => (window.history.length > 1 ? navigate(-1) : navigate(back))}>
          ‹
        </button>
      )}
      <div className="titles">
        <h1>{title}</h1>
        {subtitle && <div className="subtitle">{subtitle}</div>}
      </div>
      {actions}
    </header>
  )
}

export const Loading = () => <div className="loading"><div className="spinner" /></div>

export const ErrorBox = ({ error, onRetry }) => (
  <div className="alert alert-error row-between">
    <span>{error}</span>
    {onRetry && <button className="btn btn-sm" onClick={onRetry}>Retry</button>}
  </div>
)

export const Empty = ({ icon, title, children }) => (
  <div className="empty">
    <div className="icon">{icon}</div>
    <div style={{ fontWeight: 700, color: 'var(--text-1)' }}>{title}</div>
    {children && <div className="small" style={{ marginTop: 6 }}>{children}</div>}
  </div>
)

export function BottomSheet({ title, onClose, children }) {
  useEffect(() => {
    const onKey = e => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = '' }
  }, [onClose])
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()}>
        <div className="grabber" />
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  )
}

const BUREAU_NAMES = { equifax: 'Equifax', experian: 'Experian', transunion: 'TransUnion', furnisher: 'Creditor (furnisher)' }
export const bureauName = b => BUREAU_NAMES[b] || b

export const money = v => (v === null || v === undefined ? '—' : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)

export function shortDate(iso) {
  if (!iso) return '—'
  // A date-only value like "2026-09-24" is a calendar date, not an instant.
  // `new Date("2026-09-24")` parses as UTC midnight, which renders as the day
  // before in any timezone behind UTC — so build it in local time from parts
  // and keep the calendar date the report actually states.
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  const d = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export const humanize = s => (s || '').replace(/_/g, ' ')

const SEVERITY = {
  supported_dispute_ground: ['red', 'Supported ground'],
  likely_inaccuracy: ['amber', 'Likely inaccuracy'],
  potential_inconsistency: ['sky', 'Potential inconsistency'],
  difference: ['', 'Difference'],
  // The report doesn't print the field. A question to ask, not an error.
  not_disclosed: ['', 'Not disclosed'],
}
export function SeverityBadge({ severity }) {
  const [tone, label] = SEVERITY[severity] || ['', humanize(severity)]
  return <span className={`badge ${tone}`}>{label}</span>
}

const STATUS_TONE = {
  draft: '', awaiting_approval: 'violet', approved: 'violet', submitted: 'sky', delivered: 'sky',
  investigation_active: 'sky', response_received: 'sky', corrected: 'green', deleted: 'green',
  verified: 'amber', more_information_required: 'amber', followup_recommended: 'amber', monitoring: 'green', resolved: '',
}
export function StatusBadge({ status, overdue }) {
  if (overdue) return <span className="badge red">Overdue</span>
  return <span className={`badge ${STATUS_TONE[status] ?? ''}`}>{humanize(status)}</span>
}

export const ACTION_LABELS = {
  dispute_bureau: 'Dispute with the bureau',
  dispute_furnisher: 'Dispute with the creditor',
  dispute_both: 'Dispute with bureau and creditor',
  no_dispute: 'No legitimate dispute ground identified',
  need_more_evidence: 'Needs more evidence',
}

const FIELD_LABELS = {
  account_status: 'Status', payment_status: 'Payment status', balance: 'Balance', past_due_amount: 'Past due',
  credit_limit: 'Credit limit', high_balance: 'High balance', date_opened: 'Date opened', date_closed: 'Date closed',
  date_of_first_delinquency: 'Date of first delinquency', date_last_payment: 'Last payment', date_last_reported: 'Last reported',
}
export const fieldLabel = f => FIELD_LABELS[f] || humanize(f)
