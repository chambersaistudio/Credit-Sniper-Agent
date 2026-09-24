// The 3–5 badges worth showing for an account at a glance. Every badge
// carries text — colour is reinforcement, never the only signal.
// Explicit extension so this resolves under both Vite and plain Node ESM
// (the unit tests import it directly).
import { compactPaymentBadge, summarize } from './paymentHistory.js'

/** "Needs a look": Credit Sniper found a finding, discrepancy, missing
 *  evidence or review opportunity. Deliberately distinct from "Negative",
 *  which is about the account's own reporting. */
export const needsLook = account => (account.findings || []).length > 0

const OPEN = new Set(['open', 'current'])
const CLOSED = new Set(['closed', 'paid', 'transferred', 'sold'])
const DEROGATORY = new Set(['collection', 'charged_off', 'derogatory', 'late',
  '30d_late', '60d_late', '90d_late', '120d_late'])

// A collection/debt-buyer tradeline reports a debt being pursued, not a
// repayment relationship — its grid is usually CO/COL/ND rather than a real
// month-by-month payment record. Recognised from the type the report prints,
// or from a normalized collection status when no type is given.
const DEBT_BUYER = /debt\s*(buyer|purchaser)/i
const COLLECTION = /collection|collection\s*agency|factoring\s*company/i

// How many real repayment observations make an on-time claim worth showing.
// Matches compactPaymentBadge's own threshold for "100% on time".
const MEANINGFUL_REPAYMENT_MONTHS = 6

/** 'debt_buyer' | 'collection' | null, from what the report actually says. */
export function collectionKind(records = []) {
  const types = records.map(r => r.account_type || '').join(' ')
  if (DEBT_BUYER.test(types)) return 'debt_buyer'
  if (COLLECTION.test(types)) return 'collection'
  if (records.some(r => r.account_status === 'collection')) return 'collection'
  return null
}

/** Is this account currently reporting derogatory? */
export function isDerogatory(account, records = []) {
  return Boolean(account.is_negative) || records.some(r => DEROGATORY.has(r.account_status))
}

/** The record with the richest payment history, for a single card indicator. */
export function bestHistoryRecord(records = []) {
  return records.reduce((best, r) => {
    const n = (r.payment_history || []).length
    return n > (best ? (best.payment_history || []).length : 0) ? r : best
  }, null)
}

export function accountBadges(account) {
  const badges = []
  const records = account.records || []
  const statuses = new Set(records.map(r => r.account_status).filter(Boolean))
  const kind = collectionKind(records)
  const derogatory = isDerogatory(account, records)

  // What the account IS leads: a collection or debt-buyer tradeline is named
  // as such, then its negative state.
  if (kind) {
    badges.push({ key: 'kind', text: kind === 'debt_buyer' ? 'Debt buyer' : 'Collection', tone: 'red' })
  }
  if (account.is_negative) {
    badges.push({ key: 'negative', text: 'Negative', tone: 'red' })
  }
  // Open/closed is only worth stating when the bureaus agree on it, and it
  // adds nothing next to a collection badge.
  if (statuses.size === 1 && !kind) {
    const [status] = [...statuses]
    if (OPEN.has(status)) badges.push({ key: 'open', text: 'Open', tone: 'sky' })
    else if (CLOSED.has(status)) badges.push({ key: 'closed', text: 'Closed', tone: 'neutral' })
  }

  const history = bestHistoryRecord(records)
  const summary = history ? summarize(history.payment_history) : null
  const payment = compactPaymentBadge(summary)
  if (payment && !suppressPaymentBadge(payment, summary, { kind, derogatory })) {
    badges.push({ key: 'payment', text: payment.text, tone: payment.tone })
  }

  if (needsLook(account) && !account.is_negative) {
    badges.push({ key: 'review', text: 'Needs a look', tone: 'amber' })
  }
  return badges.slice(0, 4)
}

/**
 * Presentation-only gate on the card's payment badge. The underlying maths is
 * untouched and the detail page still shows it in full.
 *
 * A positive "X% on time" is withheld when it would misrepresent or visually
 * compete with the account's actual state:
 *   - on a collection / debt-buyer tradeline, unless it really does carry a
 *     traditional month-by-month repayment record;
 *   - on any account currently reporting derogatory, where a green badge
 *     beside "Negative" reads as contradiction.
 * Derogatory and late badges are never suppressed — those are the signal.
 */
export function suppressPaymentBadge(payment, summary, { kind, derogatory }) {
  if (!payment || payment.tone !== 'green') return false
  if (derogatory) return true
  if (kind) return !hasTraditionalRepaymentHistory(summary)
  return false
}

/** Real repayment observations — on-time or late months, not CO/COL/ND filler. */
export function hasTraditionalRepaymentHistory(summary) {
  return Boolean(summary) && summary.monthsReported >= MEANINGFUL_REPAYMENT_MONTHS
}
