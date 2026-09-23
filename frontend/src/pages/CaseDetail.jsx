import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { BottomSheet, ErrorBox, Loading, PageHeader, StatusBadge, bureauName, fieldLabel, humanize, shortDate, useAsync } from '../components/ui'

const CHANNELS = [
  ['certified_mail', 'Certified mail', 'Return receipt proves when it was received'],
  ['manual_mail', 'Regular mail', 'No proof of delivery date'],
  ['portal', 'Bureau website', 'Submitted through the online dispute form'],
  ['email', 'Email', ''],
]
const OUTCOMES = [
  ['deleted', 'Deleted', 'The item was removed'],
  ['corrected', 'Corrected', 'The item was updated'],
  ['verified', 'Verified', 'They say it’s accurate as reported'],
  ['more_information_required', 'More information needed', 'They asked for something else'],
]
const today = () => new Date().toISOString().slice(0, 10)
const toIso = d => (d ? new Date(`${d}T12:00:00`).toISOString() : undefined)

export default function CaseDetail() {
  const { id } = useParams()
  const { data, error, loading, reload, setData } = useAsync(() => api.getCase(id), [id])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [sheet, setSheet] = useState(null)

  const act = async fn => {
    setBusy(true); setActionError(null)
    try { setData(await fn()); setSheet(null) } catch (e) { setActionError(e.message) } finally { setBusy(false) }
  }

  if (!data) {
    return (
      <div className="content">
        <PageHeader title="Case" back="/cases" />
        {loading && <Loading />}
        {error && <ErrorBox error={error} onRetry={reload} />}
      </div>
    )
  }

  const c = data
  const recipient = c.recipient_type === 'bureau' ? bureauName(c.recipient_name) : c.recipient_name
  const actions = actionsFor(c, { act, setSheet, busy })

  return (
    <div className={`content${actions ? ' has-action-bar' : ''}`}>
      <PageHeader title={c.creditor_name} subtitle={`Dispute with ${recipient}`} back="/cases"
        actions={<StatusBadge status={c.status} overdue={c.overdue} />} />

      <div className="stack">
        <div className={`alert ${c.overdue ? 'alert-error' : 'alert-info'}`}>{c.next_action}</div>
        {actionError && <div className="alert alert-error">{actionError}</div>}

        {(c.submitted_at || c.response_due_at) && (
          <div className="card grid-2">
            <Info label="Sent" value={c.submitted_at ? `${shortDate(c.submitted_at)} · ${humanize(c.submission_channel)}` : '—'} />
            <Info label="Received by them" value={shortDate(c.delivered_at)} />
            <Info label="Response due" value={c.response_due_at ? `${shortDate(c.response_due_at)}${c.deadline_basis === 'submitted_estimate' ? ' (est.)' : ''}` : '—'} />
            <Info label="Tracking" value={c.tracking_number || '—'} />
          </div>
        )}

        {c.recipient_type === 'furnisher' && ['draft', 'awaiting_approval'].includes(c.status) && (
          <FurnisherAddress c={c} onSave={address => act(() => api.setFurnisherAddress(c.id, address))} busy={busy} />
        )}

        <Package c={c} />

        <div className="section">
          <div className="section-title">Why this dispute</div>
          <div className="list">
            {c.claims.map(claim => (
              <div key={claim.id} className="card stack">
                <p className="small">{claim.reasoning}</p>
                {claim.evidence.map(e => (
                  <div key={e.id} className={`finding sev-${e.data?.severity}`}>
                    <div className="small"><strong>{fieldLabel(e.data?.field)}</strong>{e.bureau ? ` · ${bureauName(e.bureau)}` : ''}</div>
                    <div className="small">{e.description}</div>
                  </div>
                ))}
                <Link to={`/accounts/${c.canonical_account_id}`} className="small" style={{ color: 'var(--violet-light)' }}>View account →</Link>
              </div>
            ))}
          </div>
        </div>

        <div className="section">
          <div className="section-title">History</div>
          <ol className="timeline card">
            {c.events.map((e, i) => (
              <li key={i}>
                <div className="small"><strong>{e.to_status ? humanize(e.to_status) : humanize(e.event_type)}</strong>{e.actor !== 'user' ? ` · ${e.actor}` : ''}</div>
                {e.detail && <div className="small muted">{e.detail}</div>}
                <div className="tiny muted">{new Date(e.created_at).toLocaleString()}</div>
              </li>
            ))}
          </ol>
        </div>
      </div>

      {actions && <div className="action-bar">{actions}</div>}

      {sheet === 'sent' && <SentSheet busy={busy} onClose={() => setSheet(null)} onSubmit={body => act(() => api.markSubmitted(c.id, body))} />}
      {sheet === 'delivered' && <DateSheet title="When did they receive it?" hint="The return receipt or tracking shows this. The investigation clock starts here." busy={busy}
        onClose={() => setSheet(null)} onSubmit={d => act(() => api.markDelivered(c.id, { delivered_at: toIso(d) }))} />}
      {sheet === 'response' && <ResponseSheet busy={busy} onClose={() => setSheet(null)} onSubmit={body => act(() => api.recordResponse(c.id, body))} />}
    </div>
  )
}

