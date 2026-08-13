import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import {
  runWindowsForceDrainPreflight,
  type ForceDrainDeps
} from './update-preflight'
import type {
  DesktopPluginServiceProcess,
  McpBridgeProcess,
  ScanOutcome,
  VenvBlockerScanResult
} from './venv-blocker-scan'

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

const desktopPluginService = (
  overrides: Partial<DesktopPluginServiceProcess> = {}
): DesktopPluginServiceProcess => ({
  pid: 301,
  name: 'python.exe',
  cmdline: 'python.exe C:\\Hermes\\desktop-plugins\\tracker\\service.py',
  createdAt: 333.5,
  owner: 'desktop',
  role: 'desktop_plugin_wrapper',
  actionable: true,
  actionability: 'exact_desktop_plugin_service',
  action: 'terminate_desktop_plugin_service',
  ...overrides
})

const result = (overrides: Partial<VenvBlockerScanResult> = {}): VenvBlockerScanResult => ({
  blocked: false,
  processes: [],
  mcpBridges: [],
  desktopPluginServices: [],
  pausableGateways: 0,
  ...overrides
})

const clear = (): ScanOutcome => ({ kind: 'clear', result: result() })

function makeDeps(scans: ScanOutcome[], overrides: Partial<ForceDrainDeps> = {}) {
  const calls: string[] = []
  const queue = [...scans]
  const deps: ForceDrainDeps = {
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
    terminateDesktopPluginService: async service => {
      calls.push(`service:${service.pid}:${service.createdAt}`)
      return true
    },
    terminateVenvHolder: async holder => {
      calls.push(`holder:${holder.pid}:${holder.createdAt}`)
      return true
    },
    wait: async delay => {
      calls.push(`wait:${delay}`)
    },
    ...overrides
  }
  return { calls, deps }
}

describe('runWindowsForceDrainPreflight', () => {
  it('fails before scanning when Desktop-owned backends do not unlock', async () => {
    const { calls, deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => ({ unlocked: false })
    })

    const outcome = await runWindowsForceDrainPreflight(deps)

    assert.deepEqual(outcome, {
      kind: 'blocked',
      reason: 'unlock-failed',
      message:
        'Update aborted: Hermes could not release its tracked local processes. ' +
        'Close other Hermes windows and terminals, then retry.'
    })
    assert.deepEqual(calls, [])
  })

  it('hands off immediately when the target installation is already clear', async () => {
    const { calls, deps } = makeDeps([clear()])

    assert.deepEqual(await runWindowsForceDrainPreflight(deps), { kind: 'clear' })
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('refuses a holder that the scanner cannot prove current', async () => {
    const blocked: ScanOutcome = {
      kind: 'blocked',
      result: result({
        blocked: true,
        processes: [{ pid: 202, name: 'python.exe', cmdline: 'python.exe user-script.py' }]
      })
    }
    const { calls, deps } = makeDeps([blocked])

    const outcome = await runWindowsForceDrainPreflight(deps)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'holders')
    assert.deepEqual(calls, ['release', 'scan'])
  })

  it('drains every proven target holder, with workers before wrappers', async () => {
    const generic = { pid: 401, name: 'python.exe', cmdline: 'python.exe worker.py', createdAt: 444.5 }
    const blocked: ScanOutcome = {
      kind: 'blocked',
      result: result({
        blocked: true,
        processes: [generic],
        mcpBridges: [
          bridge({ pid: 102, createdAt: 125.5, role: 'mcp_bridge_worker', wrapperPid: 101 }),
          bridge({ pid: 101, createdAt: 123.5, role: 'mcp_bridge_wrapper' })
        ],
        desktopPluginServices: [
          desktopPluginService({ pid: 302, createdAt: 334.5, role: 'desktop_plugin_worker', wrapperPid: 301 }),
          desktopPluginService({ pid: 301, createdAt: 333.5, role: 'desktop_plugin_wrapper' })
        ]
      })
    }
    const { calls, deps } = makeDeps([blocked, clear(), clear()])

    assert.deepEqual(
      await runWindowsForceDrainPreflight(deps, { terminationSettleMs: 2, respawnIntervalMs: 3 }),
      { kind: 'clear' }
    )
    assert.deepEqual(calls, [
      'release',
      'scan',
      'holder:102:125.5',
      'holder:101:123.5',
      'service:302:334.5',
      'service:301:333.5',
      'holder:401:444.5',
      'wait:2',
      'scan',
      'wait:3',
      'scan'
    ])
  })

  it('refuses when a proven target holder survives the force drain', async () => {
    const blocked: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [{ pid: 401, name: 'python.exe', cmdline: 'python.exe worker.py', createdAt: 444.5 }] })
    }
    const { calls, deps } = makeDeps([blocked, blocked])

    const outcome = await runWindowsForceDrainPreflight(deps, { terminationSettleMs: 0 })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'drain-incomplete')
    assert.deepEqual(calls, ['release', 'scan', 'holder:401:444.5', 'wait:0', 'scan'])
  })

  it('refuses a process-table change that cannot be proved after draining', async () => {
    const blocked: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [{ pid: 401, name: 'python.exe', cmdline: 'python.exe worker.py', createdAt: 444.5 }] })
    }
    const unknown: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [{ pid: 402, name: 'python.exe', cmdline: 'python.exe other.py' }] })
    }
    const { deps } = makeDeps([blocked, clear(), unknown])

    const outcome = await runWindowsForceDrainPreflight(deps, { terminationSettleMs: 0, respawnIntervalMs: 0 })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'drain-incomplete')
  })
})
