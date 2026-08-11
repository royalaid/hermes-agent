import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import type { McpBridgeQuiesceLease } from './mcp-bridge-quiesce'
import {
  buildMcpBridgeConsentRequest,
  runWindowsUpdatePreflight,
  type UpdatePreflightDeps,
  type UpdatePreflightPurpose
} from './update-preflight'
import type { McpBridgeProcess, ScanOutcome, VenvBlockerScanResult } from './venv-blocker-scan'

const PURPOSES: UpdatePreflightPurpose[] = ['normal-update', 'bootstrap-recovery']

const lease: McpBridgeQuiesceLease = {
  schemaVersion: 1,
  leaseId: 'lease-123',
  ownerPid: 777,
  createdAt: 100,
  expiresAt: 1_300,
  handoffGraceUntil: 100,
  installRoot: String.raw`C:\Hermes`
}

const bridge = (overrides: Partial<McpBridgeProcess> = {}): McpBridgeProcess => ({
  pid: 101,
  name: 'python.exe',
  cmdline: 'python.exe -m agent.transports.hermes_tools_mcp_server',
  createdAt: 123.5,
  owner: 'codex',
  role: 'mcp_bridge_wrapper',
  actionable: true,
  actionability: 'exact_mcp_bridge',
  action: 'terminate_exact_mcp',
  ...overrides
})

const result = (overrides: Partial<VenvBlockerScanResult> = {}): VenvBlockerScanResult => ({
  blocked: false,
  processes: [],
  mcpBridges: [],
  pausableGateways: 0,
  ...overrides
})

const clear = (): ScanOutcome => ({ kind: 'clear', result: result() })

const blockedByBridges = (bridges = [bridge()]): ScanOutcome => ({
  kind: 'blocked',
  result: result({ blocked: true, mcpBridges: bridges })
})

function makeDeps(scans: ScanOutcome[], overrides: Partial<UpdatePreflightDeps> = {}) {
  const calls: string[] = []
  const queue = [...scans]

  const deps: UpdatePreflightDeps = {
    releaseTrackedBackendTrees: async () => {
      calls.push('release')

      return { unlocked: true }
    },
    scan: async () => {
      calls.push('scan')
      const next = queue.shift()
      assert.ok(next, 'test provided too few scanner outcomes')

      return next
    },
    requestMcpBridgeConsent: async () => {
      calls.push('consent')

      return true
    },
    acquireMcpBridgeLease: () => {
      calls.push('lease')

      return lease
    },
    clearMcpBridgeLease: current => {
      calls.push(`clear-lease:${current.leaseId}`)
    },
    terminateMcpBridge: async current => {
      calls.push(`terminate:${current.pid}:${current.createdAt}`)

      return true
    },
    wait: async delay => {
      calls.push(`wait:${delay}`)
    },
    ...overrides
  }

  return { calls, deps }
}

