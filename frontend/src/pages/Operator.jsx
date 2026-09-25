import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { ErrorBox, Loading, PageHeader, useAsync } from '../components/ui'
import {
  BENCHMARK_CONFIGS, DEFAULT_TRUTH_LABEL, benchmarkBlockedReason, benchmarkRequest,
  confirmationFor, isJobRunning, jobSummary, parseTruthText, paymentByAccount,
  paymentMisses, pct, shortFingerprint, truthCounts, truthFor, truthStatus, truthToText,
  usd,
} from '../lib/operator'

// Internal tooling: reachable by URL, absent from navigation, and it spends
// real money. Every paid action is confirmed, and the expensive one says so
// in its own words rather than behind one generic prompt.

export default function Operator() {
  const [reportId, setReportId] = useState(null)
  const reports = useAsync(() => api.operatorReports(), [])

  if (reports.loading && !reports.data) return <div className="content"><Loading /></div>
  if (reports.error) {
    return (
      <div className="content">
        <PageHeader title="Operator" back="/" />
        <ErrorBox error={reports.error} onRetry={reports.reload} />
      </div>
    )
  }

  return (
    <div className="content">
      <PageHeader title="Operator" subtitle="Internal tooling — not for consumers" back="/" />
      {!reportId
        ? <ReportList reports={reports.data || []} onPick={setReportId} />
        : <ReportDetail reportId={reportId} onBack={() => setReportId(null)} />}
    </div>
  )
}

function ReportList({ reports, onPick }) {
  if (!reports.length) return <div className="card">No reports yet.</div>
  return (
    <div className="list">
      {reports.map(r => (
        <button key={r.report_id} className="card tappable text-left" onClick={() => onPick(r.report_id)}>
          <div className="row-between">
            <div className="grow">
              <div className="card-title truncate">{r.bureau || 'unknown'}</div>
              <div className="small muted truncate">
                {r.extraction_status}
                {r.processing ? ' · reading…' : ''}
                {r.last_processing_error_class ? ` · ${r.last_processing_error_class}` : ''}
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              {r.has_index && <span className="badge green">index</span>}{' '}
              {r.banked_batches?.length > 0 && (
                <span className="badge amber">{r.banked_batches.length} batch</span>
              )}
            </div>
          </div>
        </button>
      ))}
    </div>
  )
}

