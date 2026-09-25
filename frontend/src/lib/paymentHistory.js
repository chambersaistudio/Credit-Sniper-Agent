// Payment-history maths. Deterministic, and deliberately conservative: a
// month the report doesn't cover is NOT a missed payment, and a derogatory
// status is NOT an ordinary late payment. Every number here is computed from
// actual month-level observations or is omitted entirely.
//
// Pure JS with no framework imports so it can be unit-tested directly.

export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// kind drives the maths:
//   ok          counts toward on-time
//   late        counts toward on-time denominator AND the late tally
//   derogatory  reported separately — never folded into a late percentage
//   none        no payment was observed; excluded from every denominator
export const CODES = {
  ok: { label: 'Current / terms met', short: '✓', tone: 'green', kind: 'ok' },
  30: { label: '30 days late', short: '30', tone: 'amber', kind: 'late', days: 30 },
  60: { label: '60 days late', short: '60', tone: 'amber', kind: 'late', days: 60 },
  90: { label: '90 days late', short: '90', tone: 'red', kind: 'late', days: 90 },
  120: { label: '120 days late', short: '120', tone: 'red', kind: 'late', days: 120 },
  150: { label: '150 days late', short: '150', tone: 'red', kind: 'late', days: 150 },
  180: { label: '180 days late', short: '180', tone: 'red', kind: 'late', days: 180 },
  CO: { label: 'Charge-off', short: 'CO', tone: 'red', kind: 'derogatory' },
  COL: { label: 'Collection', short: 'COL', tone: 'red', kind: 'derogatory' },
  VS: { label: 'Voluntarily surrendered', short: 'VS', tone: 'red', kind: 'derogatory' },
  RepoM: { label: 'Repossession', short: 'RPO', tone: 'red', kind: 'derogatory' },
  CLS: { label: 'Closed', short: 'CLS', tone: 'neutral', kind: 'none' },
  ND: { label: 'No data reported', short: 'ND', tone: 'neutral', kind: 'none' },
  unknown: { label: 'Unavailable', short: '—', tone: 'neutral', kind: 'none' },
}

// Order used for "most severe" comparisons.
const SEVERITY_ORDER = ['ok', '30', '60', '90', '120', '150', '180', 'VS', 'RepoM', 'COL', 'CO']

/**
 * Map a reported code to one of our canonical keys. Unrecognised codes fall
 * back to `unknown`, which is excluded from every calculation rather than
 * guessed at.
 */
export function normalizeCode(raw) {
  const value = String(raw ?? '').trim().toUpperCase()
  if (!value) return 'unknown'
  if (['OK', 'C', 'CUR', 'CURRENT', '✓', '0', 'PAYS AS AGREED', 'TERMS MET'].includes(value)) return 'ok'
  const days = value.match(/^(30|60|90|120|150|180)\b/)
  if (days) return days[1]
  if (/^(CO|CHARGE.?OFF)$/.test(value)) return 'CO'
  if (/^(COL|COLL|COLLECTION)$/.test(value)) return 'COL'
  if (/^(VS|VOLUNTAR)/.test(value)) return 'VS'
  if (/^(R|RPO|REPO)/.test(value)) return 'RepoM'
  if (/^(CLS|CLOSED)$/.test(value)) return 'CLS'
  if (['ND', 'N/D', 'NO DATA', 'NR', '-', '--'].includes(value)) return 'ND'
  return 'unknown'
}

export const codeInfo = key => CODES[key] || CODES.unknown

/** Normalize raw entries into {year, month, key, raw} and drop unusable rows. */
export function normalizeEntries(entries) {
  return (entries || [])
    .map(e => {
      // Accepts both the current per-month shape and the earlier
      // raw_code/code one, so already-stored histories still render.
      const raw = e.raw_status_code ?? e.status_code ?? e.raw_code ?? e.code ?? ''
      return {
        year: Number(e.year),
        month: Number(e.month),
        raw,
        key: normalizeCode(raw),
        // Per-month detail some bureaus print alongside the status letter.
        balance: e.balance ?? null,
        pastDue: e.past_due ?? null,
        amountPaid: e.amount_paid ?? null,
        amountDue: e.amount_due ?? null,
        remarks: e.remarks || [],
        sourcePage: e.source_page ?? null,
      }
    })
    .filter(e => Number.isInteger(e.year) && e.month >= 1 && e.month <= 12)
}

/**
 * A year-by-month grid: newest year first, 12 columns each. Months the report
 * never covered stay null so they render as blanks rather than as anything
 * that could read like a missed payment.
 */
