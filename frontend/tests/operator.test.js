// Run with: npm test  (node --test, no test framework dependency)
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  BENCHMARK_CONFIGS, benchmarkRequest, configFor, confirmationFor, isJobRunning,
  jobSummary, paymentByAccount, paymentMisses, pct, usd,
} from '../src/lib/operator.js'

describe('benchmark configs', () => {
  it('offers exactly three, one model each, and no "all"', () => {
    assert.equal(BENCHMARK_CONFIGS.length, 3)
    assert.deepEqual(BENCHMARK_CONFIGS.map(c => c.config), ['A', 'B', 'C'])
    assert.deepEqual(BENCHMARK_CONFIGS.map(c => c.model),
      ['gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'])
    for (const forbidden of ['all', '*', 'every']) {
      assert.ok(!BENCHMARK_CONFIGS.some(c => c.config.toLowerCase() === forbidden))
    }
  })

  it('marks only Sol as expensive', () => {
    assert.deepEqual(BENCHMARK_CONFIGS.filter(c => c.expensive).map(c => c.config), ['C'])
  })
})

describe('confirmationFor', () => {
  it('confirms every paid run', () => {
    for (const { config } of BENCHMARK_CONFIGS) {
      const prompt = confirmationFor(config, 'b0')
      assert.ok(prompt.title.includes('b0'))
      assert.ok(prompt.body.includes('one paid model call'))
      assert.ok(prompt.body.includes('banks nothing'))
    }
  })

  it('says why the expensive one is different rather than reusing one prompt', () => {
    const sol = confirmationFor('C', 'b0')
    const luna = confirmationFor('A', 'b0')
    assert.equal(sol.requiresAck, true)
    assert.equal(luna.requiresAck, false)
    assert.ok(sol.body.includes('expensive escalation model'))
    assert.ok(!luna.body.includes('expensive escalation model'))
    assert.notEqual(sol.confirmLabel, luna.confirmLabel)
  })

  it('returns nothing for a config that does not exist', () => {
    assert.equal(confirmationFor('D', 'b0'), null)
    assert.equal(configFor('all'), null)
  })
})

describe('benchmarkRequest', () => {
  const base = { reportId: 'r1', batchId: 'b0', truth: { accounts: [{ creditor_name: 'X' }] } }

  it('sends one config and never a list', () => {
    const body = benchmarkRequest({ ...base, config: 'A' })
    assert.equal(body.config, 'A')
    assert.equal(body.report_id, 'r1')
    assert.equal(body.batch_id, 'b0')
    assert.ok(!Array.isArray(body.config))
  })

  it('only acknowledges the expense for the config that requires it', () => {
    assert.equal(benchmarkRequest({ ...base, config: 'A' }).acknowledge_expensive, undefined)
    assert.equal(benchmarkRequest({ ...base, config: 'B' }).acknowledge_expensive, undefined)
    assert.equal(benchmarkRequest({ ...base, config: 'C' }).acknowledge_expensive, true)
  })

  it('omits an idempotency key rather than sending an empty one', () => {
    assert.equal('idempotency_key' in benchmarkRequest({ ...base, config: 'A' }), false)
    assert.equal(
      benchmarkRequest({ ...base, config: 'A', idempotencyKey: 'k1' }).idempotency_key, 'k1')
  })
})

describe('job status', () => {
  it('treats anything not terminal as still running', () => {
    assert.equal(isJobRunning('queued'), true)
    assert.equal(isJobRunning('running'), true)
    assert.equal(isJobRunning('succeeded'), false)
    assert.equal(isJobRunning('failed'), false)
    assert.equal(isJobRunning(null), false)
    // A status added server-side must not read as finished here.
    assert.equal(isJobRunning('claiming'), true)
  })
})

describe('formatting', () => {
  it('shows an em dash rather than a fake zero', () => {
    assert.equal(pct(null), '—')
    assert.equal(pct({ accuracy: null }), '—')
    assert.equal(pct({ accuracy: 0.989 }), '98.9%')
    assert.equal(usd(null), '—')
    assert.equal(usd(0.0163), '$0.0163')
  })
})

describe('payment detail', () => {
  const result = {
    payment_history: {
      correct: 4, total: 12,
      by_account: {
        'TRADELINE 2': { expected: 3, extracted: 2, correct: 1 },
        'TRADELINE 1': { expected: 3, extracted: 2, correct: 1 },
      },
      misses: [
        { account: 'TRADELINE 1', month: '2026-04', expected: 'OK', got: '30', missing: false },
        { account: 'TRADELINE 1', month: '2026-05', expected: 'OK', got: null, missing: true },
      ],
    },
  }

  it('distinguishes a month read wrong from one never returned', () => {
    const misses = paymentMisses(result)
    assert.equal(misses.length, 2)
    assert.deepEqual(misses[0], {
      account: 'TRADELINE 1', month: '2026-04', expected: 'OK', got: '30', missing: false,
    })
    assert.equal(misses[1].got, 'MISSING')
    assert.equal(misses[1].missing, true)
  })

  it('sorts per-account counts for a stable render', () => {
    assert.deepEqual(paymentByAccount(result).map(a => a.account),
      ['TRADELINE 1', 'TRADELINE 2'])
  })

  it('is empty rather than throwing when there is no history', () => {
    assert.deepEqual(paymentMisses({}), [])
    assert.deepEqual(paymentMisses(null), [])
    assert.deepEqual(paymentByAccount({}), [])
  })
})

describe('jobSummary', () => {
  it('shows the failure message for a failed job', () => {
    assert.equal(jobSummary({ status: 'failed', error: { message: 'The AI provider was unavailable.' } }),
      'The AI provider was unavailable.')
  })

  it('shows the measurement for a finished one', () => {
    const summary = jobSummary({
      status: 'succeeded',
      result: {
        accounts: { matched: 4, asked: 4 },
        field_accuracy: { accuracy: 0.989 },
        payment_history: { accuracy: 0.478 },
      },
    })
    assert.equal(summary, '4/4 matched · fields 98.9% · payments 47.8%')
  })

  it('says it is running while it is', () => {
    assert.equal(jobSummary({ status: 'queued' }), 'Running…')
  })
})
