import { describe, expect, it } from 'vitest'

import { SCENARIOS } from './perf/scenarios/index.mjs'
import { firstVisibleElement, requiredInteractionMs } from './perf/scenarios/subagent-fanout.mjs'

describe('desktop perf scenario registry', () => {
  it('registers the isolated fanout workload and independent code-scroll control', () => {
    expect(SCENARIOS['subagent-fanout']).toMatchObject({
      name: 'subagent-fanout',
      tier: 'report'
    })
    expect(SCENARIOS['code-scroll-control']).toMatchObject({
      name: 'code-scroll-control',
      tier: 'report'
    })
  })

  it('rejects missing or non-finite required interaction metrics', () => {
    expect(() => requiredInteractionMs(null, 'composer')).toThrow('composer')
    expect(() => requiredInteractionMs({ hostMs: Number.NaN }, 'composer')).toThrow('composer')
    expect(requiredInteractionMs({ hostMs: 42.25 }, 'composer')).toBe(42.3)
  })

  it('targets the visible copy when primary and tile controls coexist', () => {
    const hidden = {
      checkVisibility: () => false,
      getBoundingClientRect: () => ({ height: 40, width: 200 })
    }
    const visible = {
      checkVisibility: () => true,
      getBoundingClientRect: () => ({ height: 40, width: 200 })
    }

    expect(firstVisibleElement([hidden, visible])).toBe(visible)
    expect(firstVisibleElement([hidden])).toBeNull()
  })
})
