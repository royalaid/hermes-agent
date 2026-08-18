import { describe, expect, it, vi } from 'vitest'

import {
  discardPerfProbeSession,
  dispatchPerfProbeGatewayEvent,
  registerPerfProbeGatewayHandler,
  seedPerfProbeSession
} from './perf-probe-bridge'

const event = { session_id: 'runtime-test', type: 'subagent.start' }

describe('perf probe gateway bridge', () => {
  it('dispatches and seeds through the registered wiring callbacks, then removes them on cleanup', () => {
    const handler = vi.fn()
    const seedSession = vi.fn()
    const discardSession = vi.fn()
    const cleanup = registerPerfProbeGatewayHandler(handler, seedSession, discardSession)

    expect(dispatchPerfProbeGatewayEvent(event)).toBe(true)
    expect(handler).toHaveBeenCalledWith(event)
    expect(seedPerfProbeSession('runtime-test', [])).toBe(true)
    expect(seedSession).toHaveBeenCalledWith('runtime-test', [])
    expect(discardPerfProbeSession('runtime-test')).toBe(true)
    expect(discardSession).toHaveBeenCalledWith('runtime-test')

    cleanup()
    expect(dispatchPerfProbeGatewayEvent(event)).toBe(false)
    expect(seedPerfProbeSession('runtime-test', [])).toBe(false)
    expect(discardPerfProbeSession('runtime-test')).toBe(false)
  })

  it('does not let stale cleanup unregister a newer registration', () => {
    const first = vi.fn()
    const second = vi.fn()
    const cleanupFirst = registerPerfProbeGatewayHandler(first, vi.fn(), vi.fn())
    const cleanupSecond = registerPerfProbeGatewayHandler(second, vi.fn(), vi.fn())

    cleanupFirst()
    expect(dispatchPerfProbeGatewayEvent(event)).toBe(true)
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledWith(event)

    cleanupSecond()
    expect(dispatchPerfProbeGatewayEvent(event)).toBe(false)
  })
})
