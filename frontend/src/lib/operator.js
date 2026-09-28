// Operator page logic, kept out of the component so it can be unit-tested.
//
// The page spends real money, so the rules about when and how are worth
// testing directly rather than inferring from rendered markup.

// One config per run. There is deliberately no "all" entry — the backend has
// no such operation either, and the UI must not imply one exists.
export const BENCHMARK_CONFIGS = [
  { config: 'A', model: 'gpt-5.6-luna', label: 'Luna', tone: 'green', expensive: false },
  { config: 'B', model: 'gpt-5.6-terra', label: 'Terra', tone: 'amber', expensive: false },
  { config: 'C', model: 'gpt-5.6-sol', label: 'Sol', tone: 'red', expensive: true },
]

export const TERMINAL_JOB_STATUSES = ['succeeded', 'failed']

export function isJobRunning(status) {
  return Boolean(status) && !TERMINAL_JOB_STATUSES.includes(status)
}

export function configFor(letter) {
  return BENCHMARK_CONFIGS.find(c => c.config === letter) || null
}

/**
 * What the confirmation must say before a paid run. Every paid action is
 * confirmed, and the expensive one says so in its own words — a single
 * generic prompt trains people to tap through it.
 */
export function confirmationFor(letter, batchId) {
  const config = configFor(letter)
  if (!config) return null
  const base = `Run the ${config.label} benchmark on batch ${batchId}?`
  const cost = 'This makes exactly one paid model call and banks nothing.'
  return {
    title: base,
    body: config.expensive
      ? `${cost} ${config.label} is the expensive escalation model — only run it deliberately.`
      : cost,
    requiresAck: config.expensive,
    confirmLabel: config.expensive ? `Yes, run ${config.label}` : `Run ${config.label}`,
  }
}

/**
 * The request body for a benchmark job.
 *
 * Truth travels by REFERENCE: it is stored server-side once and named by
 * label, so running a second config does not mean re-entering account data on
 * a phone. Inline `truth` stays supported for the CLI, where a file is the
 * natural source, but the page never sends it.
 */
export function benchmarkRequest({ reportId, batchId, config, truth, truthLabel, idempotencyKey }) {
  const body = {
    report_id: reportId,
    batch_id: batchId,
    config,
  }
  if (truth) body.truth = truth
  if (truthLabel) body.truth_label = truthLabel
  if (idempotencyKey) body.idempotency_key = idempotencyKey
  // Only ever set for the config that requires it, and only from an explicit
  // confirmation — never defaulted on.
  if (configFor(config)?.expensive) body.acknowledge_expensive = true
  return body
}

/** Percentages, or an em dash where the measurement does not exist. */
export function pct(tally) {
  if (!tally || tally.accuracy === null || tally.accuracy === undefined) return '—'
  return `${(tally.accuracy * 100).toFixed(1)}%`
}

export function usd(value) {
  return value === null || value === undefined ? '—' : `$${value.toFixed(4)}`
}

/**
 * Payment-history misses, flattened for rendering: account, month, expected,
 * and what came back — distinguishing a cell read wrong from one never
 * returned, because they are different failures.
 */
export function paymentMisses(result) {
  const history = result?.payment_history
  if (!history?.misses?.length) return []
  return history.misses.map(m => ({
    account: m.account,
    month: m.month,
    expected: m.expected,
    got: m.missing ? 'MISSING' : m.got,
    missing: Boolean(m.missing),
  }))
}

/** Per-account month counts, sorted for a stable render. */
export function paymentByAccount(result) {
  const by = result?.payment_history?.by_account || {}
  return Object.entries(by)
    .map(([account, counts]) => ({ account, ...counts }))
    .sort((a, b) => a.account.localeCompare(b.account))
}

/** A short, human summary of a finished job, for the list. */
export function jobSummary(job) {
  if (!job) return ''
  if (job.status === 'failed') return job.error?.message || 'Failed'
  if (isJobRunning(job.status)) return 'Running…'
  const result = job.result
  if (!result) return 'Done'
  if (job.operation === 'draft_truth_batch') {
    return `Drafted ${result.accounts_drafted} accounts · ${result.months_drafted} months `
      + '— correct and verify'
  }
  return `${result.accounts?.matched ?? '?'}/${result.accounts?.asked ?? '?'} matched · `
    + `fields ${pct(result.field_accuracy)} · payments ${pct(result.payment_history)}`
}


// ── Benchmark truth ─────────────────────────────────────────────────────

export const DEFAULT_TRUTH_LABEL = 'current'

/** The stored truth summary for one batch, or null. */
export function truthFor(summaries, batchId, label = DEFAULT_TRUTH_LABEL) {
  return (summaries || []).find(t => t.batch_id === batchId && t.label === label) || null
}

/**
 * How a batch's truth reads on the page, and whether it can be scored against.
 *
 * Three states, not two: absent, present-but-unconfirmed, and confirmed. The
 * middle one exists because a draft prefilled from a model's own extraction
 * looks exactly like truth and is not — benchmarking against it would measure
 * agreement with that model rather than correctness.
 */
