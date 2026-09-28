// Run with: npm test  (node --test, no test framework dependency)
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  BENCHMARK_CONFIGS, DEFAULT_TRUTH_LABEL, benchmarkBlockedReason, benchmarkRequest,
  configFor, confirmationFor, draftConfirmationFor, draftInFlight, isJobRunning, jobSummary,
  modelLabel, newIdempotencyKey, parseTruthText, paymentByAccount, paymentMisses, pct,
  shortFingerprint, truthCounts, truthDraftRequest, truthFor, truthNextStep, truthStatus,
  truthToText, usd,
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


describe('benchmark truth travels by reference', () => {
  const base = { reportId: 'r1', batchId: 'b0', config: 'A' }

  it('sends a label and no account values when truth is stored', () => {
    const body = benchmarkRequest({ ...base, truthLabel: DEFAULT_TRUTH_LABEL })
    assert.equal(body.truth_label, 'current')
    assert.equal('truth' in body, false)
  })

  it('still supports inline truth for the CLI', () => {
    const body = benchmarkRequest({ ...base, truth: { accounts: [{ creditor_name: 'X' }] } })
    assert.deepEqual(body.truth, { accounts: [{ creditor_name: 'X' }] })
    assert.equal('truth_label' in body, false)
  })

  it('omits both rather than sending an empty one', () => {
    const body = benchmarkRequest(base)
    assert.equal('truth' in body, false)
    assert.equal('truth_label' in body, false)
  })
})

describe('truthFor', () => {
  const summaries = [
    { batch_id: 'b0', label: 'current', verified: true },
    { batch_id: 'b0', label: 'strict', verified: false },
    { batch_id: 'b1', label: 'current', verified: false },
  ]

  it('picks the batch and label asked for', () => {
    assert.equal(truthFor(summaries, 'b0').label, 'current')
    assert.equal(truthFor(summaries, 'b0', 'strict').verified, false)
    assert.equal(truthFor(summaries, 'b1').verified, false)
  })

  it('returns null for a batch with nothing stored', () => {
    assert.equal(truthFor(summaries, 'b3'), null)
    assert.equal(truthFor([], 'b0'), null)
    assert.equal(truthFor(null, 'b0'), null)
  })
})

