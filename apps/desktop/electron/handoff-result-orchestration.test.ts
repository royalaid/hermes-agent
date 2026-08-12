import assert from 'node:assert/strict'

import { describe, it, vi } from 'vitest'

import type { HandoffResult } from './handoff-result'
import { retryHandoffResultLifecycle, runHandoffResultLifecycle } from './handoff-result-orchestration'

const pending = {
  attemptId: 'attempt-123456789',
  invocationId: 'invocation-123456789',
  leaseId: 'lease-123456789',
  state: 'pending',
  root: 'C:/Hermes',
  relaunch: {
    pid: 42,
    processStartedAt: 1_723_330_000,
    executable: 'C:/Hermes/Hermes.exe'
  }
} as unknown as HandoffResult

const complete = { ...pending, state: 'complete', ok: true } as HandoffResult
const failed = { ...pending, state: 'failed', ok: false } as HandoffResult

function options(overrides: Record<string, unknown> = {}) {
  return {
    currentExecutable: 'C:/Hermes/Hermes.exe',
    currentPid: 42,
    currentProcessStartedAt: 1_723_330_000,
    expectedRoot: 'C:/Hermes',
    getBackendReadiness: () => ({ backendReady: true as const, backendMode: 'local' as const }),
    resourcesPath: 'C:/Hermes/resources',
    discoveryTimeoutMs: 20,
    terminalTimeoutMs: 20,
    pollMs: 10,
    wait: async () => {},
    ...overrides
  }
}

function deps(overrides: Record<string, unknown> = {}) {
  return {
    expectedBuildId: vi.fn(() => 'a'.repeat(40)),
    readBuildProof: vi.fn(() => ({ buildId: 'a'.repeat(40), buildSource: 'install-stamp' as const })),
    readResult: vi.fn(() => pending),
    validateDesktopIdentity: vi.fn(() => ({ root: 'C:/Hermes', executable: 'C:/Hermes/Hermes.exe' })),
    waitForTerminal: vi.fn(async () => complete),
    writeAck: vi.fn(() => ({ attemptId: pending.attemptId })),
    ...overrides
  } as any
}

describe('runHandoffResultLifecycle', () => {
  it('polls through initial absence, ACKs only after backend readiness, and returns terminal complete', async () => {
    let reads = 0
    let readinessChecks = 0
    let resolveTerminal!: (result: HandoffResult) => void

    const terminal = new Promise<HandoffResult>(resolve => {
      resolveTerminal = resolve
    })

    const writeAck = vi.fn((..._args: [string, HandoffResult, { backendMode: 'local' | 'remote' }]) => {
      resolveTerminal(complete)

      return { attemptId: pending.attemptId }
    })

    const d = deps({
      readResult: vi.fn(() => (++reads === 1 ? null : pending)),
      waitForTerminal: vi.fn(() => terminal),
      writeAck
    })

    const result = await runHandoffResultLifecycle(
      'C:/Hermes',
      options({
        getBackendReadiness: () =>
          ++readinessChecks === 1 ? null : { backendReady: true as const, backendMode: 'remote' as const }
      }),
      d
    )

    assert.equal(result, complete)
    assert.ok(reads >= 2)
    assert.ok(readinessChecks >= 2)
    assert.equal(writeAck.mock.calls.length, 1)
    assert.equal(writeAck.mock.calls.at(0)?.[2].backendMode, 'remote')
  })

  it('never treats pending as success and does not ACK before readiness', async () => {
    const writeAck = vi.fn()
    let readinessChecks = 0

    const result = await runHandoffResultLifecycle(
      'C:/Hermes',
      options({
        getBackendReadiness: () => {
          readinessChecks += 1

          return null
        }
      }),
      deps({ waitForTerminal: vi.fn(async () => failed), writeAck })
    )

    assert.equal(result, failed)
    assert.equal(writeAck.mock.calls.length, 0)
    await Promise.resolve()
    assert.equal(readinessChecks, 1)
  })

  it('fails closed on process/build proof and leaves terminal authority to the updater', async () => {
    const writeAck = vi.fn()
    const statuses: string[] = []
    let resolveTerminal!: (result: HandoffResult) => void

    const terminal = new Promise<HandoffResult>(resolve => {
      resolveTerminal = resolve
    })

    const result = await runHandoffResultLifecycle(
      'C:/Hermes',
      options({
        onStatus: status => {
          statuses.push(status)
          resolveTerminal(failed)
        }
      }),
      deps({ validateDesktopIdentity: vi.fn(() => null), waitForTerminal: vi.fn(() => terminal), writeAck })
    )

    assert.equal(result, failed)
    assert.equal(writeAck.mock.calls.length, 0)
    assert.match(statuses.join('\n'), /could not be proven/)
  })

  it('consumes an already-terminal result without publishing an ACK', async () => {
    const waitForTerminal = vi.fn(
      async (..._args: [string, { timeoutMs: number }]) => complete
    )

    const writeAck = vi.fn()

    const result = await runHandoffResultLifecycle(
      'C:/Hermes',
      options(),
      deps({ readResult: vi.fn(() => complete), waitForTerminal, writeAck })
    )

    assert.equal(result, complete)
    assert.equal(waitForTerminal.mock.calls.at(0)?.[1].timeoutMs, 0)
    assert.equal(writeAck.mock.calls.length, 0)
  })
})

describe('retryHandoffResultLifecycle', () => {
  it('retries an unknown process identity and pins the first positive timestamp', async () => {
    const results = [null, null, complete]
    const resolvedProcessStarts: Array<number | null> = [null, 1_723_330_000, 1_800_000_000]
    const lifecycleProcessStarts: Array<number | null> = []
    const waits: number[] = []
    let processIdentityCalls = 0

    const result = await retryHandoffResultLifecycle(
      async currentProcessStartedAt => {
        lifecycleProcessStarts.push(currentProcessStartedAt)

        return results.shift() ?? null
      },
      {
        retryDelayMs: 25,
        resolveCurrentProcessStartedAt: async () =>
          resolvedProcessStarts[Math.min(processIdentityCalls++, resolvedProcessStarts.length - 1)],
        shouldRetryAfterNull: () => true,
        wait: async delay => {
          waits.push(delay)
        }
      }
    )

    assert.equal(result, complete)
    assert.deepEqual(lifecycleProcessStarts, [null, 1_723_330_000, 1_723_330_000])
    assert.equal(processIdentityCalls, 2, 'the first positive exact timestamp stays pinned')
    assert.deepEqual(waits, [25, 25])
  })

  it('stops without spinning when no updater evidence remains', async () => {
    let runs = 0

    const result = await retryHandoffResultLifecycle(
      async () => {
        runs += 1

        return null
      },
      {
        resolveCurrentProcessStartedAt: async () => 1_723_330_000,
        shouldRetryAfterNull: () => false
      }
    )

    assert.equal(result, null)
    assert.equal(runs, 1)
  })
})
