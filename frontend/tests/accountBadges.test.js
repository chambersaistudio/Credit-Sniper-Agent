// Card-badge semantics. Presentation only — these assert what a card SHOWS,
// never what the payment-history maths computes.
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  accountBadges, collectionKind, hasTraditionalRepaymentHistory, isDerogatory, needsLook,
} from '../src/lib/accountBadges.js'
import { summarize } from '../src/lib/paymentHistory.js'

const onTime = months => Array.from({ length: months }, (_, i) => ({
  year: 2025, month: (i % 12) + 1, raw_code: 'OK',
}))

const account = (overrides = {}) => ({
  is_negative: false,
  findings: [],
  records: [{ bureau: 'experian', account_status: 'open', account_type: 'Credit card', payment_history: [] }],
  ...overrides,
})

const texts = a => accountBadges(a).map(b => b.text)

describe('collectionKind', () => {
  it('recognises collections and debt buyers from the printed type', () => {
    assert.equal(collectionKind([{ account_type: 'Collection' }]), 'collection')
    assert.equal(collectionKind([{ account_type: 'Collection Agency' }]), 'collection')
    assert.equal(collectionKind([{ account_type: 'Factoring Company Account' }]), 'collection')
    assert.equal(collectionKind([{ account_type: 'Debt Buyer' }]), 'debt_buyer')
    assert.equal(collectionKind([{ account_type: 'Debt Purchaser' }]), 'debt_buyer')
  })

  it('falls back to a normalized collection status when no type is printed', () => {
    assert.equal(collectionKind([{ account_type: null, account_status: 'collection' }]), 'collection')
  })

  it('leaves ordinary tradelines alone', () => {
    assert.equal(collectionKind([{ account_type: 'Credit card', account_status: 'open' }]), null)
    assert.equal(collectionKind([{ account_type: 'Auto Loan' }]), null)
    assert.equal(collectionKind([]), null)
  })
})

describe('on-time badge is withheld on collection tradelines', () => {
  it('does not claim "% on time" for a collection without a real repayment record', () => {
    // A collection grid that is only CO/ND carries no repayment relationship.
    const a = account({
      is_negative: false,
      records: [{
        bureau: 'experian', account_type: 'Collection', account_status: 'collection',
        payment_history: [
          { year: 2026, month: 1, raw_code: 'ND' }, { year: 2026, month: 2, raw_code: 'ND' },
        ],
      }],
    })
    assert.deepEqual(texts(a), ['Collection'])
  })

  it('withholds it on a still-derogatory collection however long the grid is', () => {
    // The stricter rule wins: a collection currently reporting as such never
    // shows green, even with a full clean grid.
    const a = account({
      records: [{
        bureau: 'experian', account_type: 'Collection', account_status: 'collection',
        payment_history: onTime(12),
      }],
    })
    assert.equal(texts(a).includes('100% on time'), false)
  })

  it('allows it on a settled collection that does carry a repayment record', () => {
    const settled = record => account({
      records: [{ bureau: 'experian', account_type: 'Collection', account_status: 'paid', ...record }],
    })
    // Paid off and with a real month-by-month record behind it.
    assert.ok(texts(settled({ payment_history: onTime(8) })).includes('100% on time'))
    // Too few reportable months to call it a repayment history.
    assert.equal(texts(settled({ payment_history: onTime(3) })).includes('100% on time'), false)
  })

  it('names a debt buyer as such', () => {
    const a = account({
      records: [{ bureau: 'experian', account_type: 'Debt Buyer', account_status: 'collection', payment_history: [] }],
    })
    assert.deepEqual(texts(a), ['Debt buyer'])
  })

  it('does not add Open/Closed next to a collection badge', () => {
    const a = account({
      records: [{ bureau: 'experian', account_type: 'Collection', account_status: 'open', payment_history: [] }],
    })
    assert.deepEqual(texts(a), ['Collection'])
  })
})

describe('green never competes with a negative state', () => {
  it('withholds "100% on time" on a derogatory account', () => {
    const a = account({
      is_negative: true,
      records: [{
        bureau: 'experian', account_type: 'Credit card', account_status: 'charged_off',
        payment_history: onTime(12),
      }],
    })
    const shown = texts(a)
    assert.ok(shown.includes('Negative'))
    assert.equal(shown.includes('100% on time'), false)
  })

  it('still shows the derogatory payment badge itself', () => {
    const a = account({
      is_negative: true,
      records: [{
        bureau: 'experian', account_type: 'Credit card', account_status: 'charged_off',
        payment_history: [...onTime(6), { year: 2026, month: 1, raw_code: 'CO' }],
      }],
    })
    assert.deepEqual(texts(a), ['Negative', 'Charge-off'])
  })

  it('still shows late payments on a derogatory account', () => {
    const a = account({
      is_negative: true,
      records: [{
        bureau: 'experian', account_type: 'Credit card', account_status: 'late',
        payment_history: [...onTime(6), { year: 2026, month: 1, raw_code: '30' }],
      }],
    })
    assert.ok(texts(a).includes('1×30'))
  })
})

describe('ordinary accounts are unaffected', () => {
  it('keeps the on-time badge on a healthy open card', () => {
    const a = account({
      records: [{
        bureau: 'experian', account_type: 'Credit card', account_status: 'open',
        payment_history: onTime(12),
      }],
    })
    assert.deepEqual(texts(a), ['Open', '100% on time'])
  })

  it('flags review only when there are findings and no negative state', () => {
    assert.equal(needsLook(account()), false)
    const reviewed = account({ findings: [{ severity: 'not_disclosed' }] })
    assert.ok(texts(reviewed).includes('Needs a look'))
  })

  it('caps the badge list', () => {
    const a = account({
      is_negative: true,
      findings: [{ severity: 'likely_inaccuracy' }],
      records: [{
        bureau: 'experian', account_type: 'Collection', account_status: 'collection',
        payment_history: [{ year: 2026, month: 1, raw_code: 'CO' }],
      }],
    })
    assert.ok(accountBadges(a).length <= 4)
  })
})

describe('helpers', () => {
  it('isDerogatory reads both the flag and the status', () => {
    assert.ok(isDerogatory({ is_negative: true }, []))
    assert.ok(isDerogatory({}, [{ account_status: 'collection' }]))
    assert.equal(isDerogatory({}, [{ account_status: 'open' }]), false)
  })

  it('hasTraditionalRepaymentHistory needs real reportable months', () => {
    assert.equal(hasTraditionalRepaymentHistory(summarize([])), false)
    assert.equal(hasTraditionalRepaymentHistory(summarize(onTime(5))), false)
    assert.ok(hasTraditionalRepaymentHistory(summarize(onTime(6))))
    // ND months are not repayment observations, so they don't qualify it.
    const nd = Array.from({ length: 12 }, (_, i) => ({ year: 2025, month: i + 1, raw_code: 'ND' }))
    assert.equal(hasTraditionalRepaymentHistory(summarize(nd)), false)
  })
})
