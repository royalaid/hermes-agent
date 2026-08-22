import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import type { McpBridgeQuiesceLease } from './mcp-bridge-quiesce'
import {
  authorizeUpdateMutation,
  createNormalUpdateBlockerDeadline,
  runAuthorizedUpdateMutation,
  runWindowsUpdatePreflight,
  type UpdatePreflightDeps,
  type UpdatePreflightPurpose
} from './update-preflight'
import type {
  DesktopPluginServiceProcess,
  McpBridgeProcess,
  ScanOutcome,
  VenvBlockerScanResult
} from './venv-blocker-scan'

const PURPOSES: UpdatePreflightPurpose[] = ['normal-update', 'bootstrap-recovery']

describe('normal update request deadline', () => {
  it('creates one absolute five-second deadline at the request boundary', () => {
    const deadline = createNormalUpdateBlockerDeadline(() => 12_345)

    assert.deepEqual(deadline, { deadlineAt: 17_345 })
    assert.ok(Object.isFrozen(deadline))
  })

  it('passes the same absolute deadline through tracked preparation and force release', async () => {
    const seen: Array<[string, number]> = []
    let currentTime = 10_000
    const deadline = createNormalUpdateBlockerDeadline(() => currentTime)

    const { deps } = makeDeps([], {
      now: () => currentTime,
      releaseTrackedBackendTrees: async current => {
        seen.push(['release', current.deadlineAt])
        currentTime += 1_750

        return { unlocked: false }
      },
      forceReleaseInstallHolders: async current => {
        seen.push(['force-release', current.deadlineAt])

        return { kind: 'timeout', holders: [], message: 'deadline elapsed' }
      }
    })

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {}, deadline)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'unlock-failed')
    assert.deepEqual(seen, [
      ['release', 15_000],
      ['force-release', 15_000]
    ])
  })

  it('does not start a fresh force-release window after preparation exhausts the request deadline', async () => {
    let currentTime = 20_000
    let forceReleaseCalled = false
    const deadline = createNormalUpdateBlockerDeadline(() => currentTime)

    const { deps } = makeDeps([], {
      now: () => currentTime,
      releaseTrackedBackendTrees: async () => {
        currentTime = deadline.deadlineAt

        return { unlocked: false }
      },
      forceReleaseInstallHolders: async () => {
        forceReleaseCalled = true

        return { kind: 'needs-elevation', holders: [], message: 'should not run' }
      }
    })

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {}, deadline)

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'unlock-failed')
    assert.equal(forceReleaseCalled, false)
    assert.equal('elevationHolders' in outcome, false)
  })
})

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

const desktopPluginService = (overrides: Partial<DesktopPluginServiceProcess> = {}): DesktopPluginServiceProcess => ({
  pid: 301,
  name: 'python.exe',
  cmdline: 'python.exe C:\\Users\\u\\AppData\\Local\\hermes\\desktop-plugins\\tracker\\service.py',
  createdAt: 333.5,
  owner: 'desktop',
  role: 'desktop_plugin_wrapper',
  actionable: true,
  actionability: 'exact_desktop_plugin_service',
  action: 'terminate_desktop_plugin_service',
  ...overrides
})

function pairedBridgeGroups(count: number): McpBridgeProcess[] {
  return Array.from({ length: count }, (_, index) => {
    const wrapperPid = 1_000 + index

    return [
      bridge({
        pid: 2_000 + index,
        createdAt: 3_000 + index,
        role: 'mcp_bridge_worker',
        wrapperPid
      }),
      bridge({ pid: wrapperPid, createdAt: 4_000 + index })
    ]
  }).flat()
}

