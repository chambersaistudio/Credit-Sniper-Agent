// Run with: npm test  (node --test, no test framework dependency)
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { buildStamp, notFoundMessage } from '../src/lib/build.js'

describe('buildStamp', () => {
  it('identifies the commit a bundle was built from', () => {
    assert.equal(
      buildStamp({ commit: '4b2217fcb0dfd2ec61432129051680d4c45ff7f5',
                   ref: 'claude/credit-dispute-agent-u0yyq3',
                   builtAt: '2026-09-27T04:12:00.000Z' }),
      '4b2217f · claude/credit-dispute-agent-u0yyq3 · 2026-09-27')
  })

  it('says "dev build" rather than inventing a commit', () => {
    assert.equal(buildStamp({ commit: '', ref: '', builtAt: '' }), 'dev build')
    assert.equal(buildStamp({}), 'dev build')
  })

  it('works with a commit and nothing else', () => {
    assert.equal(buildStamp({ commit: 'e5527aa04c144e1c997d33bc' }), 'e5527aa')
  })
})

describe('notFoundMessage', () => {
  // The failure this exists to prevent: /operator rendered an EMPTY content
  // area on production, because the production alias pointed at a deployment
  // older than the commit that added the route. Blank is indistinguishable
  // from a crash, and says nothing about the cause.
  const build = { commit: 'e5527aa04c144e1c997d33bc', ref: 'claude/x', builtAt: '' }

  it('names the path instead of rendering nothing', () => {
    const message = notFoundMessage('/operator', build)
    assert.equal(message.path, '/operator')
    assert.ok(message.title.length > 0)
  })

  it('names the build, so a stale bundle identifies itself', () => {
    assert.equal(notFoundMessage('/operator', build).stamp, 'e5527aa · claude/x')
  })

  it('points at the actual cause rather than blaming the URL', () => {
    const { hint } = notFoundMessage('/operator', build)
    assert.match(hint, /older than the commit/)
    assert.match(hint, /production alias/)
  })
})
