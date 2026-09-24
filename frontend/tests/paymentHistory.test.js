// Run with: npm test  (node --test, no test framework dependency)
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  accountAgeMonths, buildGrid, compactPaymentBadge, normalizeCode, summarize, utilization,
} from '../src/lib/paymentHistory.js'

const entry = (year, month, raw_code) => ({ year, month, raw_code })

describe('normalizeCode', () => {
  it('maps the codes reports actually print', () => {
    assert.equal(normalizeCode('OK'), 'ok')
    assert.equal(normalizeCode('Current'), 'ok')
    assert.equal(normalizeCode('30'), '30')
    assert.equal(normalizeCode('120'), '120')
    assert.equal(normalizeCode('CO'), 'CO')
    assert.equal(normalizeCode('Collection'), 'COL')
    assert.equal(normalizeCode('VS'), 'VS')
    assert.equal(normalizeCode('CLS'), 'CLS')
    assert.equal(normalizeCode('ND'), 'ND')
  })

  it('never guesses at an unrecognised or empty code', () => {
    assert.equal(normalizeCode(''), 'unknown')
    assert.equal(normalizeCode(null), 'unknown')
    assert.equal(normalizeCode('???'), 'unknown')
  })
})

describe('summarize', () => {
  it('computes on-time percentage from reportable months only', () => {
    const s = summarize([
      entry(2025, 1, 'OK'), entry(2025, 2, 'OK'), entry(2025, 3, 'OK'),
      entry(2025, 4, '30'),
      entry(2025, 5, 'ND'), entry(2025, 6, ''),   // excluded entirely
    ])
    assert.equal(s.monthsReported, 4)   // 3 on-time + 1 late, ND/blank excluded
    assert.equal(s.onTime, 3)
    assert.equal(s.lateTotal, 1)
    assert.equal(s.onTimePct, 75)
  })

  it('does not count unavailable months as missed payments', () => {
    const s = summarize([entry(2025, 1, 'OK'), entry(2025, 2, 'ND'), entry(2025, 3, 'CLS')])
    assert.equal(s.lateTotal, 0)
    assert.equal(s.monthsReported, 1)
    assert.equal(s.onTimePct, 100)
  })

  it('returns null rather than fabricating a percentage', () => {
    const s = summarize([entry(2025, 1, 'ND'), entry(2025, 2, '')])
    assert.equal(s.onTimePct, null)
    assert.equal(s.monthsReported, 0)
  })

  it('returns null for an empty history', () => {
    assert.equal(summarize([]).onTimePct, null)
    assert.equal(summarize(undefined).monthsObserved, 0)
  })

  it('keeps derogatory statuses out of the late percentage', () => {
    const s = summarize([
      entry(2025, 1, 'OK'), entry(2025, 2, 'OK'),
      entry(2025, 3, 'CO'), entry(2025, 4, 'CO'),
    ])
    // The charge-offs are reported on their own, not as lates or as on-time.
    assert.equal(s.lateTotal, 0)
    assert.equal(s.monthsReported, 2)
    assert.equal(s.onTimePct, 100)
    assert.deepEqual(s.derogatory, [{ key: 'CO', label: 'Charge-off', count: 2 }])
  })

  it('tracks the worst and most recent late observation', () => {
    const s = summarize([
      entry(2024, 5, '30'), entry(2024, 6, '90'), entry(2025, 2, '60'),
    ])
    assert.equal(s.worst, '90')
    assert.deepEqual(
      { year: s.mostRecentLate.year, month: s.mostRecentLate.month },
      { year: 2025, month: 2 },
    )
    assert.deepEqual(s.lateCounts, { 30: 1, 60: 1, 90: 1, 120: 0, 150: 0, 180: 0 })
  })

  it('records the first and last derogatory observation', () => {
    const s = summarize([entry(2024, 1, 'CO'), entry(2024, 2, 'OK'), entry(2025, 3, 'COL')])
    assert.equal(s.firstDerogatory.year, 2024)
    assert.equal(s.lastDerogatory.year, 2025)
  })
})

describe('buildGrid', () => {
  it('lays out newest year first with twelve columns and blanks preserved', () => {
    const grid = buildGrid([entry(2025, 1, 'OK'), entry(2026, 3, '30')])
    assert.deepEqual(grid.map(r => r.year), [2026, 2025])
    assert.equal(grid[0].months.length, 12)
    assert.equal(grid[0].months[2].key, '30')   // March 2026
    assert.equal(grid[0].months[0], null)        // January 2026 never reported
    assert.equal(grid[1].months[0].key, 'ok')    // January 2025
  })

  it('is empty when there is no history', () => {
    assert.deepEqual(buildGrid([]), [])
  })

  it('drops rows with an unusable month or year', () => {
    assert.deepEqual(buildGrid([{ year: 'x', month: 1, raw_code: 'OK' }]), [])
    assert.deepEqual(buildGrid([entry(2025, 13, 'OK')]), [])
  })
})

describe('compactPaymentBadge', () => {
  it('leads with derogatory status', () => {
    const badge = compactPaymentBadge(summarize([entry(2025, 1, 'OK'), entry(2025, 2, 'CO')]))
    assert.deepEqual(badge, { text: 'Charge-off', tone: 'red' })
  })

  it('breaks down a small number of lates', () => {
    const badge = compactPaymentBadge(summarize([entry(2025, 1, '30'), entry(2025, 2, '60')]))
    assert.deepEqual(badge, { text: '1×30, 1×60', tone: 'amber' })
  })

  it('summarises many late buckets as a total', () => {
    const badge = compactPaymentBadge(summarize([
      entry(2025, 1, '30'), entry(2025, 2, '60'), entry(2025, 3, '90'),
    ]))
    assert.deepEqual(badge, { text: '3 late payments', tone: 'amber' })
  })

  it('claims 100% on time only with enough reportable months', () => {
    const many = Array.from({ length: 6 }, (_, i) => entry(2025, i + 1, 'OK'))
    assert.deepEqual(compactPaymentBadge(summarize(many)), { text: '100% on time', tone: 'green' })
    // Two clean months is not a track record worth advertising.
    assert.equal(compactPaymentBadge(summarize(many.slice(0, 2))), null)
  })

  it('says nothing when the history supports nothing', () => {
    assert.equal(compactPaymentBadge(summarize([entry(2025, 1, 'ND')])), null)
    assert.equal(compactPaymentBadge(null), null)
  })
})

describe('utilization', () => {
  it('computes only when both numbers exist', () => {
    assert.equal(utilization({ balance: 500, credit_limit: 1000 }), 50)
    assert.equal(utilization({ balance: 500, credit_limit: 0 }), null)
    assert.equal(utilization({ balance: null, credit_limit: 1000 }), null)
    assert.equal(utilization({ balance: 500 }), null)
    assert.equal(utilization(null), null)
  })
})

describe('accountAgeMonths', () => {
  it('measures whole months from the opening date', () => {
    const now = new Date(2026, 8, 24)  // Sep 2026
    assert.equal(accountAgeMonths('Dec 22, 2025', now), 9)
    assert.equal(accountAgeMonths(null, now), null)
    assert.equal(accountAgeMonths('not a date', now), null)
  })
})