describe.each(PURPOSES)('runWindowsUpdatePreflight (%s)', purpose => {
  it('fails closed before scanning when tracked backend trees do not unlock', async () => {
    const { calls, deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => {
        calls.push('release')

        return { unlocked: false }
      }
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'unlock-failed')
    assert.deepEqual(calls, ['release'])
  })

  it('does not coerce a malformed unlock result into permission to scan', async () => {
    const { calls, deps } = makeDeps([], {
      releaseTrackedBackendTrees: (async () => {
        calls.push('release')

        return { unlocked: 'true' }
      }) as any
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'unlock-failed')
    assert.deepEqual(calls, ['release'])
  })

  it('returns a typed probe failure before acquiring a prevention lease', async () => {
    const { calls, deps } = makeDeps([{ kind: 'probe-failure', error: 'scanner crashed' }])

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'probe-failure')
    assert.equal(outcome.error, 'scanner crashed')
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('refuses generic holders without offering the MCP consent path', async () => {
    const generic = result({
      blocked: true,
      processes: [{ pid: 202, name: 'python.exe', cmdline: 'python.exe user-script.py' }]
    })

    const { calls, deps } = makeDeps([{ kind: 'blocked', result: generic }])

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'holders')
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('refuses MCP entries without proven agent ownership and exact actionability', async () => {
    const unproven = blockedByBridges([bridge({ owner: 'unknown' })])
    const { calls, deps } = makeDeps([unproven])

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'holders')
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('never consents to or terminates an MCP entry missing scanner actionability proof', async () => {
    const unproven = bridge()
    delete (unproven as Partial<McpBridgeProcess>).actionable
    delete (unproven as Partial<McpBridgeProcess>).actionability
    delete (unproven as Partial<McpBridgeProcess>).action
    const { calls, deps } = makeDeps([blockedByBridges([unproven])])

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'holders')
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('does not activate the prevention lease when the user declines bridge interruption', async () => {
    const { calls, deps } = makeDeps([blockedByBridges()], {
      requestMcpBridgeConsent: async request => {
        calls.push(`consent:${request.ownerLabel}`)

        return false
      }
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'consent-declined')
    assert.deepEqual(calls, ['release', 'scan', 'consent:Codex'])
  })
})

describe('MCP bridge drain', () => {
  it('gets consent before activating the lease, then lets cooperative exit win', async () => {
    const { calls, deps } = makeDeps([blockedByBridges(), clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 900,
      respawnIntervalMs: 1_100,
      terminationSettleMs: 700
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(outcome.lease?.leaseId, lease.leaseId)
    assert.deepEqual(calls, ['release', 'scan', 'consent', 'lease', 'wait:900', 'scan', 'wait:1100', 'scan'])
  })

  it('uses one PID/create-time fallback pass for exact wrappers and workers, never a process tree', async () => {
    const wrapper = bridge()
    const worker = bridge({ pid: 102, createdAt: 124.5, role: 'mcp_bridge_worker' })
    const stillRunning = blockedByBridges([wrapper, worker])
    const { calls, deps } = makeDeps([stillRunning, stillRunning, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('bootstrap-recovery', deps, {
      cooperativeExitMs: 900,
      respawnIntervalMs: 1_100,
      terminationSettleMs: 700
    })

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'consent',
      'lease',
      'wait:900',
      'scan',
      'terminate:102:124.5',
      'terminate:101:123.5',
      'wait:700',
      'scan',
      'wait:1100',
      'scan'
    ])
  })

  it('awaits each worker-first termination before starting the next bridge', async () => {
    const wrapper = bridge()
    const worker = bridge({ pid: 102, createdAt: 124.5, role: 'mcp_bridge_worker' })
    const stillRunning = blockedByBridges([wrapper, worker])
    let releaseWorker!: () => void
    let workerStarted!: () => void

    const workerStartedPromise = new Promise<void>(resolve => {
      workerStarted = resolve
    })

    const workerRelease = new Promise<void>(resolve => {
      releaseWorker = resolve
    })

    let activeTerminations = 0
    let maximumConcurrentTerminations = 0

    const { calls, deps } = makeDeps([stillRunning, stillRunning, clear(), clear()], {
      terminateMcpBridge: async current => {
        activeTerminations += 1
        maximumConcurrentTerminations = Math.max(maximumConcurrentTerminations, activeTerminations)
        calls.push(`terminate-start:${current.pid}`)

        if (current.role === 'mcp_bridge_worker') {
          workerStarted()
          await workerRelease
        }

        calls.push(`terminate-end:${current.pid}`)
        activeTerminations -= 1

        return true
      }
    })

    const outcomePromise = runWindowsUpdatePreflight('normal-update', deps)
    await workerStartedPromise

    assert.equal(calls.includes('terminate-start:101'), false)
    assert.equal(maximumConcurrentTerminations, 1)
    releaseWorker()

    const outcome = await outcomePromise
    assert.equal(outcome.kind, 'clear')
    assert.equal(maximumConcurrentTerminations, 1)
    assert.ok(calls.indexOf('terminate-end:102') < calls.indexOf('terminate-start:101'))
  })

  it('clears its lease when a bridge respawns between the two clear scans', async () => {
    const { calls, deps } = makeDeps([blockedByBridges(), clear(), blockedByBridges()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 900,
      respawnIntervalMs: 1_100
    })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.ok(calls.includes(`clear-lease:${lease.leaseId}`))
    assert.ok(!calls.some(call => call.startsWith('terminate:')))
  })

  it('gets newcomer consent before the lease and catches a bridge spawned after a clear observation', async () => {
    const { calls, deps } = makeDeps([clear(), blockedByBridges(), clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 900,
      respawnIntervalMs: 1_100
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(outcome.lease?.leaseId, lease.leaseId)
    assert.deepEqual(calls, [
      'release',
      'scan',
      'consent',
      'lease',
      'wait:900',
      'scan',
      'terminate:101:123.5',
      'wait:750',
      'scan',
      'wait:1100',
      'scan'
    ])
  })
})

describe('buildMcpBridgeConsentRequest', () => {
  it('names every proven owner without calling an MCP server a Desktop backend', () => {
    const request = buildMcpBridgeConsentRequest([
      bridge({ owner: 'codex' }),
      bridge({ pid: 102, owner: 'claude' }),
      bridge({ pid: 103, owner: 'unknown' })
    ])

    assert.equal(request.ownerLabel, 'Codex and Claude')
    assert.match(request.detail, /Unknown owner/)
    assert.match(request.message, /MCP tool bridges/)
    assert.match(request.detail, /interrupt active tool calls/)
    assert.doesNotMatch(`${request.message}\n${request.detail}`, /Desktop backend/i)
  })

  it('explains the bounded prevention lease when no bridge is currently running', () => {
    const request = buildMcpBridgeConsentRequest([])

    assert.match(request.message, /new Codex and Claude MCP bridge launches/)
    assert.match(request.detail, /No exact Hermes MCP tool bridges are running now/)
    assert.match(request.detail, /bounded update transaction/)
  })
})
