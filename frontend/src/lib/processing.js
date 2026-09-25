// Background extraction, as the client sees it.
//
// Uploading a report returns 202 and a report id: reading the PDF is a
// durable background job, because an Experian disclosure can take longer than
// a browser or proxy will hold a connection open. The client polls this state
// instead of waiting on one long request.
//
// Pure JS with no framework imports so it can be unit-tested directly.

// A report is finished when its stage is one of the extraction outcomes.
// Anything else is still in flight.
export const TERMINAL_STAGES = [
  'verified', 'needs_audit', 'extraction_incomplete', 'failed', 'provider_unavailable',
]

// In the order they happen, for the progress indicator.
export const PIPELINE = ['queued', 'extracting', 'extraction_complete', 'auditing', 'reconciling']

// What each stage means, in the consumer's terms. Never names a provider or
// an error code: which vendor is slow, and why, is ours to deal with.
export const STAGE_LABELS = {
  queued: 'Waiting to be read',
  extracting: 'Reading your report',
  extraction_complete: 'Report read — starting verification',
  auditing: 'Double-checking what we read against your document',
  reconciling: 'Finishing up',
}

export function isProcessing(stage) {
  return Boolean(stage) && !TERMINAL_STAGES.includes(stage)
}

export function stageLabel(stage) {
  return STAGE_LABELS[stage] || (isProcessing(stage) ? 'Working on your report' : null)
}

/** Rough progress through the pipeline, 0–100, for a determinate-ish bar. */
export function stageProgress(stage) {
  if (!isProcessing(stage)) return 100
  const index = PIPELINE.indexOf(stage)
  if (index < 0) return 5
  return Math.round(((index + 1) / (PIPELINE.length + 1)) * 100)
}

// Poll quickly at first — a small report finishes in seconds — then back off
// so a long extraction doesn't hammer the API for minutes.
const MIN_DELAY_MS = 1500
const MAX_DELAY_MS = 8000

export function pollDelayMs(attempt) {
  const delay = MIN_DELAY_MS * Math.pow(1.5, Math.max(0, attempt))
  return Math.min(Math.round(delay), MAX_DELAY_MS)
}

// Stop eventually rather than poll forever in a tab someone left open. The
// work is durable, so giving up here loses nothing: the report keeps
// processing and the page shows the result whenever it is reopened.
export const POLL_TIMEOUT_MS = 10 * 60 * 1000

export function pollTimedOut(startedAt, now = Date.now()) {
  return now - startedAt >= POLL_TIMEOUT_MS
}