const result = (overrides: Partial<VenvBlockerScanResult> = {}): VenvBlockerScanResult => ({
  blocked: false,
  processes: [],
  mcpBridges: [],
  desktopPluginServices: [],
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
  let currentTime = 0

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
    acquireMcpBridgeLease: () => {
      calls.push('lease')

      return lease
    },
    clearMcpBridgeLease: current => {
      calls.push(`clear-lease:${current.leaseId}`)
    },
    now: () => currentTime,
    terminateDesktopPluginService: async current => {
      calls.push(`terminate-desktop-plugin:${current.pid}:${current.createdAt}`)

      return true
    },
    terminateVenvHolder: async current => {
      const isMcpBridge =
        'role' in current && (current.role === 'mcp_bridge_worker' || current.role === 'mcp_bridge_wrapper')

      calls.push(`${isMcpBridge ? 'terminate' : 'terminate-holder'}:${current.pid}:${current.createdAt}`)

      return true
    },
    wait: async delay => {
      calls.push(`wait:${delay}`)
      currentTime += Math.max(0, delay)
    },
    ...overrides
  }

  return { calls, deps }
}

describe.each(PURPOSES)('runWindowsUpdatePreflight (%s)', purpose => {
  it('fails closed before scanning when tracked backend trees do not unlock and no force-release is wired', async () => {
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

  it('force-releases exact holders when tracked unlock fails, then continues the scan path', async () => {
    const { calls, deps } = makeDeps([clear(), clear(), clear()], {
      releaseTrackedBackendTrees: async () => {
        calls.push('release')

        return { unlocked: false }
      },
      forceReleaseInstallHolders: async () => {
        calls.push('force-release')

        return { kind: 'clear' }
      }
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.ok(calls.includes('force-release'))
    assert.ok(calls.includes('scan'))
  })

  it('surfaces needs-elevation from the force-release quick path', async () => {
    const { deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => ({ unlocked: false }),
      forceReleaseInstallHolders: async () => ({
        kind: 'needs-elevation',
        holders: [
          {
            pid: 901,
            createdAt: 1,
            creationFileTime: '133000000000000000',
            name: 'python.exe',
            cmdline: 'python.exe',
            source: 'scanner',
            resource: String.raw`C:\Hermes\venv\Scripts\hermes.exe`,
            resources: [String.raw`C:\Hermes\venv\Scripts\hermes.exe`]
          }
        ],
        message: 'needs Administrator'
      })
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)
    assert.equal(outcome.kind, 'blocked')

    if (outcome.kind === 'blocked') {
      assert.equal(outcome.reason, 'needs-elevation')
      assert.equal(outcome.elevationHolders?.[0]?.pid, 901)
    }
  })

  it('does not expose Administrator action for an unauthenticated permission-shaped result', async () => {
    const { deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => ({ unlocked: false }),
      forceReleaseInstallHolders: async () => ({
        kind: 'needs-elevation',
        holders: [
          {
            pid: 901,
            createdAt: Number.NaN,
            name: 'python.exe',
            cmdline: 'python.exe',
            source: 'scanner'
          }
        ],
        message: 'generic access failure'
      })
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')

    if (outcome.kind === 'blocked') {
      assert.equal(outcome.reason, 'unlock-failed')
      assert.equal(outcome.elevationHolders, undefined)
    }
  })

  it('does not preserve a permission claim without exact create-time and per-resource evidence', async () => {
    const { deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => ({ unlocked: false }),
      forceReleaseInstallHolders: async () => ({
        kind: 'needs-elevation',
        holders: [
          {
            pid: 901,
            createdAt: 1,
            name: 'python.exe',
            cmdline: 'python.exe',
            source: 'restart-manager',
            resource: String.raw`C:\Hermes\venv\Scripts\hermes.exe`
          }
        ],
        message: 'permission required'
      })
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')

    if (outcome.kind === 'blocked') {
      assert.equal(outcome.reason, 'unlock-failed')
      assert.equal(outcome.elevationHolders, undefined)
    }
  })

  it('never advances toward updater mutation while a verified holder survives force-release', async () => {
    const survivor = {
      pid: 902,
      createdAt: 2,
      name: 'python.exe',
      cmdline: 'python.exe -m hermes_cli',
      source: 'scanner' as const,
      resource: 'C:\\Hermes\\venv\\Scripts\\hermes.exe'
    }

    const { calls, deps } = makeDeps([], {
      releaseTrackedBackendTrees: async () => {
        calls.push('release')

        return { unlocked: false }
      },
      forceReleaseInstallHolders: async () => {
        calls.push('force-release')

        return {
          kind: 'timeout',
          holders: [survivor],
          message: 'verified holder survived the deadline'
        }
      }
    })

    const outcome = await runWindowsUpdatePreflight(purpose, deps)

    assert.equal(outcome.kind, 'blocked')
    assert.deepEqual(calls, ['release', 'force-release'])
    assert.ok(!calls.includes('scan'), 'scanner continuation would permit the updater handoff to progress')
    assert.ok(!calls.includes('lease'), 'no mutation-prevention lease starts while an authenticated holder survives')
    assert.ok(!calls.some(call => call.startsWith('terminate-holder:')))
    const permit = authorizeUpdateMutation(outcome)
    assert.equal(permit, null)
    let updaterLaunched = false
    let desktopShutdown = false
    assert.throws(
      () =>
        runAuthorizedUpdateMutation(permit as never, () => {
          updaterLaunched = true
          desktopShutdown = true
        }),
      /clear-preflight permit/
    )
    assert.equal(updaterLaunched, false)
    assert.equal(desktopShutdown, false)
  })

  it('mints a permit only after the clear production preflight and runs both handoff mutations', async () => {
    const { deps } = makeDeps([clear(), clear(), clear()])

    const outcome = await runWindowsUpdatePreflight(purpose, deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(outcome.lease, lease, 'clear preflight preserves the exact capability-bearing lease identity')
    const permit = authorizeUpdateMutation(outcome)
    assert.ok(permit)
    assert.equal(authorizeUpdateMutation(outcome), null, 'a successful preflight permit is consumed exactly once')
    const mutations: string[] = []
    runAuthorizedUpdateMutation(permit, () => mutations.push('updater-launch'))
    runAuthorizedUpdateMutation(permit, () => mutations.push('desktop-shutdown'))
    assert.deepEqual(mutations, ['updater-launch', 'desktop-shutdown'])
  })

  it('rejects a fabricated structural clear outcome and never runs its mutation', () => {
    const fabricated = { kind: 'clear' as const, lease }
    const permit = authorizeUpdateMutation(fabricated)
    assert.equal(permit, null)
    let mutated = false
    assert.throws(
      () =>
        runAuthorizedUpdateMutation(permit as never, () => {
          mutated = true
        }),
      /clear-preflight permit/
    )
    assert.equal(mutated, false)
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

  it('force-stops every current target-install holder after Update is chosen', async () => {
    const holder = {
      pid: 202,
      name: 'python.exe',
      cmdline: 'python.exe user-script.py',
      createdAt: 456.5
    }

    const blocked: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [holder] })
    }

    const { calls, deps } = makeDeps([blocked, blocked, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight(purpose, deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'terminate-holder:202:456.5',
      'wait:0',
      'scan',
      'wait:0',
      'scan'
    ])
  })

  it('force-stops a scanned MCP bridge even when it has no agent ownership attribution', async () => {
    const unproven = blockedByBridges([bridge({ owner: 'unknown' })])
    const { calls, deps } = makeDeps([unproven, unproven, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight(purpose, deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.ok(calls.includes('terminate:101:123.5'))
  })
})

describe('MCP bridge drain', () => {
  it('routes a normal-update newcomer through the native force-release boundary, never legacy termination', async () => {
    let currentTime = 1_000
    const deadline = createNormalUpdateBlockerDeadline(() => currentTime)

    const { calls, deps } = makeDeps([clear(), blockedByBridges(), clear(), clear()], {
      now: () => currentTime,
      forceReleaseInstallHolders: async current => {
        calls.push(`native-force:${current?.deadlineAt}`)

        return { kind: 'clear' }
      },
      wait: async delay => {
        calls.push(`wait:${delay}`)
        currentTime += Math.max(0, delay)
      }
    })

    const outcome = await runWindowsUpdatePreflight(
      'normal-update',
      deps,
      { cooperativeExitMs: 0, respawnIntervalMs: 0, terminationSettleMs: 0 },
      deadline
    )

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'native-force:6000',
      'scan',
      'wait:0',
      'scan'
    ])
    assert.ok(!calls.some(call => call.startsWith('terminate:')))
  })

  it('gets consent before activating the lease, then lets cooperative exit win', async () => {
    const { calls, deps } = makeDeps([blockedByBridges(), clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 900,
      respawnIntervalMs: 1_100,
      terminationSettleMs: 700
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(outcome.lease?.leaseId, lease.leaseId)
    assert.deepEqual(calls, ['release', 'scan', 'lease', 'wait:900', 'scan', 'wait:1100', 'scan'])
  })

  it('waits for a generic holder first seen on the stability scan, then proves a fresh stable interval', async () => {
    const transient = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), clear(), genericOnly, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 5,
      genericHolderTimeoutMs: 10,
      respawnIntervalMs: 7
    })

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'wait:7',
      'scan',
      'wait:5',
      'scan',
      'wait:7',
      'scan'
    ])
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

  it('drains an exact Desktop plugin worker before its wrapper after explicit consent', async () => {
    const wrapper = desktopPluginService()

    const worker = desktopPluginService({
      pid: 302,
      createdAt: 334.5,
      role: 'desktop_plugin_worker',
      wrapperPid: wrapper.pid
    })

    const stillRunning: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, desktopPluginServices: [wrapper, worker] })
    }

    const { calls, deps } = makeDeps([stillRunning, stillRunning, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'terminate-desktop-plugin:302:334.5',
      'terminate-desktop-plugin:301:333.5',
      'wait:0',
      'scan',
      'wait:0',
      'scan'
    ])
  })

  it('waits for a late generic holder when every bridge exits cooperatively', async () => {
    const transient = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), genericOnly, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 5,
      genericHolderTimeoutMs: 10,
      respawnIntervalMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(
      calls.some(call => call.startsWith('terminate:')),
      false
    )
    assert.deepEqual(calls, ['release', 'scan', 'lease', 'wait:0', 'scan', 'wait:5', 'scan', 'wait:0', 'scan'])
  })

  it('drains an exact bridge from a mixed late scan while a transient generic holder exits naturally', async () => {
    const transient = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const mixed: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient], mcpBridges: [bridge()] })
    }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), mixed, genericOnly, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 900,
      genericHolderPollMs: 250,
      genericHolderTimeoutMs: 2_000,
      respawnIntervalMs: 1_100,
      terminationSettleMs: 700
    })

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:900',
      'scan',
      'terminate:101:123.5',
      'wait:700',
      'scan',
      'wait:250',
      'scan',
      'wait:1100',
      'scan'
    ])
  })

  it('refuses a generic holder that persists through the bounded post-termination window', async () => {
    const persistent = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const mixed: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [persistent], mcpBridges: [bridge()] })
    }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [persistent] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), mixed, genericOnly, genericOnly, genericOnly])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 5,
      genericHolderTimeoutMs: 10,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.deepEqual(outcome.result?.processes, [persistent])
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'terminate:101:123.5',
      'wait:0',
      'scan',
      'wait:5',
      'scan',
      'wait:5',
      'scan',
      `clear-lease:${lease.leaseId}`
    ])
  })

  it.each([
    {
      phase: 'cooperative exit',
      scans: [
        blockedByBridges(),
        {
          kind: 'blocked',
          result: result({
            blocked: true,
            processes: [{ pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }]
          })
        },
        { kind: 'probe-failure', error: 'poll probe failed' }
      ],
      expectedCalls: ['release', 'scan', 'lease', 'wait:0', 'scan', 'wait:5', 'scan']
    },
    {
      phase: 'fallback termination',
      scans: [
        blockedByBridges(),
        blockedByBridges(),
        {
          kind: 'blocked',
          result: result({
            blocked: true,
            processes: [{ pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }]
          })
        },
        { kind: 'probe-failure', error: 'poll probe failed' }
      ],
      expectedCalls: [
        'release',
        'scan',
        'lease',
        'wait:0',
        'scan',
        'terminate:101:123.5',
        'wait:0',
        'scan',
        'wait:5',
        'scan'
      ]
    }
  ] satisfies Array<{ phase: string; scans: ScanOutcome[]; expectedCalls: string[] }>)(
    'fails closed on a probe failure while polling generic holders after $phase',
    async ({ scans, expectedCalls }) => {
      const { calls, deps } = makeDeps(scans)

      const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
        cooperativeExitMs: 0,
        genericHolderPollMs: 5,
        genericHolderTimeoutMs: 10,
        terminationSettleMs: 0
      })

      assert.equal(outcome.kind, 'probe-failure')
      assert.equal(outcome.error, 'poll probe failed')
      assert.deepEqual(calls, [...expectedCalls, `clear-lease:${lease.leaseId}`])
    }
  )

  it('force-stops an unproven current MCP record after Update is chosen', async () => {
    const transient = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const unproven = bridge({
      pid: 103,
      owner: 'unknown',
      actionable: false,
      actionability: 'hard_block',
      action: 'refuse'
    })

    const mixed: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient], mcpBridges: [unproven] })
    }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [transient] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), mixed, genericOnly, clear(), clear()])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 0,
      genericHolderTimeoutMs: 1,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.ok(calls.includes('terminate:103:123.5'))
  })

  it('fails closed without terminating when the current exact bridge set exceeds the fallback cap', async () => {
    const bridges = Array.from({ length: 33 }, (_, index) => bridge({ pid: 1_000 + index, createdAt: 2_000 + index }))
    const { calls, deps } = makeDeps([blockedByBridges(), blockedByBridges(bridges)])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, { cooperativeExitMs: 0 })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.equal(
      calls.some(call => call.startsWith('terminate:')),
      false
    )
    assert.ok(calls.includes(`clear-lease:${lease.leaseId}`))
  })

  it('allows exactly 64 fallback records across 32 logical bridge groups', async () => {
    const bridges = pairedBridgeGroups(32)
    const terminated: McpBridgeProcess[] = []

    const { deps } = makeDeps([blockedByBridges(), blockedByBridges(bridges), clear(), clear()], {
      terminateVenvHolder: async current => {
        terminated.push(current as McpBridgeProcess)

        return true
      }
    })

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 0,
      terminationSettleMs: 0
    })

    assert.equal(outcome.kind, 'clear')
    assert.equal(terminated.length, 64)
    assert.equal(
      terminated.slice(0, 32).every(current => current.role === 'mcp_bridge_worker'),
      true
    )
    assert.equal(
      terminated.slice(32).every(current => current.role === 'mcp_bridge_wrapper'),
      true
    )
    assert.equal(new Set(terminated.map(current => current.wrapperPid ?? current.pid)).size, 32)
  })

  it('refuses 65 fallback records even when they fit within 32 logical bridge groups', async () => {
    const bridges = [
      ...pairedBridgeGroups(32),
      bridge({
        pid: 5_000,
        createdAt: 6_000,
        role: 'mcp_bridge_worker',
        wrapperPid: 1_000
      })
    ]

    const { calls, deps } = makeDeps([blockedByBridges(), blockedByBridges(bridges)])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, { cooperativeExitMs: 0 })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.equal(
      calls.some(call => call.startsWith('terminate:')),
      false
    )
    assert.ok(calls.includes(`clear-lease:${lease.leaseId}`))
  })

  it('refuses 33 paired wrapper and worker groups before terminating any record', async () => {
    const bridges = pairedBridgeGroups(33)
    const { calls, deps } = makeDeps([blockedByBridges(), blockedByBridges(bridges)])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, { cooperativeExitMs: 0 })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.equal(
      calls.some(call => call.startsWith('terminate:')),
      false
    )
    assert.ok(calls.includes(`clear-lease:${lease.leaseId}`))
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
      terminateVenvHolder: async current => {
        const bridge = current as McpBridgeProcess
        activeTerminations += 1
        maximumConcurrentTerminations = Math.max(maximumConcurrentTerminations, activeTerminations)
        calls.push(`terminate-start:${bridge.pid}`)

        if (bridge.role === 'mcp_bridge_worker') {
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

  it.each([
    { blocker: 'an exact bridge', outcome: blockedByBridges() },
    {
      blocker: 'mixed generic and exact holders',
      outcome: {
        kind: 'blocked',
        result: result({
          blocked: true,
          processes: [{ pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }],
          mcpBridges: [bridge()]
        })
      }
    },
    {
      blocker: 'an unproven MCP record',
      outcome: blockedByBridges([
        bridge({
          owner: 'unknown',
          actionable: false,
          actionability: 'hard_block',
          action: 'refuse'
        })
      ])
    }
  ] satisfies Array<{ blocker: string; outcome: ScanOutcome }>)(
    'clears its lease when $blocker appears on the stability scan',
    async ({ outcome: blocker }) => {
      const { calls, deps } = makeDeps([blockedByBridges(), clear(), blocker])

      const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
        cooperativeExitMs: 900,
        respawnIntervalMs: 1_100
      })

      assert.equal(outcome.kind, 'blocked')
      assert.equal(outcome.reason, 'quiesce-incomplete')
      assert.ok(calls.includes(`clear-lease:${lease.leaseId}`))
      assert.ok(!calls.some(call => call.startsWith('terminate:')))
    }
  )

  it('returns a probe failure from the stability scan and clears its lease', async () => {
    const { calls, deps } = makeDeps([
      blockedByBridges(),
      clear(),
      { kind: 'probe-failure', error: 'stability probe failed' }
    ])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      respawnIntervalMs: 7
    })

    assert.equal(outcome.kind, 'probe-failure')
    assert.equal(outcome.error, 'stability probe failed')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'wait:7',
      'scan',
      `clear-lease:${lease.leaseId}`
    ])
  })

  it('returns a probe failure while polling a generic holder from the stability scan', async () => {
    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({
        blocked: true,
        processes: [{ pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }]
      })
    }

    const { calls, deps } = makeDeps([
      blockedByBridges(),
      clear(),
      genericOnly,
      { kind: 'probe-failure', error: 'stability poll failed' }
    ])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 5,
      genericHolderTimeoutMs: 10,
      respawnIntervalMs: 7
    })

    assert.equal(outcome.kind, 'probe-failure')
    assert.equal(outcome.error, 'stability poll failed')
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'wait:7',
      'scan',
      'wait:5',
      'scan',
      `clear-lease:${lease.leaseId}`
    ])
  })

  it('refuses a generic holder that reaches the final-scan polling deadline', async () => {
    const persistent = { pid: 202, name: 'python.exe', cmdline: 'hermes gateway status --deep' }

    const genericOnly: ScanOutcome = {
      kind: 'blocked',
      result: result({ blocked: true, processes: [persistent] })
    }

    const { calls, deps } = makeDeps([blockedByBridges(), clear(), genericOnly, genericOnly, genericOnly])

    const outcome = await runWindowsUpdatePreflight('normal-update', deps, {
      cooperativeExitMs: 0,
      genericHolderPollMs: 5,
      genericHolderTimeoutMs: 10,
      respawnIntervalMs: 7
    })

    assert.equal(outcome.kind, 'blocked')
    assert.equal(outcome.reason, 'quiesce-incomplete')
    assert.deepEqual(outcome.result?.processes, [persistent])
    assert.deepEqual(calls, [
      'release',
      'scan',
      'lease',
      'wait:0',
      'scan',
      'wait:7',
      'scan',
      'wait:5',
      'scan',
      'wait:5',
      'scan',
      `clear-lease:${lease.leaseId}`
    ])
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

describe('production update mutation permit wiring', () => {
  it('threads one request-bound deadline through every ordinary preflight attempt', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    assert.match(
      main,
      /async function applyUpdates\([^)]*\)\s*{\s*const blockerDeadline = createNormalUpdateBlockerDeadline\(\)/
    )
    assert.match(main, /applyUpdatesTransaction\(opts, blockerDeadline\)/)
    assert.equal(
      (main.match(/runWindowsHandoffPreflight\(updateRoot, 'normal-update', blockerDeadline\)/g) ?? []).length,
      2,
      'the initial attempt and same-request safe-blocker continuation share one absolute deadline'
    )
  })

  it('does not broad-tree-kill tracked backends before native holder authentication', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    const body = main.slice(
      main.indexOf('async function releaseBackendLockForUpdate'),
      main.indexOf('function windowsPreflightErrorCode')
    )

    assert.doesNotMatch(body, /stopBackendTreesForUpdate|forceKillProcessTree|releaseBackendLock\(/)
    assert.match(body, /isAnyInstallResourceLocked/)

    const transactionBody = main.slice(
      main.indexOf('async function applyUpdatesTransaction'),
      main.indexOf('async function handOffWindowsBootstrapRecovery')
    )

    assert.doesNotMatch(transactionBody, /forceKillProcessTree|releaseBackendLock\(|stopBackendTreesForUpdate/)
  })

  it('propagates abort and the absolute deadline into both discovery helpers', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    const body = main.slice(
      main.indexOf('async function forceReleaseInstallHoldersForUpdate'),
      main.indexOf('function runWindowsHandoffPreflight')
    )

    assert.match(body, /deadlineAt:\s*deadline\.deadlineAt/)
    assert.match(body, /listScannerHolders:\s*async \(budgetMs, signal, deadlineAt\)/)
    assert.match(body, /scanVenvBlockersWithinDeadline\([\s\S]{0,300}signal/)
    assert.match(body, /listRestartManagerHolders:\s*async \(budgetMs, signal, deadlineAt\)/)
    assert.match(body, /listRestartManagerHoldersForResources\([\s\S]{0,300}\{ timeoutMs, signal \}/)
  })

  it('does not launch the legacy elevated helper from an unbound renderer retry flag', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    const body = main.slice(
      main.indexOf('async function applyUpdatesTransaction'),
      main.indexOf('const mutationPermit')
    )

    assert.doesNotMatch(body, /runElevatedForceReleaseForUpdate/)
    assert.match(body, /if \(IS_WINDOWS && opts\.forceUpdateElevated\)[\s\S]{0,700}error: 'elevation-claim-required'/)
    assert.match(main, /case 'needs-elevation':[\s\S]{0,500}return 'venv-permission-required'/)
    assert.doesNotMatch(body, /elevationHolders:\s*preflight\.elevationHolders/)
  })

  it('authenticates and observes the actual hidden writer generation before Desktop quit', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    const body = main.slice(
      main.indexOf('const launch = runAuthorizedUpdateMutation'),
      main.indexOf('return { ok: true, handedOff: true')
    )

    assert.match(body, /const handoffOwner = await waitForWindowsHandoffOwnerIdentity\(child\)/)
    assert.match(body, /requiredOwnerPid:\s*handoffOwner\.pid/)
    assert.match(
      body,
      /observeUpdaterGeneration\(\s*handoffOwner\.pid,\s*handoffOwner\.creationFileTime,\s*[\s\S]{0,500}if \(!handoffOutcome\.ok \|\| !writerGenerationActive\)/
    )
    assert.ok(
      body.indexOf('const handoffOwner = await waitForWindowsHandoffOwnerIdentity(child)') <
        body.indexOf('isQuittingForHandoff = true'),
      'actual writer identity must be proven before the Desktop quit boundary'
    )
  })

  it('gates normal and bootstrap-recovery updater launch and Desktop shutdown sites', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const main = fs.readFileSync(path.resolve(__dirname, 'main.ts'), 'utf8')

    assert.match(main, /runAuthorizedUpdateMutation\(mutationPermit, \(\) =>\s*launchWindowsUpdateTransport\(/)
    assert.match(main, /runAuthorizedUpdateMutation\(mutationPermit, \(\) =>\s*spawnUpdaterProcess\(/)
    assert.equal(
      (main.match(/runAuthorizedUpdateMutation\(mutationPermit, \(\) =>\s*setTimeout\(/g) ?? []).length,
      2,
      'both normal-update and bootstrap-recovery shutdowns require the clear-preflight permit'
    )
    assert.doesNotMatch(main, /const launch = launchWindowsUpdateTransport\(/)
  })
})
