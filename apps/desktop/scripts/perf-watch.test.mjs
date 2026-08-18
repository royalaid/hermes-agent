import { describe, expect, it, vi } from 'vitest'

import {
  collectWatchSnapshots,
  isWatchMainModule,
  parseWatchArgs,
  safeTargetIdentity,
  selectSingleWatchTarget
} from './perf/watch.mjs'

describe('perf watch arguments', () => {
  it('requires an explicit CDP port and bounds sampling', () => {
    expect(() => parseWatchArgs([])).toThrow('--port')
    expect(parseWatchArgs(['--port', '19322', '--samples', '999'])).toMatchObject({ port: 19322, samples: 60 })
  })

  it('fails closed on ambiguous targets and emits non-content identity', () => {
    const target = {
      id: 'page-1',
      title: 'Sensitive transcript title',
      type: 'page',
      url: 'http://127.0.0.1:15210/session/secret',
      webSocketDebuggerUrl: 'ws://127.0.0.1:19360/devtools/page/page-1'
    }

    expect(() => selectSingleWatchTarget([target, { ...target, id: 'page-2' }], 19360)).toThrow('ambiguous')
    expect(safeTargetIdentity(selectSingleWatchTarget([target], 19360), 19360)).toEqual({
      port: 19360,
      targetId: 'page-1',
      type: 'page'
    })
  })

  it('recognizes a relative Windows CLI entry path as the running module', () => {
    const moduleUrl = new URL('./perf/watch.mjs', import.meta.url).href

    expect(isWatchMainModule(moduleUrl, 'scripts/perf/watch.mjs', process.cwd())).toBe(true)
    expect(isWatchMainModule(moduleUrl, 'scripts/perf/run.mjs', process.cwd())).toBe(false)
  })

  it('emits bounded redacted NDJSON using Runtime.evaluate only', async () => {
    const lines = []
    const cdp = {
      eval: vi.fn(async () => ({ dom: { composer: 1 }, heapMb: 12 })),
      send: vi.fn()
    }

    await collectWatchSnapshots(
      cdp,
      { intervalMs: 100, port: 19360, samples: 2, target: { port: 19360, targetId: 'page-1', type: 'page' } },
      line => lines.push(line),
      async () => undefined
    )

    expect(cdp.eval).toHaveBeenCalledTimes(2)
    expect(cdp.send).not.toHaveBeenCalled()
    expect(lines.map(line => JSON.parse(line))).toEqual([
      {
        sample: 1,
        snapshot: { dom: { composer: 1 }, heapMb: 12 },
        target: { port: 19360, targetId: 'page-1', type: 'page' }
      },
      {
        sample: 2,
        snapshot: { dom: { composer: 1 }, heapMb: 12 },
        target: { port: 19360, targetId: 'page-1', type: 'page' }
      }
    ])
  })
})
