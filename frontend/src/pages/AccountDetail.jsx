import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import {
  ACTION_LABELS, BottomSheet, Empty, ErrorBox, Loading, PageHeader, SeverityBadge, StatusBadge,
  bureauName, fieldLabel, humanize, money, useAsync,
} from '../components/ui'

const ROWS = [
  ['account_status', 'Status'],
  ['payment_status', 'Payment status'],
  ['balance', 'Balance', money],
  ['past_due_amount', 'Past due', money],
  ['credit_limit', 'Credit limit', money],
  ['high_balance', 'High balance', money],
  ['date_opened', 'Opened'],
  ['date_closed', 'Closed'],
  ['date_of_first_delinquency', 'First delinquency'],
  ['date_last_payment', 'Last payment'],
  ['date_last_reported', 'Last reported'],
  ['remarks', 'Remarks'],
]

export default function AccountDetail() {
  const { id } = useParams()
  const { data, error, loading, reload, setData } = useAsync(() => api.getAccount(id), [id])

  return (
    <div className="content">
      <PageHeader
        title={data?.creditor_name || 'Account'}
        subtitle={data && `${data.account_type || 'Account'}${data.account_number_last_four ? ` · …${data.account_number_last_four}` : ''}`}
        back="/reports"
      />
      {loading && !data && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {data && (
        <div className="stack">
          <Comparison records={data.records} />
          <Findings findings={data.findings} />
          <Evaluation account={data} onEvaluated={evaluation => setData({ ...data, evaluation })} onCaseOpened={reload} />
          {data.cases.length > 0 && (
            <div className="section">
              <div className="section-title">Cases</div>
              <div className="list">
                {data.cases.map(c => (
                  <Link key={c.id} to={`/cases/${c.id}`} className="card tappable row-between">
                    <span>{bureauName(c.recipient_type === 'bureau' ? c.recipient_name : 'furnisher')}</span>
                    <StatusBadge status={c.status} />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Comparison({ records }) {
  const bureaus = [...records].sort((a, b) => a.bureau.localeCompare(b.bureau))
  const rows = ROWS.filter(([key]) => bureaus.some(r => r[key] !== null && r[key] !== undefined && r[key] !== ''))
  return (
    <div className="card">
      <div className="section-title">As each bureau reports it</div>
      <div className="compare" style={{ '--cols': bureaus.length }}>
        <div className="head" />
        {bureaus.map(r => <div key={r.bureau} className="head">{bureauName(r.bureau)}</div>)}
        {rows.map(([key, label, fmt]) => {
          const values = bureaus.map(r => r[key] ?? null)
          const differs = new Set(values.filter(v => v !== null).map(String)).size > 1
          return [
            <div key={`${key}-l`} className="label">{label}</div>,
            ...values.map((v, i) => (
              <div key={`${key}-${i}`} className={differs ? 'diff' : ''}>{v === null ? '—' : fmt ? fmt(v) : humanize(String(v))}</div>
            )),
          ]
        })}
      </div>
      {bureaus.length === 1 && (
        <p className="small muted" style={{ marginTop: 8 }}>Only {bureauName(bureaus[0].bureau)} reports this account so far. Upload your other reports to compare.</p>
      )}
    </div>
  )
}

function Findings({ findings }) {
  const shown = findings.filter(f => f.severity !== 'difference')
  return (
    <div className="section">
      <div className="section-title">Findings</div>
      {!shown.length ? (
        <div className="card small muted">No inconsistencies or reporting problems were found in what the bureaus report.</div>
      ) : (
        <div className="list">
          {shown.map((f, i) => (
            <div key={i} className={`finding sev-${f.severity}`}>
              <div className="row-between">
                <strong className="small">{fieldLabel(f.field)}{f.bureau ? ` · ${bureauName(f.bureau)}` : ''}</strong>
                <SeverityBadge severity={f.severity} />
              </div>
              <div className="small" style={{ marginTop: 4 }}>{f.rationale}</div>
            </div>
          ))}
        </div>
      )}
      <p className="tiny muted" style={{ marginTop: 8 }}>
        Findings come from fixed rules, not AI. A difference between bureaus isn't automatically an error.
      </p>
    </div>
  )
}

function Evaluation({ account, onEvaluated, onCaseOpened }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const evaluation = account.evaluation

  const evaluate = async () => {
    setBusy(true); setError(null)
    try { onEvaluated(await api.evaluateAccount(account.id)) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="section">
      <div className="section-title">Evaluation</div>
      {error && <div className="alert alert-error" style={{ marginBottom: 8 }}>{error}</div>}
      {!evaluation ? (
        <div className="card stack">
          <p className="small">
            Ask whether the evidence supports a dispute. The AI sees only this account's bureau data and findings — not your name or
            other accounts — and must tie any dispute ground to a specific finding.
          </p>
          <button className="btn btn-primary btn-block" disabled={busy} onClick={evaluate}>
            {busy ? <><span className="spinner" /> Evaluating…</> : 'Evaluate this account'}
          </button>
        </div>
      ) : (
        <EvaluationResult evaluation={evaluation} account={account} busy={busy} onReevaluate={evaluate} onCaseOpened={onCaseOpened} />
      )}
    </div>
  )
}

function EvaluationResult({ evaluation, account, busy, onReevaluate, onCaseOpened }) {
  const [furnisherSheet, setFurnisherSheet] = useState(false)
  const [error, setError] = useState(null)
  const navigate = useNavigate()
  const openRecipients = new Set(account.cases.filter(c => c.status !== 'resolved').map(c => (c.recipient_type === 'bureau' ? c.recipient_name : 'furnisher')))

  const open = async (recipient, furnisher) => {
    setError(null)
    try {
      const created = await api.openCase([evaluation.id], recipient, furnisher)
      onCaseOpened()
      navigate(`/cases/${created.id}`)
    } catch (e) { setError(e.message) }
  }

  return (
    <div className="card stack">
      <div className="row-between">
        <strong style={{ color: evaluation.has_dispute_ground ? 'var(--violet-light)' : 'var(--emerald)' }}>
          {ACTION_LABELS[evaluation.recommended_action]}
        </strong>
        <span className="badge">{Math.round(evaluation.confidence * 100)}% confidence</span>
      </div>
      <p className="small">{evaluation.reasoning}</p>

      {evaluation.legal_basis.length > 0 && (
        <details>
          <summary className="small">Legal basis ({evaluation.legal_basis.length})</summary>
          <div className="list">
            {evaluation.legal_basis.map(l => (
              <div key={l.id} className="small"><strong>{l.citation}</strong> — {l.applies_because || l.title}</div>
            ))}
          </div>
        </details>
      )}
      {evaluation.additional_evidence_needed.length > 0 && (
        <div className="alert alert-info small">
          More evidence would help: {evaluation.additional_evidence_needed.join('; ')}
        </div>
      )}
      {evaluation.requested_remedy && <div className="small"><span className="muted">Requested fix: </span>{evaluation.requested_remedy}</div>}

      {error && <div className="alert alert-error">{error}</div>}
      {evaluation.has_dispute_ground && (
        <div className="stack">
          <div className="section-title" style={{ margin: 0 }}>Open a case with</div>
          {evaluation.recipients.map(r => (
            <button key={r} className="btn btn-primary btn-block" disabled={openRecipients.has(r)}
              onClick={() => (r === 'furnisher' ? setFurnisherSheet(true) : open(r))}>
              {bureauName(r)}{openRecipients.has(r) ? ' — case open' : ''}
            </button>
          ))}
        </div>
      )}
      {!evaluation.has_dispute_ground && evaluation.recommended_action === 'no_dispute' && (
        <Empty icon="✓" title="Nothing to dispute here">Not every negative item is inaccurate. This one looks consistently reported.</Empty>
      )}
      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onReevaluate}>{busy ? 'Evaluating…' : 'Re-evaluate'}</button>

      {furnisherSheet && (
        <FurnisherSheet defaultName={account.creditor_name} onClose={() => setFurnisherSheet(false)}
          onSubmit={async f => { setFurnisherSheet(false); await open('furnisher', f) }} />
      )}
    </div>
  )
}

function FurnisherSheet({ defaultName, onClose, onSubmit }) {
  const [name, setName] = useState(defaultName)
  const [address, setAddress] = useState('')
  return (
    <BottomSheet title="Dispute with the creditor" onClose={onClose}>
      <div className="stack">
        <div className="field">
          <label htmlFor="fname">Creditor name</label>
          <input id="fname" value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="faddr">Dispute address</label>
          <textarea id="faddr" value={address} onChange={e => setAddress(e.target.value)} placeholder="Use the address the creditor designates for disputes" />
          <span className="hint">Often on your statements or credit report. You can add it later, before approving.</span>
        </div>
        <button className="btn btn-primary btn-block" disabled={!name.trim()} onClick={() => onSubmit({ name: name.trim(), address: address.trim() || undefined })}>
          Open case
        </button>
      </div>
    </BottomSheet>
  )
}
