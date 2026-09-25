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

/** The request body for a benchmark job. */
export function benchmarkRequest({ reportId, batchId, config, truth, idempotencyKey }) {
  const body = {
    report_id: reportId,
    batch_id: batchId,
    config,
    truth,
  }
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
  return `${result.accounts?.matched ?? '?'}/${result.accounts?.asked ?? '?'} matched · `
    + `fields ${pct(result.field_accuracy)} · payments ${pct(result.payment_history)}`
}