function actionsFor(c, { act, setSheet, busy }) {
  const move = (target, label, primary) => (
    <button key={target} className={`btn ${primary ? 'btn-primary' : ''}`} disabled={busy} onClick={() => act(() => api.moveCase(c.id, target))}>{label}</button>
  )
  switch (c.status) {
    case 'draft':
      return <button className="btn btn-primary" disabled={busy} onClick={() => act(() => api.generatePackage(c.id))}>Build dispute package</button>
    case 'awaiting_approval':
      return <>
        <button className="btn" disabled={busy} onClick={() => act(() => api.generatePackage(c.id))}>Rebuild</button>
        <button className="btn btn-success" disabled={busy || !c.package?.ready} onClick={() => act(() => api.approveCase(c.id))}>Approve</button>
      </>
    case 'approved':
      return <>
        <button className="btn" onClick={() => api.openPackagePdf(c.id)}>Download PDF</button>
        <button className="btn btn-primary" onClick={() => setSheet('sent')}>I sent it</button>
      </>
    case 'submitted':
      return <>
        <button className="btn" onClick={() => setSheet('delivered')}>They received it</button>
        <button className="btn btn-primary" onClick={() => setSheet('response')}>Record response</button>
      </>
    case 'delivered':
    case 'investigation_active':
    case 'response_received':
      return <button className="btn btn-primary" onClick={() => setSheet('response')}>Record response</button>
    case 'corrected':
    case 'deleted':
      return <>{move('monitoring', 'Keep monitoring')}{move('resolved', 'Resolve', true)}</>
    case 'verified':
      return <>{move('followup_recommended', 'Plan a follow-up')}{move('resolved', 'Accept & resolve', true)}</>
    case 'more_information_required':
    case 'followup_recommended':
      return <>{move('resolved', 'Resolve')}{move('draft', 'Revise package', true)}</>
    case 'monitoring':
      return <>{move('followup_recommended', 'It came back')}{move('resolved', 'Resolve', true)}</>
    default:
      return null
  }
}

const Info = ({ label, value }) => (
  <div><div className="tiny muted">{label}</div><div className="small">{value}</div></div>
)

function Package({ c }) {
  const p = c.package
  if (!p) return null
  return (
    <div className="section">
      <div className="section-title">Dispute package</div>
      <div className="card stack">
        {p.warnings.map(w => <div key={w} className={`alert ${p.ready ? 'alert-info' : 'alert-warn'} small`}>{w}</div>)}
        <div className="small"><span className="muted">To: </span>{p.recipient}</div>
        <div className="small"><span className="muted">Re: </span>{p.subject}</div>
        <details>
          <summary className="small">Read the letter</summary>
          <div className="letter">{p.body}</div>
        </details>
        <details>
          <summary className="small">What to enclose ({p.enclosures.length})</summary>
          <ul className="small" style={{ paddingLeft: 20 }}>{p.enclosures.map(e => <li key={e}>{e}</li>)}</ul>
        </details>
        <button className="btn btn-sm" onClick={() => api.openPackagePdf(c.id)}>Download PDF</button>
      </div>
    </div>
  )
}