function ReportDetail({ reportId, onBack }) {
  const plan = useAsync(() => api.operatorBatchPlan(reportId).catch(() => null), [reportId])
  const checkpoint = useAsync(() => api.operatorCheckpoint(reportId), [reportId])
  const [jobs, setJobs] = useState([])
  const [batchId, setBatchId] = useState('b0')
  const [truths, setTruths] = useState([])
  const [pending, setPending] = useState(null)
  const [error, setError] = useState(null)

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.operatorJobs(reportId))
    } catch (e) {
      setError(e.message)
    }
  }, [reportId])

  // Status only — counts and whether it is verified. The values themselves are
  // fetched only when someone opens the editor to correct them.
  const refreshTruths = useCallback(async () => {
    try {
      setTruths(await api.operatorTruthList(reportId))
    } catch (e) {
      setError(e.message)
    }
  }, [reportId])

  useEffect(() => { refreshJobs() }, [refreshJobs])
  useEffect(() => { refreshTruths() }, [refreshTruths])

  // While any job is in flight, keep the list fresh without the operator
  // needing to pull to refresh on a phone.
  useEffect(() => {
    if (!jobs.some(j => isJobRunning(j.status))) return undefined
    const timer = setTimeout(refreshJobs, 3000)
    return () => clearTimeout(timer)
  }, [jobs, refreshJobs])

  const truth = truthFor(truths, batchId)
  const blocked = benchmarkBlockedReason(truth)

  // The run sends a reference, not the account values: the truth is already
  // stored server-side and the job records which version it was scored
  // against, so a correction cannot silently change the question.
  const run = async (config) => {
    setError(null)
    try {
      await api.queueBenchmark(benchmarkRequest({
        reportId, batchId, config, truthLabel: DEFAULT_TRUTH_LABEL,
      }))
      setPending(null)
      await refreshJobs()
    } catch (e) {
      setError(e.message)
      setPending(null)
    }
  }

  return (
    <div className="stack">
      <button className="btn btn-ghost btn-sm" onClick={onBack}>‹ All reports</button>

      <div className="card stack">
        <div className="card-title">Extraction</div>
        {checkpoint.data && <CheckpointSummary checkpoint={checkpoint.data} />}
      </div>

      {plan.data ? (
        <div className="card stack">
          <div className="row-between">
            <div className="card-title">Batch plan</div>
            <span className="small muted">
              {plan.data.pages_sent}/{plan.data.pages_if_whole_document} pages
              {' '}({plan.data.reduction_pct}% saved)
            </span>
          </div>
          <div className="segmented">
            {plan.data.batches.map(b => (
              <button key={b.batch_id} className={batchId === b.batch_id ? 'active' : ''}
                onClick={() => setBatchId(b.batch_id)}>
                {b.batch_id}{b.banked ? ' ✓' : ''}
              </button>
            ))}
          </div>
          {plan.data.batches.filter(b => b.batch_id === batchId).map(b => (
            <div key={b.batch_id} className="small muted">
              <div>pages {JSON.stringify(b.pages)}</div>
              {b.names.map(n => <div key={n}>· {n}</div>)}
            </div>
          ))}
        </div>
      ) : (
        <div className="alert alert-warn">
          No banked Stage-1 index for this report, so there is no batch plan yet.
        </div>
      )}

      {plan.data && (
        <TruthPanel reportId={reportId} batchId={batchId} summary={truth}
          banked={Boolean(checkpoint.data?.batches?.[batchId])}
          onChanged={refreshTruths} />
      )}

      {plan.data && (
        <div className="card stack">
          <div className="card-title">Benchmark {batchId}</div>
          <div className="grid-2">
            {BENCHMARK_CONFIGS.map(c => (
              <button key={c.config} disabled={Boolean(blocked)}
                className={`btn btn-sm${c.expensive ? '' : ' btn-primary'}`}
                onClick={() => setPending(c.config)}>
                Run {c.label}
              </button>
            ))}
          </div>
          {blocked
            ? <p className="tiny muted">{blocked}</p>
            : <p className="tiny muted">
                Scored against the stored truth ({shortFingerprint(truth)}). One paid model
                call per run. Nothing is banked and no production default changes.
              </p>}
        </div>
      )}

      {error && <div className="alert alert-error">{error}</div>}
      {pending && (
        <ConfirmPaid config={pending} batchId={batchId}
          onCancel={() => setPending(null)} onConfirm={() => run(pending)} />
      )}

      <div className="card stack">
        <div className="card-title">Operator jobs</div>
        {!jobs.length && <div className="small muted">No jobs for this report yet.</div>}
        {jobs.map(job => <JobCard key={job.job_id} job={job} />)}
      </div>
    </div>
  )
}

/**
 * Benchmark truth for one batch: stored once, corrected in place, referenced
 * by every run.
 *
 * The point of this panel is that nobody types account data twice. Truth is
 * either drafted from the banked extraction and corrected, or entered once;
 * after that, running Luna and then Terra is two taps, not two pastes.
 *
 * Verification is a separate act from saving, and the benchmark requires it. A
 * draft prefilled from a model's own extraction reads exactly like truth and
 * is not: scoring that model against it would measure self-consistency.
 */