describe('truthStatus', () => {
  it('refuses to benchmark with nothing stored', () => {
    const status = truthStatus(null)
    assert.equal(status.state, 'missing')
    assert.equal(status.canBenchmark, false)
  })

  it('refuses to benchmark against truth nobody has verified', () => {
    const status = truthStatus({ verified: false, source: 'operator', account_count: 4 })
    assert.equal(status.state, 'unverified')
    assert.equal(status.canBenchmark, false)
  })

  it('says a draft came from the model, because that is the trap', () => {
    const status = truthStatus({ verified: false, source: 'drafted_from_batch' })
    assert.equal(status.canBenchmark, false)
    assert.match(status.label, /Draft/)
    assert.match(status.detail, /model's own read/)
    // A drafted entry must never read the same as one a human entered.
    assert.notEqual(status.label, truthStatus({ verified: false, source: 'operator' }).label)
  })

  it('allows it only once verified', () => {
    const status = truthStatus({ verified: true, source: 'operator' })
    assert.equal(status.state, 'verified')
    assert.equal(status.canBenchmark, true)
  })

  it('a verified draft is still usable — verifying is the human act', () => {
    assert.equal(truthStatus({ verified: true, source: 'drafted_from_batch' }).canBenchmark, true)
  })
})

describe('benchmarkBlockedReason', () => {
  it('names what is missing so the button explains itself', () => {
    assert.match(benchmarkBlockedReason(null), /Save and verify/)
    assert.match(benchmarkBlockedReason({ verified: false }), /Verify/)
    assert.equal(benchmarkBlockedReason({ verified: true }), null)
  })
})

describe('truth summaries carry no account values', () => {
  it('renders counts and a short fingerprint only', () => {
    const summary = {
      batch_id: 'b0', label: 'current', account_count: 4, months: 36,
      verified: true, source: 'operator',
      fingerprint: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
    }
    assert.equal(truthCounts(summary), '4 accounts · 36 months')
    assert.equal(truthCounts({ account_count: 1, months: 0 }), '1 account')
    assert.equal(shortFingerprint(summary), 'abcdef01')
    assert.equal(shortFingerprint(null), '—')
    // Nothing a summary renders may be an account value.
    const rendered = truthCounts(summary) + shortFingerprint(summary)
      + truthStatus(summary).label + truthStatus(summary).detail
    for (const value of ['CREDIT ACCEPTANCE', '1234', '$7,684']) {
      assert.ok(!rendered.includes(value))
    }
  })
})

describe('truth editing', () => {
  it('prefills the editor from stored values so correcting is not retyping', () => {
    const text = truthToText({ accounts: [{ creditor_name: 'X', balance: '$0' }] })
    assert.deepEqual(JSON.parse(text), { accounts: [{ creditor_name: 'X', balance: '$0' }] })
    assert.ok(text.includes('\n'))  // indented for reading on a phone
    assert.equal(truthToText(null), '')
  })

  it('explains a bad edit instead of failing the save', () => {
    assert.match(parseTruthText('{oops').error, /valid JSON/)
    assert.match(parseTruthText('{"accounts": []}').error, /at least one account/)
    assert.match(parseTruthText('{"accounts": [{"balance": "$0"}]}').error,
      /Account 1 has no creditor_name/)
    assert.match(parseTruthText('{"accounts": [{"creditor_name": "  "}]}').error,
      /no creditor_name/)
  })

  it('returns the accounts for a good edit', () => {
    const parsed = parseTruthText('{"accounts": [{"creditor_name": "X"}]}')
    assert.equal(parsed.error, undefined)
    assert.deepEqual(parsed.accounts, [{ creditor_name: 'X' }])
  })
})


describe('truthNextStep — what the panel offers when there is no truth', () => {
  it('offers the paid draft, not a blank editor, when nothing is banked', () => {
    // The production dead end: no truth, no banked batch. A blank Save flow
    // read as the next step and meant typing every account into JSON.
    const next = truthNextStep({ summary: null, banked: false })
    assert.equal(next.step, 'draft-with-model')
    assert.match(next.detail, /no banked extraction/)
    assert.match(next.detail, /banks nothing/)
    assert.match(next.detail, /unverified/)
  })

  it('offers the free draft when the batch is banked', () => {
    assert.equal(truthNextStep({ summary: null, banked: true }).step, 'draft-from-banked')
  })

  it('moves on to correcting and verifying once something is stored', () => {
    assert.equal(truthNextStep({ summary: { verified: false }, banked: false }).step,
      'correct-and-verify')
    assert.equal(truthNextStep({ summary: { verified: true }, banked: true }).step, 'ready')
  })
})

describe('truthDraftRequest', () => {
  it('sends one config and acknowledges only Sol', () => {
    const base = { reportId: 'r1', batchId: 'b0' }
    assert.deepEqual(truthDraftRequest({ ...base, config: 'A' }),
      { report_id: 'r1', batch_id: 'b0', config: 'A' })
    assert.equal(truthDraftRequest({ ...base, config: 'B' }).acknowledge_expensive, undefined)
    assert.equal(truthDraftRequest({ ...base, config: 'C' }).acknowledge_expensive, true)
  })

  it('never sends account values — a draft is read from the document', () => {
    const body = truthDraftRequest({ reportId: 'r1', batchId: 'b0', config: 'A' })
    assert.equal('truth' in body, false)
  })
})

describe('draftConfirmationFor', () => {
  it('says it is paid, banks nothing, and is only a draft', () => {
    const prompt = draftConfirmationFor('A', 'b0')
    assert.match(prompt.title, /b0/)
    assert.match(prompt.body, /one paid model call/)
    assert.match(prompt.body, /banks nothing/)
    assert.match(prompt.body, /correct and verify/)
  })

  it('warns up front that the drafting model will be flagged against its own read', () => {
    assert.match(draftConfirmationFor('A', 'b0').body, /Luna benchmark against it will be flagged/)
  })

  it('keeps the Sol wording distinct', () => {
    const sol = draftConfirmationFor('C', 'b0')
    assert.equal(sol.requiresAck, true)
    assert.match(sol.body, /expensive escalation model/)
    assert.notEqual(sol.confirmLabel, draftConfirmationFor('A', 'b0').confirmLabel)
    assert.equal(draftConfirmationFor('D', 'b0'), null)
  })
})

describe('drafted truth status', () => {
  it('names the model that drafted it', () => {
    const status = truthStatus({ verified: false, source: 'drafted_from_model',
                                 drafted_by_model: 'gpt-5.6-terra' })
    assert.equal(status.canBenchmark, false)
    assert.match(status.label, /Draft/)
    assert.match(status.detail, /by Terra/)
  })

  it('stays flagged after it is verified', () => {
    const status = truthStatus({ verified: true, source: 'operator',
                                 drafted_by_model: 'gpt-5.6-luna' })
    assert.equal(status.canBenchmark, true)
    assert.match(status.detail, /Luna benchmark against it is flagged/)
  })

  it('maps model ids to their labels', () => {
    assert.equal(modelLabel('gpt-5.6-sol'), 'Sol')
    assert.equal(modelLabel('some-other-model'), 'some-other-model')
    assert.equal(modelLabel(null), '')
  })
})

describe('draftInFlight', () => {
  const jobs = [
    { operation: 'draft_truth_batch', status: 'running', request: { batch_id: 'b0' } },
    { operation: 'draft_truth_batch', status: 'succeeded', request: { batch_id: 'b1' } },
    { operation: 'benchmark_batch', status: 'running', request: { batch_id: 'b1' } },
  ]

  it('finds a running draft for this batch only', () => {
    assert.ok(draftInFlight(jobs, 'b0'))
    assert.equal(draftInFlight(jobs, 'b1'), null)  // finished draft, running benchmark
    assert.equal(draftInFlight([], 'b0'), null)
    assert.equal(draftInFlight(null, 'b0'), null)
  })
})

describe('newIdempotencyKey', () => {
  it('is unique per confirmation and says what it bought', () => {
    const a = newIdempotencyKey('draft', 'b0', 'A')
    const b = newIdempotencyKey('draft', 'b0', 'A')
    assert.notEqual(a, b)
    assert.ok(a.startsWith('draft-b0-A-'))
    assert.ok(a.length <= 200, 'the server caps keys at 200 characters')
  })
})

describe('jobSummary for a draft', () => {
  it('reports counts and the next step, never values', () => {
    assert.equal(jobSummary({
      operation: 'draft_truth_batch', status: 'succeeded',
      result: { accounts_drafted: 4, months_drafted: 36 },
    }), 'Drafted 4 accounts · 36 months — correct and verify')
  })
})