function FurnisherAddress({ c, onSave, busy }) {
  const [address, setAddress] = useState(c.package?.recipient_address || '')
  return (
    <div className="card stack">
      <div className="field">
        <label htmlFor="addr">Creditor's dispute address</label>
        <textarea id="addr" value={address} onChange={e => setAddress(e.target.value)} />
        <span className="hint">Direct disputes must go to the address the creditor designates for them.</span>
      </div>
      <button className="btn btn-sm" disabled={busy || !address.trim()} onClick={() => onSave(address)}>Save address</button>
      <span className="tiny muted">Rebuild the package after saving.</span>
    </div>
  )
}

function SentSheet({ busy, onClose, onSubmit }) {
  const [channel, setChannel] = useState('certified_mail')
  const [date, setDate] = useState(today())
  const [tracking, setTracking] = useState('')
  return (
    <BottomSheet title="How did you send it?" onClose={onClose}>
      <div className="stack">
        <div className="choice-list">
          {CHANNELS.map(([value, label, hint]) => (
            <button key={value} className={`choice${channel === value ? ' selected' : ''}`} onClick={() => setChannel(value)}>
              <div><div style={{ fontWeight: 600 }}>{label}</div>{hint && <div className="tiny muted">{hint}</div>}</div>
            </button>
          ))}
        </div>
        <div className="field"><label htmlFor="sent">Date sent</label><input id="sent" type="date" max={today()} value={date} onChange={e => setDate(e.target.value)} /></div>
        {channel === 'certified_mail' && (
          <div className="field"><label htmlFor="trk">Tracking number</label><input id="trk" inputMode="numeric" value={tracking} onChange={e => setTracking(e.target.value)} /></div>
        )}
        <button className="btn btn-primary btn-block" disabled={busy || !date}
          onClick={() => onSubmit({ channel, submitted_at: toIso(date), tracking_number: tracking || undefined })}>Save</button>
      </div>
    </BottomSheet>
  )
}

function DateSheet({ title, hint, busy, onClose, onSubmit }) {
  const [date, setDate] = useState(today())
  return (
    <BottomSheet title={title} onClose={onClose}>
      <div className="stack">
        <div className="field"><label htmlFor="d">Date</label><input id="d" type="date" max={today()} value={date} onChange={e => setDate(e.target.value)} />{hint && <span className="hint">{hint}</span>}</div>
        <button className="btn btn-primary btn-block" disabled={busy || !date} onClick={() => onSubmit(date)}>Save</button>
      </div>
    </BottomSheet>
  )
}

function ResponseSheet({ busy, onClose, onSubmit }) {
  const [outcome, setOutcome] = useState(null)
  const [date, setDate] = useState(today())
  const [detail, setDetail] = useState('')
  return (
    <BottomSheet title="What did they say?" onClose={onClose}>
      <div className="stack">
        <div className="choice-list">
          {OUTCOMES.map(([value, label, hint]) => (
            <button key={value} className={`choice${outcome === value ? ' selected' : ''}`} onClick={() => setOutcome(value)}>
              <div><div style={{ fontWeight: 600 }}>{label}</div><div className="tiny muted">{hint}</div></div>
            </button>
          ))}
        </div>
        <div className="field"><label htmlFor="rd">Date received</label><input id="rd" type="date" max={today()} value={date} onChange={e => setDate(e.target.value)} /></div>
        <div className="field"><label htmlFor="det">Notes (optional)</label><textarea id="det" value={detail} onChange={e => setDetail(e.target.value)} placeholder="What the letter said" /></div>
        <button className="btn btn-primary btn-block" disabled={busy || !outcome}
          onClick={() => onSubmit({ outcome, received_at: toIso(date), detail })}>Save</button>
      </div>
    </BottomSheet>
  )
}
