// Run with: npm test  (node --test, no test framework dependency)
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  PIPELINE, POLL_TIMEOUT_MS, STAGE_LABELS, TERMINAL_STAGES,
  isProcessing, pollDelayMs, pollTimedOut, stageLabel, stageProgress,
} from '../src/lib/processing.js'

describe('isProcessing', () => {
  it('treats every pipeline stage as still running', () => {
    for (const stage of PIPELINE) assert.equal(isProcessing(stage), true, stage)
  })

  it('treats every extraction outcome as finished', () => {
    for (const stage of TERMINAL_STAGES) assert.equal(isProcessing(stage), false, stage)
  })

  it('is false with no stage at all', () => {
    assert.equal(isProcessing(null), false)
    assert.equal(isProcessing(undefined), false)
    assert.equal(isProcessing(''), false)
  })

  it('errs toward "still running" for a stage it does not know', () => {
    // A stage added on the server must not read as a finished report here.
    assert.equal(isProcessing('uploading_to_provider'), true)
  })
})

describe('stageLabel', () => {
  it('names every stage in the consumer\'s terms', () => {
    for (const stage of PIPELINE) {
      assert.equal(stageLabel(stage), STAGE_LABELS[stage])
      assert.ok(stageLabel(stage).length > 0)
    }
  })

  it('never leaks a provider, model or error code', () => {
    const wording = Object.values(STAGE_LABELS).join(' ').toLowerCase()
    for (const leak of ['openai', 'gpt', 'anthropic', '429', 'quota', 'token', 'api']) {
      assert.ok(!wording.includes(leak), `"${leak}" leaked into a stage label`)
    }
  })

  it('has a fallback for an unknown running stage and none for a finished one', () => {
    assert.equal(stageLabel('something_new'), 'Working on your report')
    assert.equal(stageLabel('verified'), null)
  })
})

describe('stageProgress', () => {
  it('increases monotonically through the pipeline', () => {
    const values = PIPELINE.map(stageProgress)
    for (let i = 1; i < values.length; i++) assert.ok(values[i] > values[i - 1])
    assert.ok(values[0] > 0 && values[values.length - 1] < 100)
  })

  it('is complete for a finished report', () => {
    for (const stage of TERMINAL_STAGES) assert.equal(stageProgress(stage), 100)
  })
})

describe('pollDelayMs', () => {
  it('starts responsive and backs off to a ceiling', () => {
    assert.equal(pollDelayMs(0), 1500)
    assert.ok(pollDelayMs(1) > pollDelayMs(0))
    assert.equal(pollDelayMs(50), 8000)
    for (let i = 0; i < 50; i++) assert.ok(pollDelayMs(i) <= 8000)
  })

  it('never returns a nonsensical delay', () => {
    assert.equal(pollDelayMs(-5), 1500)
  })
})

describe('pollTimedOut', () => {
  it('keeps polling for a long extraction, then gives up', () => {
    const start = 1_000_000
    assert.equal(pollTimedOut(start, start + 60_000), false)
    assert.equal(pollTimedOut(start, start + POLL_TIMEOUT_MS), true)
  })
})