function TruthPanel({ reportId, batchId, summary, banked, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const status = truthStatus(summary)

  // A new batch selection is a different truth; never carry an open editor
  // (and its values) across.
  useEffect(() => {
    setEditing(false)
    setText('')
    setError(null)
  }, [batchId, reportId])

  const act = async (fn) => {
    setBusy(true)
    setError(null)
    try {
      const result = await fn()
      await onChanged()
      return result
    } catch (e) {
      setError(e.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  const openEditor = async () => {
    if (!summary) {
      setText(TRUTH_TEMPLATE)
      setEditing(true)
      return
    }
    const stored = await act(() => api.operatorTruth(reportId, batchId, summary.label))
    if (stored) {
      setText(truthToText(stored))
      setEditing(true)
    }
  }

  const draft = async () => {
    const drafted = await act(() => api.draftOperatorTruth(reportId, batchId))
    if (drafted) {
      setText(truthToText(drafted))
      setEditing(true)
    }
  }

  const save = async () => {
    const parsed = parseTruthText(text)
    if (parsed.error) {
      setError(parsed.error)
      return
    }
    // Saved unverified on purpose: confirming the values is the next,
    // deliberate step, and a correction invalidates the old confirmation.
    const saved = await act(() => api.saveOperatorTruth(reportId, batchId, {
      accounts: parsed.accounts, label: DEFAULT_TRUTH_LABEL, verified: false,
    }))
    if (saved) {
      setEditing(false)
      setText('')
    }
  }

  return (
    <div className="card stack">
      <div className="row-between">
        <div className="card-title">Truth for {batchId}</div>
        <span className={`badge ${status.tone}`}>{status.label}</span>
      </div>

      <div className="small muted">
        {summary
          ? <>{truthCounts(summary)} · {shortFingerprint(summary)}</>
          : 'Nothing stored for this batch yet.'}
      </div>
      <p className="tiny muted" style={{ margin: 0 }}>{status.detail}</p>
      {summary?.note && <p className="tiny muted" style={{ margin: 0 }}>{summary.note}</p>}

      {error && <div className="alert alert-error">{error}</div>}

      {!editing ? (
        <div className="grid-2">
          <button className="btn btn-sm" disabled={busy} onClick={openEditor}>
            {summary ? 'Review & correct' : 'Enter truth'}
          </button>
          {banked && !summary && (
            <button className="btn btn-sm" disabled={busy} onClick={draft}>
              Draft from banked
            </button>
          )}
          {summary && !summary.verified && (
            <button className="btn btn-sm btn-primary" disabled={busy}
              onClick={() => act(() => api.verifyOperatorTruth(reportId, batchId, true,
                                                              summary.label))}>
              Verify
            </button>
          )}
          {summary?.verified && (
            <button className="btn btn-sm btn-ghost" disabled={busy}
              onClick={() => act(() => api.verifyOperatorTruth(reportId, batchId, false,
                                                              summary.label))}>
              Un-verify
            </button>
          )}
        </div>
      ) : (
        <div className="stack">
          <div className="field">
            <label htmlFor="truth-editor">Values as the document prints them</label>
            <textarea id="truth-editor" rows={12} value={text} spellCheck={false}
              autoCapitalize="none" autoCorrect="off"
              onChange={e => setText(e.target.value)} />
            <span className="hint">
              Each field in full — Experian prints "Voluntarily surrendered. $7,684 past due
              as of Sep 2026." and half of that scores a correct read as a miss. Saved
              server-side only; never committed to Git.
            </span>
          </div>
          <div className="grid-2">
            <button className="btn btn-sm" disabled={busy}
              onClick={() => { setEditing(false); setText(''); setError(null) }}>
              Cancel
            </button>
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={save}>
              Save (unverified)
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// The shape to fill in, for a batch with nothing banked to draft from.
const TRUTH_TEMPLATE = `{
  "accounts": [
    {
      "creditor_name": "",
      "account_number": "",
      "status_raw": "",
      "balance": "",
      "payment_history": { "2026-08": "" }
    }
  ]
}`

function CheckpointSummary({ checkpoint }) {
  const index = checkpoint.index
  return (
    <div className="small muted stack">
      {index
        ? <div>Index: {index.tradelines} tradelines, {index.total_pages} pages, {index.model}</div>
        : <div>No banked index.</div>}
      {checkpoint.batches
        ? Object.entries(checkpoint.batches).map(([id, b]) => (
            <div key={id}>Batch {id}: {b.accounts} accounts, {b.model}</div>
          ))
        : <div>No banked batches.</div>}
      {checkpoint.last_failure && (
        <div>Last failure: {checkpoint.last_failure.error_class} on the {checkpoint.last_failure.pass} pass</div>
      )}
    </div>
  )
}

function ConfirmPaid({ config, batchId, onCancel, onConfirm }) {
  const prompt = confirmationFor(config, batchId)
  return (
    <div className={`alert alert-${prompt.requiresAck ? 'error' : 'warn'}`} role="alertdialog">
      <strong>{prompt.title}</strong>
      <p className="small" style={{ margin: '6px 0' }}>{prompt.body}</p>
      <div className="grid-2">
        <button className="btn btn-sm" onClick={onCancel}>Cancel</button>
        <button className="btn btn-sm btn-primary" onClick={onConfirm}>{prompt.confirmLabel}</button>
      </div>
    </div>
  )
}

function JobCard({ job }) {
  const [open, setOpen] = useState(false)
  const result = job.result
  return (
    <div className="card" style={{ padding: 12 }}>
      <button className="row-between text-left grow" style={{ width: '100%' }}
        onClick={() => setOpen(o => !o)}>
        <div className="grow">
          <div className="card-title truncate">
            {job.operation} {job.request?.config ? `· ${job.request.config}` : ''}
          </div>
          <div className="small muted truncate">{jobSummary(job)}</div>
        </div>
        <span className={`badge ${job.status === 'succeeded' ? 'green'
          : job.status === 'failed' ? 'red' : 'amber'}`}>{job.status}</span>
      </button>

      {open && (
        <div className="small stack" style={{ marginTop: 10 }}>
          <div className="grid-2">
            <Stat label="Cost" value={usd(job.estimated_cost_usd)} />
            <Stat label="Model calls" value={`${job.model_calls_made}/${job.max_model_calls}`} />
          </div>
          {job.error && <div className="alert alert-error">{job.error.message}</div>}
          {result && (
            <>
              <div className="grid-2">
                <Stat label="Matched" value={`${result.accounts?.matched}/${result.accounts?.asked}`} />
                <Stat label="Gate" value={result.gate?.ok ? 'PASS' : 'FAIL'} />
                <Stat label="Fields" value={pct(result.field_accuracy)} />
                <Stat label="Payments" value={pct(result.payment_history)} />
                <Stat label="Provenance" value={pct(result.provenance)} />
                <Stat label="Banked" value={result.banked_batches_unchanged ? 'unchanged' : 'CHANGED'} />
              </div>
              <PaymentDetail result={result} />
              {result.field_accuracy?.misses?.length > 0 && (
                <div>
                  <div className="label">Field misses</div>
                  {result.field_accuracy.misses.map((m, i) => (
                    <div key={i} className="muted">
                      {m.account} · {m.field}: expected <code>{String(m.expected)}</code>,
                      got <code>{String(m.got)}</code>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function PaymentDetail({ result }) {
  const misses = paymentMisses(result)
  const byAccount = paymentByAccount(result)
  const history = result.payment_history
  if (!history?.total) return null
  return (
    <div>
      <div className="label">
        Payment history — {history.correct}/{history.total} months correct
      </div>
      {byAccount.map(a => (
        <div key={a.account} className="muted">
          {a.account}: expected {a.expected}, extracted {a.extracted}, correct {a.correct}
        </div>
      ))}
      {misses.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {misses.map((m, i) => (
            <div key={i} className="muted">
              {m.account} · {m.month}: expected <code>{m.expected}</code>,
              got {m.missing ? <strong>MISSING</strong> : <code>{m.got}</code>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const Stat = ({ label, value }) => (
  <div className="stat">
    <div className="stat-label">{label}</div>
    <div className="stat-value" style={{ fontSize: 16 }}>{value}</div>
  </div>
)