export function buildGrid(entries) {
  const rows = normalizeEntries(entries)
  if (!rows.length) return []
  const years = [...new Set(rows.map(e => e.year))].sort((a, b) => b - a)
  const byKey = new Map(rows.map(e => [`${e.year}-${e.month}`, e]))
  return years.map(year => ({
    year,
    months: MONTHS.map((_, i) => byKey.get(`${year}-${i + 1}`) || null),
  }))
}

/**
 * Derived metrics, or nulls where the history doesn't support them.
 *
 * onTimePct's denominator is months with an actual payment observation —
 * on-time plus late. Blanks, ND, closed markers and unrecognised codes are
 * excluded, and derogatory statuses are reported on their own rather than
 * diluted into a percentage.
 */
export function summarize(entries) {
  const rows = normalizeEntries(entries)
  const counts = {}
  for (const row of rows) counts[row.key] = (counts[row.key] || 0) + 1

  const onTime = counts.ok || 0
  const lateKeys = ['30', '60', '90', '120', '150', '180']
  const lateCounts = Object.fromEntries(lateKeys.map(k => [k, counts[k] || 0]))
  const lateTotal = lateKeys.reduce((sum, k) => sum + lateCounts[k], 0)
  const denominator = onTime + lateTotal

  const derogatory = ['CO', 'COL', 'VS', 'RepoM']
    .filter(k => counts[k])
    .map(k => ({ key: k, label: codeInfo(k).label, count: counts[k] }))

  const chronological = [...rows].sort((a, b) => a.year - b.year || a.month - b.month)
  const lateRows = chronological.filter(r => lateKeys.includes(r.key))
  const worst = SEVERITY_ORDER.filter(k => counts[k] && k !== 'ok').pop() || null
  const firstDerogatory = chronological.find(r => derogatory.some(d => d.key === r.key)) || null
  const lastDerogatory = [...chronological].reverse().find(r => derogatory.some(d => d.key === r.key)) || null

  return {
    monthsReported: denominator,
    monthsObserved: rows.length,
    onTime,
    lateTotal,
    lateCounts,
    // Null, never 100, when nothing reportable exists.
    onTimePct: denominator > 0 ? Math.round((onTime / denominator) * 100) : null,
    worst,
    derogatory,
    mostRecentLate: lateRows.length ? lateRows[lateRows.length - 1] : null,
    firstDerogatory,
    lastDerogatory,
    range: chronological.length
      ? { from: chronological[0], to: chronological[chronological.length - 1] }
      : null,
  }
}

/**
 * One compact badge for an account card, or null when the history doesn't
 * support saying anything. Derogatory status wins over a late tally, which
 * wins over a clean record.
 */
export function compactPaymentBadge(summary) {
  if (!summary) return null
  if (summary.derogatory.length) {
    return { text: summary.derogatory[0].label, tone: 'red' }
  }
  if (summary.lateTotal > 0) {
    const parts = Object.entries(summary.lateCounts).filter(([, n]) => n > 0)
    if (parts.length <= 2) {
      return { text: parts.map(([days, n]) => `${n}×${days}`).join(', '), tone: 'amber' }
    }
    return { text: `${summary.lateTotal} late payment${summary.lateTotal === 1 ? '' : 's'}`, tone: 'amber' }
  }
  if (summary.onTimePct === 100 && summary.monthsReported >= 6) {
    return { text: '100% on time', tone: 'green' }
  }
  return null
}

/** Revolving utilisation, only when both numbers genuinely exist. */
export function utilization(record) {
  const limit = Number(record?.credit_limit)
  const balance = Number(record?.balance)
  if (!Number.isFinite(limit) || limit <= 0) return null
  if (!Number.isFinite(balance) || record.balance === null) return null
  return Math.round((balance / limit) * 100)
}

/** Whole months between a date string and now, or null if unparseable. */
export function accountAgeMonths(dateOpened, now = new Date()) {
  if (!dateOpened) return null
  const parsed = new Date(dateOpened)
  if (Number.isNaN(parsed.getTime())) return null
  const months = (now.getFullYear() - parsed.getFullYear()) * 12 + (now.getMonth() - parsed.getMonth())
  return months >= 0 ? months : null
}

export function formatAge(months) {
  if (months === null) return null
  const years = Math.floor(months / 12)
  const rest = months % 12
  if (!years) return `${rest} mo`
  return rest ? `${years} yr ${rest} mo` : `${years} yr`
}
