// The 3–5 badges worth showing for an account at a glance. Every badge
// carries text — colour is reinforcement, never the only signal.
import { compactPaymentBadge, summarize } from './paymentHistory'

/** "Needs a look": Credit Sniper found a finding, discrepancy, missing
 *  evidence or review opportunity. Deliberately distinct from "Negative",
 *  which is about the account's own reporting. */
export const needsLook = account => (account.findings || []).length > 0

const OPEN = new Set(['open', 'current'])
const CLOSED = new Set(['closed', 'paid', 'transferred', 'sold'])

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

  if (account.is_negative) {
    badges.push({ key: 'negative', text: 'Negative', tone: 'red' })
  }
  // Open/closed is only worth stating when the bureaus agree on it.
  if (statuses.size === 1) {
    const [status] = [...statuses]
    if (OPEN.has(status)) badges.push({ key: 'open', text: 'Open', tone: 'sky' })
    else if (CLOSED.has(status)) badges.push({ key: 'closed', text: 'Closed', tone: 'neutral' })
  }

  const history = bestHistoryRecord(records)
  const payment = history ? compactPaymentBadge(summarize(history.payment_history)) : null
  if (payment) badges.push({ key: 'payment', text: payment.text, tone: payment.tone })

  if (needsLook(account) && !account.is_negative) {
    badges.push({ key: 'review', text: 'Needs a look', tone: 'amber' })
  }
  return badges.slice(0, 4)
}
