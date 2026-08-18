import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('perf probe fanout contract', () => {
  it('drives the real gateway handler while rendering and cleaning isolated session tiles', () => {
    const source = readFileSync('src/app/chat/perf-probe.tsx', 'utf8')

    expect(source).toContain('dispatchPerfProbeGatewayEvent, seedPerfProbeSession')
    expect(source).toContain("from './perf-probe-bridge'")
    expect(source).toContain('seedPerfProbeSession(sessionId, transcript)')
    expect(source).toContain('dispatchEvent: event => dispatchPerfProbeGatewayEvent(event)')
    expect(source).not.toContain('emitLocalGatewayEvent')
    expect(source).not.toContain('upsertSubagents')
    expect(source).toContain('__HERMES_SESSION_TILES__')
    expect(source).toContain("'perf-fanout-visible'")
    expect(source).toContain("'perf-fanout-control'")
    expect(source).toContain("'perf-fanout-control-runtime'")
    expect(source).toContain('tiles.open(visibleStoredId')
    expect(source).toContain('tiles.open(controlStoredId')
    expect(source).toContain('tiles.patch(visibleStoredId, { runtimeId: sessionId })')
    expect(source).toContain('tiles.publish(sessionId')
    expect(source).toContain('tiles.close(visibleStoredId)')
    expect(source).toContain('tiles.close(controlStoredId)')
    expect(source).toContain('tiles.drop(controlRuntimeId)')
    expect(source).toContain('subagents: $subagentsBySession.get()')
    expect(source).toContain('discardPerfProbeSession(fanoutRuntimeId)')
    expect(source.indexOf('discardPerfProbeSession(fanoutRuntimeId)')).toBeLessThan(
      source.indexOf('$subagentsBySession.set(baseline.subagents)')
    )

    const wiring = readFileSync('src/app/contrib/wiring.tsx', 'utf8')
    expect(wiring).toContain('discardQueuedStreamState(sessionId)')
    expect(wiring).toContain('sessionStateByRuntimeIdRef.current.delete(sessionId)')
    expect(wiring.indexOf('discardQueuedStreamState(sessionId)')).toBeLessThan(
      wiring.indexOf('sessionStateByRuntimeIdRef.current.delete(sessionId)')
    )
    expect(source).toContain('$subagentsBySession.set(baseline.subagents)')
    expect(source).toContain('buildSubagentFanoutBatches(options)')
    expect(source).toContain('for (const event of batch)')
    expect(source).toContain('holdTerminal')
    expect(source).toContain('pendingTerminalBatch')
    expect(source).toContain('releaseFanoutTerminal')
    expect(source).toContain('stateSnapshot')
  })
})