export function truthStatus(summary) {
  if (!summary) {
    return {
      state: 'missing', tone: 'red', label: 'No truth stored', canBenchmark: false,
      detail: 'Save the values this batch actually prints before scoring a model against them.',
    }
  }
  const drafted = DRAFT_SOURCES.includes(summary.source)
  const by = summary.drafted_by_model ? ` by ${modelLabel(summary.drafted_by_model)}` : ''
  if (!summary.verified) {
    return {
      state: 'unverified',
      tone: 'amber',
      label: drafted ? 'Draft — not verified' : 'Saved — not verified',
      canBenchmark: false,
      detail: drafted
        ? `Prefilled${by} from the model's own read. Correct every field against the document `
          + 'and verify it — scoring a model against its own output measures nothing.'
        : 'Check these values against the document, then verify. A benchmark against '
          + 'unverified truth is refused.',
    }
  }
  return {
    state: 'verified', tone: 'green', label: 'Verified', canBenchmark: true,
    detail: summary.drafted_by_model
      ? `Stored once and referenced by every run. Drafted${by} and corrected by hand, so a `
        + `${modelLabel(summary.drafted_by_model)} benchmark against it is flagged.`
      : 'Stored once and referenced by every run — nothing to re-enter.',
  }
}

const DRAFT_SOURCES = ['drafted_from_batch', 'drafted_from_model']

/** "Luna" for "gpt-5.6-luna"; the raw id for anything not in the configs. */
export function modelLabel(model) {
  return BENCHMARK_CONFIGS.find(c => c.model === model)?.label || model || ''
}

/** "4 accounts · 36 months", for the truth panel. */
export function truthCounts(summary) {
  if (!summary) return ''
  const accounts = `${summary.account_count} account${summary.account_count === 1 ? '' : 's'}`
  return summary.months ? `${accounts} · ${summary.months} months` : accounts
}

/** The first 8 characters of the fingerprint, which is all that is useful. */
export function shortFingerprint(summary) {
  return summary?.fingerprint ? summary.fingerprint.slice(0, 8) : '—'
}

/**
 * Why a benchmark button is disabled, or null when it is not.
 *
 * The page refuses the run itself rather than letting the request fail: a 409
 * after a tap on a paid button reads like the money went somewhere.
 */
export function benchmarkBlockedReason(summary) {
  const status = truthStatus(summary)
  if (status.canBenchmark) return null
  return status.state === 'missing'
    ? 'Save and verify this batch\'s truth first.'
    : 'Verify this batch\'s truth first.'
}

/** Pretty-print stored truth for correction, so editing is editing, not typing. */
export function truthToText(truth) {
  if (!truth) return ''
  return JSON.stringify({ accounts: truth.accounts || [] }, null, 2)
}

/** Parse an edited truth document, with a message a person can act on. */
export function parseTruthText(text) {
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    return { error: 'That is not valid JSON. Check for a trailing comma or a missing quote.' }
  }
  const accounts = parsed?.accounts
  if (!Array.isArray(accounts) || !accounts.length) {
    return { error: 'Truth needs an "accounts" array with at least one account.' }
  }
  const unnamed = accounts.findIndex(a => !String(a?.creditor_name || '').trim())
  if (unnamed >= 0) {
    return { error: `Account ${unnamed + 1} has no creditor_name.` }
  }
  return { accounts }
}


// ── Getting truth when there is none ────────────────────────────────────

/**
 * What the truth panel should offer next, and why.
 *
 * The case this exists for: a batch with no stored truth AND nothing banked.
 * The free draft needs a banked extraction, the benchmark needs verified
 * truth, and a blank editor with a Save button reads as the obvious next step
 * — which means typing four accounts of payment grid into JSON on a phone. The
 * page says what is missing instead, and offers the paid draft that fills it.
 */
export function truthNextStep({ summary, banked }) {
  if (summary) {
    return summary.verified
      ? { step: 'ready', title: 'Ready to benchmark' }
      : { step: 'correct-and-verify', title: 'Correct it, then verify' }
  }
  if (banked) {
    return {
      step: 'draft-from-banked',
      title: 'Draft from the banked extraction',
      detail: 'Free: this batch is already banked, so the draft reuses that read.',
    }
  }
  return {
    step: 'draft-with-model',
    title: 'Nothing to draft from yet',
    detail: 'This batch has no banked extraction, so there is nothing free to prefill truth '
      + 'from. Draft it with one paid model read: it banks nothing, is stored unverified, '
      + 'and no benchmark will use it until you have corrected it against the document '
      + 'and verified it.',
  }
}

/** The request body for a paid truth draft. */
export function truthDraftRequest({ reportId, batchId, config, idempotencyKey }) {
  const body = { report_id: reportId, batch_id: batchId, config }
  if (idempotencyKey) body.idempotency_key = idempotencyKey
  if (configFor(config)?.expensive) body.acknowledge_expensive = true
  return body
}

/** What the confirmation says before a paid draft. */
export function draftConfirmationFor(letter, batchId) {
  const config = configFor(letter)
  if (!config) return null
  const cost = 'This makes exactly one paid model call and banks nothing. The result is '
    + 'a draft: you correct and verify it before any benchmark will use it, and a '
    + `${config.label} benchmark against it will be flagged as scoring ${config.label} `
    + 'against its own read.'
  return {
    title: `Draft ${batchId} truth with ${config.label}?`,
    body: config.expensive
      ? `${cost} ${config.label} is the expensive escalation model — only run it deliberately.`
      : cost,
    requiresAck: config.expensive,
    confirmLabel: config.expensive ? `Yes, draft with ${config.label}` : `Draft with ${config.label}`,
  }
}

/** A draft job for this batch that has not finished, if any. */
export function draftInFlight(jobs, batchId) {
  return (jobs || []).find(j => j.operation === 'draft_truth_batch'
    && j.request?.batch_id === batchId && isJobRunning(j.status)) || null
}

/**
 * One key per confirmation, so a double tap on a paid button reuses the
 * first job instead of buying a second one. The server treats a repeated key
 * as the same request.
 */
export function newIdempotencyKey(kind, batchId, config) {
  const random = globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${kind}-${batchId}-${config}-${random}`
}
