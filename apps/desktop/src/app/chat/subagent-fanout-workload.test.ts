import { describe, expect, it } from 'vitest'

import {
  buildSubagentFanoutBatches,
  buildSubagentFanoutEvents,
  normalizeSubagentFanoutOptions
} from './subagent-fanout-workload'

describe('subagent fanout perf workload', () => {
  it('normalizes bounded deterministic defaults', () => {
    expect(normalizeSubagentFanoutOptions({})).toEqual({
      holdTerminal: false,
      intervalMs: 33,
      seed: 1,
      turns: 20,
      updates: 12,
      workers: 1
    })
    expect(normalizeSubagentFanoutOptions({ holdTerminal: true, intervalMs: 0, seed: 7, turns: -1, updates: 9999, workers: 99 })).toEqual({
      holdTerminal: true,
      intervalMs: 1,
      seed: 7,
      turns: 1,
      updates: 240,
      workers: 8
    })
  })

  it('groups one event per worker into each shared cadence tick', () => {
    const options = normalizeSubagentFanoutOptions({ intervalMs: 33, seed: 41, updates: 4, workers: 3 })
    const batches = buildSubagentFanoutBatches(options)

    expect(batches).toHaveLength(6)
    expect(batches.map(batch => batch.length)).toEqual([3, 3, 3, 3, 3, 3])
    expect(batches.map(batch => [...new Set(batch.map(event => event.type))])).toEqual([
      ['subagent.start'],
      ['subagent.progress'],
      ['subagent.thinking'],
      ['subagent.tool'],
      ['subagent.progress'],
      ['subagent.complete']
    ])
    expect(batches.flat()).toEqual(buildSubagentFanoutEvents(options))
  })

  it('builds the same typed lifecycle for the same seed', () => {
    const options = normalizeSubagentFanoutOptions({ seed: 41, updates: 4, workers: 2 })
    const first = buildSubagentFanoutEvents(options)
    const second = buildSubagentFanoutEvents(options)

    expect(first).toEqual(second)
    expect(first.map(event => event.type)).toEqual([
      'subagent.start',
      'subagent.start',
      'subagent.progress',
      'subagent.progress',
      'subagent.thinking',
      'subagent.thinking',
      'subagent.tool',
      'subagent.tool',
      'subagent.progress',
      'subagent.progress',
      'subagent.complete',
      'subagent.complete'
    ])
    expect(new Set(first.map(event => event.session_id))).toEqual(new Set(['perf-subagent-fanout']))
    expect(first.at(-1)?.payload).toMatchObject({ status: 'completed', subagent_id: 'perf-worker-2' })
    expect(first.filter(event => event.type === 'subagent.start').map(event => event.payload.goal)).toEqual([
      'Inspect deterministic fanout worker 1',
      'Inspect deterministic fanout worker 2'
    ])
  })
})
