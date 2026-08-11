'use strict'

/**
 * Tests for apps/desktop/electron/venv-blocker-scan.ts
 *
 * Run with: bunx vitest run electron/venv-blocker-scan.test.ts
 * (from apps/desktop; wired into npm test:desktop:platforms)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, it } from 'vitest'

import {
  formatBlockerMessage,
  formatProbeFailedMessage,
  type McpBridgeProcess,
  parseVenvBlockerScanOutput,
  resolveVenvPython,
  scanVenvBlockers,
  terminateMcpBridge
} from './venv-blocker-scan'

const volumeRoot = path.parse(process.cwd()).root

function genericProcess(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pid: 11,
    name: 'python.exe',
    cmdline: 'python.exe -m hermes_cli.main serve',
    created_at: 101.25,
    owner: 'desktop',
    role: 'desktop_backend',
    actionable: false,
    actionability: 'hard_block',
    action: 'refuse',
    ...overrides
  }
}

function mcpBridge(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pid: 22,
    name: 'python.exe',
    cmdline: 'python.exe -m agent.transports.hermes_tools_mcp_server',
    created_at: 202.5,
    owner: 'codex',
    role: 'mcp_bridge_worker',
    actionable: true,
    actionability: 'exact_mcp_bridge',
    action: 'terminate_exact_mcp',
    ...overrides
  }
}

function pausableGateway(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pid: 33,
    name: 'python.exe',
    cmdline: 'python.exe -m hermes_cli.main gateway run',
    created_at: 303.75,
    owner: 'gateway',
    role: 'gateway_run',
    actionable: false,
    actionability: 'downstream_drainable',
    action: 'pause_downstream',
    ...overrides
  }
}

function scanEnvelope(
  root: string,
  venv: string,
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    schema_version: 1,
    mode: 'scan',
    ok: true,
    ready: true,
    blocked: false,
    reason: null,
    root,
    venv,
    processes: [],
    mcp_bridges: [],
    pausable_gateways: 0,
    pausable_gateway_processes: [],
    error: null,
    ...overrides
  }
}

function blockedScanEnvelope(
  root: string,
  venv: string,
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return scanEnvelope(root, venv, {
    ready: false,
    blocked: true,
    reason: 'processes_running',
    ...overrides
  })
}

function execReturn(value: unknown): any {
  const stdout = typeof value === 'string' ? value : JSON.stringify(value)

  return (async () => ({ stdout, stderr: '' })) as any
}

function execThrow(status: number, stderr: string): any {
  return (async () => {
    const error: any = new Error()
    error.status = status
    error.stderr = Buffer.from(stderr)
    throw error
  }) as any
}

// ---------------------------------------------------------------------------
// resolveVenvPython
// ---------------------------------------------------------------------------

describe('resolveVenvPython', () => {
  it('returns the platform venv interpreter when it exists', () => {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-vt-'))

    try {
      const scriptsDir = process.platform === 'win32' ? 'Scripts' : 'bin'
      const pythonName = process.platform === 'win32' ? 'python.exe' : 'python3'
      const dir = path.join(sandbox, 'venv', scriptsDir)
      fs.mkdirSync(dir, { recursive: true })
      const python = path.join(dir, pythonName)
      fs.writeFileSync(python, '', { mode: 0o755 })
      assert.equal(resolveVenvPython(sandbox), python)
    } finally {
      fs.rmSync(sandbox, { recursive: true, force: true })
    }
  })

  it('returns null when the target venv interpreter does not exist', () => {
    assert.equal(resolveVenvPython(path.join(volumeRoot, 'nonexistent')), null)
  })
})

// ---------------------------------------------------------------------------
// formatBlockerMessage / formatProbeFailedMessage
// ---------------------------------------------------------------------------

describe('formatBlockerMessage', () => {
  it('includes blocker identity, the remote-client warning, and retry guidance', () => {
    const msg = formatBlockerMessage({
      blocked: true,
      processes: [{ pid: 101, name: 'python.exe', cmdline: 'serve --host 10.0.0.1' }],
      mcpBridges: [],
      pausableGateways: 0
    })

    assert.ok(msg.includes('PID 101'))
    assert.ok(msg.includes('python.exe'))
    assert.ok(msg.includes('serve'))
    assert.ok(msg.includes('remote Hermes service'))
    assert.ok(msg.includes('retry'))
    assert.ok(!msg.includes('force-venv'))
  })
})

describe('formatProbeFailedMessage', () => {
  it('suggests retry and the terminal updater', () => {
    const msg = formatProbeFailedMessage()
    assert.ok(msg.includes('hermes update'))
    assert.ok(msg.includes('retry'))
  })
})

// ---------------------------------------------------------------------------
// parseVenvBlockerScanOutput — pure strict-contract parser
// ---------------------------------------------------------------------------

describe('parseVenvBlockerScanOutput', () => {
  const expectedRoot = path.join(volumeRoot, 'update', 'root')
  const expectedVenv = path.join(expectedRoot, 'venv')
  const target = { expectedRoot, expectedVenv }
  const parseObject = (value: unknown) => parseVenvBlockerScanOutput(JSON.stringify(value), target)

  it('accepts the exact clear v1 scan envelope', () => {
    const outcome = parseObject(scanEnvelope(expectedRoot, expectedVenv))
    assert.deepEqual(outcome, {
      kind: 'clear',
      result: { blocked: false, processes: [], mcpBridges: [], pausableGateways: 0 }
    })
  })

  it('accepts a generic blocker only with its exact hard-block tuple', () => {
    const outcome = parseObject(
      blockedScanEnvelope(expectedRoot, expectedVenv, { processes: [genericProcess()] })
    )

    assert.deepEqual(outcome, {
      kind: 'blocked',
      result: {
        blocked: true,
        processes: [
          { pid: 11, name: 'python.exe', cmdline: 'python.exe -m hermes_cli.main serve' }
        ],
        mcpBridges: [],
        pausableGateways: 0
      }
    })
  })

  it('accepts exact actionable and non-actionable MCP tuples', () => {
    const outcome = parseObject(
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        mcp_bridges: [
          mcpBridge(),
          mcpBridge({
            pid: 23,
            owner: 'unknown',
            role: 'mcp_bridge_wrapper',
            actionable: false,
            actionability: 'hard_block',
            action: 'refuse'
          })
        ]
      })
    )

    assert.equal(outcome.kind, 'blocked')

    if (outcome.kind === 'blocked') {
      assert.deepEqual(outcome.result.mcpBridges, [
        {
          pid: 22,
          name: 'python.exe',
          cmdline: 'python.exe -m agent.transports.hermes_tools_mcp_server',
          createdAt: 202.5,
          owner: 'codex',
          role: 'mcp_bridge_worker',
          actionable: true,
          actionability: 'exact_mcp_bridge',
          action: 'terminate_exact_mcp'
        },
        {
          pid: 23,
          name: 'python.exe',
          cmdline: 'python.exe -m agent.transports.hermes_tools_mcp_server',
          createdAt: 202.5,
          owner: 'unknown',
          role: 'mcp_bridge_wrapper',
          actionable: false,
          actionability: 'hard_block',
          action: 'refuse'
        }
      ])
    }
  })

  it('accepts a worker wrapper reference only when it names a wrapper record', () => {
    const outcome = parseObject(
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        mcp_bridges: [
          mcpBridge({ pid: 41, role: 'mcp_bridge_worker', wrapper_pid: 42 }),
          mcpBridge({ pid: 42, role: 'mcp_bridge_wrapper' })
        ]
      })
    )

    assert.equal(outcome.kind, 'blocked')

    if (outcome.kind === 'blocked') {
      assert.equal(outcome.result.mcpBridges[0]?.wrapperPid, 42)
      assert.equal(outcome.result.mcpBridges[1]?.wrapperPid, undefined)
    }
  })

  it('does not count an exact pausable gateway as a blocker', () => {
    const outcome = parseObject(
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway()]
      })
    )

    assert.deepEqual(outcome, {
      kind: 'clear',
      result: { blocked: false, processes: [], mcpBridges: [], pausableGateways: 1 }
    })
  })

  it('rejects malformed JSON', () => {
    assert.equal(parseVenvBlockerScanOutput('not json', target).kind, 'probe-failure')
  })

  it('requires exactly the 13 current scan envelope keys', () => {
    const exact = scanEnvelope(expectedRoot, expectedVenv)

    for (const key of Object.keys(exact)) {
      const missing = { ...exact }
      delete missing[key]
      assert.equal(parseObject(missing).kind, 'probe-failure', `missing ${key}`)
    }

    assert.equal(parseObject({ ...exact, legacy: true }).kind, 'probe-failure')
  })

  it('requires v1 scan success metadata and null error', () => {
    const invalidMetadata: Record<string, unknown>[] = [
      { schema_version: 2 },
      { mode: 'preflight' },
      { ok: false },
      { ok: 1 },
      { error: { code: 'probe_failed', message: 'failed' } },
      { error: undefined }
    ]

    for (const override of invalidMetadata) {
      assert.equal(
        parseObject(scanEnvelope(expectedRoot, expectedVenv, override)).kind,
        'probe-failure',
        JSON.stringify(override)
      )
    }
  })

  it('binds success to the requested canonical root and interpreter venv', () => {
    assert.equal(
      parseObject(scanEnvelope(path.join(volumeRoot, 'other', 'root'), expectedVenv)).kind,
      'probe-failure'
    )
    assert.equal(
      parseObject(scanEnvelope(expectedRoot, path.join(volumeRoot, 'other', 'venv'))).kind,
      'probe-failure'
    )
    assert.equal(
      parseVenvBlockerScanOutput(JSON.stringify(scanEnvelope(expectedRoot, expectedVenv)), {
        expectedRoot: 'relative-root',
        expectedVenv
      }).kind,
      'probe-failure'
    )
  })

  it('enforces ready, blocked, and reason from generic plus MCP blockers only', () => {
    const invalid = [
      scanEnvelope(expectedRoot, expectedVenv, { ready: false }),
      scanEnvelope(expectedRoot, expectedVenv, { blocked: true }),
      scanEnvelope(expectedRoot, expectedVenv, { reason: 'processes_running' }),
      blockedScanEnvelope(expectedRoot, expectedVenv),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess()],
        ready: true
      }),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess()],
        reason: null
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess()]
      })
    ]

    for (const envelope of invalid) {
      assert.equal(parseObject(envelope).kind, 'probe-failure')
    }
  })

  it('requires every process collection to be an array', () => {
    for (const override of [
      { processes: null },
      { mcp_bridges: null },
      { pausable_gateway_processes: null }
    ]) {
      assert.equal(
        parseObject(scanEnvelope(expectedRoot, expectedVenv, override)).kind,
        'probe-failure'
      )
    }
  })

  it('rejects missing, extra, or malformed generic process fields', () => {
    const exact = genericProcess()

    for (const key of ['pid', 'name', 'cmdline', 'owner', 'role', 'actionable', 'actionability', 'action']) {
      const missing = { ...exact }
      delete missing[key]
      assert.equal(
        parseObject(blockedScanEnvelope(expectedRoot, expectedVenv, { processes: [missing] })).kind,
        'probe-failure',
        `missing ${key}`
      )
    }

    const invalid = [
      genericProcess({ pid: 0 }),
      genericProcess({ pid: 1.5 }),
      genericProcess({ name: '' }),
      genericProcess({ cmdline: 4 }),
      genericProcess({ cmdline: 'x'.repeat(121) }),
      genericProcess({ created_at: 0 }),
      genericProcess({ extra: true })
    ]

    for (const processRecord of invalid) {
      assert.equal(
        parseObject(
          blockedScanEnvelope(expectedRoot, expectedVenv, { processes: [processRecord] })
        ).kind,
        'probe-failure'
      )
    }
  })

  it('rejects every generic tuple other than desktop/backend or unknown/other hard block', () => {
    const invalid = [
      genericProcess({ owner: 'unknown', role: 'desktop_backend' }),
      genericProcess({ owner: 'desktop', role: 'other' }),
      genericProcess({ owner: 'gateway', role: 'gateway_run' }),
      genericProcess({ actionable: true }),
      genericProcess({ actionability: 'downstream_drainable' }),
      genericProcess({ action: 'pause_downstream' })
    ]

    for (const processRecord of invalid) {
      assert.equal(
        parseObject(
          blockedScanEnvelope(expectedRoot, expectedVenv, { processes: [processRecord] })
        ).kind,
        'probe-failure'
      )
    }

    assert.equal(
      parseObject(
        blockedScanEnvelope(expectedRoot, expectedVenv, {
          processes: [genericProcess({ owner: 'unknown', role: 'other' })]
        })
      ).kind,
      'blocked'
    )
  })

  it('requires exact MCP keys, scalar fields, roles, and owner/action tuple', () => {
    const exact = mcpBridge()

    for (const key of [
      'pid',
      'name',
      'cmdline',
      'created_at',
      'owner',
      'role',
      'actionable',
      'actionability',
      'action'
    ]) {
      const missing = { ...exact }
      delete missing[key]
      assert.equal(
        parseObject(
          blockedScanEnvelope(expectedRoot, expectedVenv, { mcp_bridges: [missing] })
        ).kind,
        'probe-failure',
        `missing ${key}`
      )
    }

    const invalid = [
      mcpBridge({ pid: 0 }),
      mcpBridge({ name: '' }),
      mcpBridge({ cmdline: 'x'.repeat(121) }),
      mcpBridge({ created_at: Number.NaN }),
      mcpBridge({ created_at: 0 }),
      mcpBridge({ owner: 'gateway' }),
      mcpBridge({ role: 'other' }),
      mcpBridge({ actionable: false }),
      mcpBridge({ actionability: 'hard_block' }),
      mcpBridge({ action: 'refuse' }),
      mcpBridge({ owner: 'unknown', actionable: true }),
      mcpBridge({ owner: 'desktop', actionable: false }),
      mcpBridge({ extra: true })
    ]

    for (const bridge of invalid) {
      assert.equal(
        parseObject(
          blockedScanEnvelope(expectedRoot, expectedVenv, { mcp_bridges: [bridge] })
        ).kind,
        'probe-failure'
      )
    }
  })

  it('rejects impossible MCP wrapper relationships', () => {
    const invalidLists = [
      [mcpBridge({ role: 'mcp_bridge_wrapper', wrapper_pid: 99 })],
      [mcpBridge({ wrapper_pid: 22 })],
      [mcpBridge({ wrapper_pid: 99 })],
      [mcpBridge({ wrapper_pid: 23 }), mcpBridge({ pid: 23, role: 'mcp_bridge_worker' })],
      [mcpBridge({ wrapper_pid: 0 })]
    ]

    for (const mcp_bridges of invalidLists) {
      assert.equal(
        parseObject(blockedScanEnvelope(expectedRoot, expectedVenv, { mcp_bridges })).kind,
        'probe-failure'
      )
    }
  })

  it('requires gateway count equality and the exact downstream-drainable tuple', () => {
    const invalid = [
      scanEnvelope(expectedRoot, expectedVenv, { pausable_gateways: -1 }),
      scanEnvelope(expectedRoot, expectedVenv, { pausable_gateways: 0.5 }),
      scanEnvelope(expectedRoot, expectedVenv, { pausable_gateways: '1' }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: []
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ owner: 'desktop' })]
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ role: 'desktop_backend' })]
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ actionable: true })]
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ actionability: 'hard_block' })]
      }),
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ action: 'refuse' })]
      })
    ]

    const missingCreatedAt = pausableGateway()
    delete missingCreatedAt.created_at
    invalid.push(
      scanEnvelope(expectedRoot, expectedVenv, {
        pausable_gateways: 1,
        pausable_gateway_processes: [missingCreatedAt]
      })
    )

    for (const envelope of invalid) {
      assert.equal(parseObject(envelope).kind, 'probe-failure')
    }
  })

  it('requires every reported process PID to be unique across all three lists', () => {
    const invalid = [
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess(), genericProcess()]
      }),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        mcp_bridges: [mcpBridge(), mcpBridge()]
      }),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess()],
        mcp_bridges: [mcpBridge({ pid: 11 })]
      }),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        processes: [genericProcess()],
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ pid: 11 })]
      }),
      blockedScanEnvelope(expectedRoot, expectedVenv, {
        mcp_bridges: [mcpBridge()],
        pausable_gateways: 1,
        pausable_gateway_processes: [pausableGateway({ pid: 22 })]
      })
    ]

    for (const envelope of invalid) {
      assert.equal(parseObject(envelope).kind, 'probe-failure')
    }
  })
})

// ---------------------------------------------------------------------------
// scanVenvBlockers — subprocess boundary
// ---------------------------------------------------------------------------

describe('scanVenvBlockers', () => {
  const requestedRoot = path.join(volumeRoot, 'requested', '..', 'install')
  const canonicalRoot = path.join(volumeRoot, 'canonical', 'install')
  const venvPython = path.join(canonicalRoot, '.venv', 'Scripts', 'python.exe')
  const unresolvedVenv = path.dirname(path.dirname(venvPython))
  const expectedVenv = path.join(canonicalRoot, 'resolved-venv')
  const resolveVenv = () => venvPython

  const canonicalize = (value: string) => {
    if (value === requestedRoot) {return canonicalRoot}

    if (value === unresolvedVenv) {return expectedVenv}
    throw new Error(`unexpected canonicalization target: ${value}`)
  }

  it('canonicalizes the venv directory separately before accepting its echoed identity', async () => {
    const canonicalized: string[] = []

    const outcome = await scanVenvBlockers(
      requestedRoot,
      execReturn(scanEnvelope(canonicalRoot, expectedVenv)),
      resolveVenv,
      value => {
        canonicalized.push(value)

        return canonicalize(value)
      }
    )

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(canonicalized, [requestedRoot, unresolvedVenv])
  })

  it('returns blocked for an exact generic blocker response', async () => {
    const outcome = await scanVenvBlockers(
      requestedRoot,
      execReturn(
        blockedScanEnvelope(canonicalRoot, expectedVenv, { processes: [genericProcess()] })
      ),
      resolveVenv,
      canonicalize
    )

    assert.equal(outcome.kind, 'blocked')
  })

  it('rejects legacy response defaults and mismatched target identity', async () => {
    const legacy = await scanVenvBlockers(
      requestedRoot,
      execReturn({ ok: true, blocked: false, processes: [] }),
      resolveVenv,
      canonicalize
    )

    assert.equal(legacy.kind, 'probe-failure')

    const wrongRoot = await scanVenvBlockers(
      requestedRoot,
      execReturn(scanEnvelope(requestedRoot, expectedVenv)),
      resolveVenv,
      canonicalize
    )

    assert.equal(wrongRoot.kind, 'probe-failure')

    const wrongVenv = await scanVenvBlockers(
      requestedRoot,
      execReturn(scanEnvelope(canonicalRoot, path.join(canonicalRoot, 'venv'))),
      resolveVenv,
      canonicalize
    )

    assert.equal(wrongVenv.kind, 'probe-failure')
  })

  it('passes the canonical root to the target interpreter with a scrubbed environment', async () => {
    const calls: any[] = []

    const exec = (async (command: string, args: string[], options: any) => {
      calls.push({ command, args, options })

      return { stdout: JSON.stringify(scanEnvelope(canonicalRoot, expectedVenv)), stderr: '' }
    }) as any

    await scanVenvBlockers(requestedRoot, exec, resolveVenv, canonicalize)

    assert.equal(calls.length, 1)
    assert.equal(calls[0].command, venvPython)
    assert.deepEqual(calls[0].args, [
      '-m',
      'hermes_cli._scan_venv_blockers',
      '--root',
      canonicalRoot
    ])
    assert.equal(calls[0].options.cwd, canonicalRoot)
    assert.equal(calls[0].options.windowsHide, true)
    assert.ok(calls[0].options.timeout > 0)
    assert.equal(Object.hasOwn(calls[0].options.env, 'PYTHONPATH'), false)
  })

  it('fails closed before execution when root, interpreter, or venv canonicalization fails', async () => {
    let calls = 0

    const exec = (async () => {
      calls += 1

      return { stdout: '', stderr: '' }
    }) as any

    const badRoot = await scanVenvBlockers(requestedRoot, exec, resolveVenv, () => {
      throw new Error('unreadable')
    })

    const missingVenv = await scanVenvBlockers(requestedRoot, exec, () => null, canonicalize)

    const badVenv = await scanVenvBlockers(requestedRoot, exec, resolveVenv, value => {
      if (value === requestedRoot) {return canonicalRoot}
      throw new Error('venv unreadable')
    })

    assert.equal(badRoot.kind, 'probe-failure')
    assert.equal(missingVenv.kind, 'probe-failure')
    assert.equal(badVenv.kind, 'probe-failure')
    assert.equal(calls, 0)
  })

  it('fails closed for subprocess errors and malformed output', async () => {
    const failed = await scanVenvBlockers(
      requestedRoot,
      execThrow(2, 'ModuleNotFoundError'),
      resolveVenv,
      canonicalize
    )

    const malformed = await scanVenvBlockers(
      requestedRoot,
      execReturn('not json'),
      resolveVenv,
      canonicalize
    )

    assert.equal(failed.kind, 'probe-failure')
    assert.equal(malformed.kind, 'probe-failure')
  })
})

// ---------------------------------------------------------------------------
// terminateMcpBridge — exact action response contract
// ---------------------------------------------------------------------------

describe('terminateMcpBridge', () => {
  const requestedRoot = path.join(volumeRoot, 'requested', 'install')
  const canonicalRoot = path.join(volumeRoot, 'canonical', 'install')
  const venvPython = path.join(canonicalRoot, '.venv', 'Scripts', 'python.exe')
  const unresolvedVenv = path.dirname(path.dirname(venvPython))
  const expectedVenv = path.join(canonicalRoot, 'resolved-venv')
  const resolveVenv = () => venvPython

  const canonicalize = (value: string) => {
    if (value === requestedRoot) {return canonicalRoot}

    if (value === unresolvedVenv) {return expectedVenv}
    throw new Error(`unexpected canonicalization target: ${value}`)
  }

  const bridge: McpBridgeProcess = {
    pid: 44,
    name: 'python.exe',
    cmdline: 'python.exe -m agent.transports.hermes_tools_mcp_server',
    createdAt: 123.75,
    owner: 'codex',
    role: 'mcp_bridge_wrapper',
    actionable: true,
    actionability: 'exact_mcp_bridge',
    action: 'terminate_exact_mcp'
  }

  const terminationEnvelope = (overrides: Record<string, unknown> = {}) => ({
    schema_version: 1,
    mode: 'terminate_mcp_bridge',
    ok: true,
    terminated: true,
    pid: bridge.pid,
    created_at: bridge.createdAt,
    root: canonicalRoot,
    venv: expectedVenv,
    error: null,
    ...overrides
  })

  it('returns true only for the exact 9-key response echoing the requested identity', async () => {
    const calls: any[] = []
    const canonicalized: string[] = []

    const exec = (async (command: string, args: string[], options: any) => {
      calls.push({ command, args, options })

      return { stdout: JSON.stringify(terminationEnvelope()), stderr: '' }
    }) as any

    const terminated = await terminateMcpBridge(
      requestedRoot,
      bridge,
      exec,
      resolveVenv,
      value => {
        canonicalized.push(value)

        return canonicalize(value)
      }
    )

    assert.equal(terminated, true)
    assert.deepEqual(canonicalized, [requestedRoot, unresolvedVenv])
    assert.equal(calls.length, 1)
    assert.equal(calls[0].command, venvPython)
    assert.deepEqual(calls[0].args, [
      '-m',
      'hermes_cli._scan_venv_blockers',
      '--root',
      canonicalRoot,
      '--terminate-mcp-bridge',
      '44',
      '--created-at',
      '123.75'
    ])
    assert.equal(calls[0].options.cwd, canonicalRoot)
    assert.equal(calls[0].options.windowsHide, true)
    assert.equal(Object.hasOwn(calls[0].options.env, 'PYTHONPATH'), false)
  })

  it('returns false when the scanner reports changed identity without terminating', async () => {
    const terminated = await terminateMcpBridge(
      requestedRoot,
      bridge,
      execReturn(terminationEnvelope({ terminated: false })),
      resolveVenv,
      canonicalize
    )

    assert.equal(terminated, false)
  })

  it('rejects missing or additional termination envelope keys', async () => {
    const exact = terminationEnvelope()

    for (const key of Object.keys(exact)) {
      const missing = { ...exact }
      delete missing[key]
      assert.equal(
        await terminateMcpBridge(
          requestedRoot,
          bridge,
          execReturn(missing),
          resolveVenv,
          canonicalize
        ),
        false,
        `missing ${key}`
      )
    }

    assert.equal(
      await terminateMcpBridge(
        requestedRoot,
        bridge,
        execReturn({ ...exact, legacy: true }),
        resolveVenv,
        canonicalize
      ),
      false
    )
  })

  it('rejects legacy, malformed, non-v1, and non-success action responses', async () => {
    const invalid: unknown[] = [
      { ok: true, terminated: true },
      'not json',
      terminationEnvelope({ schema_version: 2 }),
      terminationEnvelope({ mode: 'scan' }),
      terminationEnvelope({ ok: false }),
      terminationEnvelope({ terminated: 1 }),
      terminationEnvelope({ error: { code: 'probe_failed' } })
    ]

    for (const response of invalid) {
      assert.equal(
        await terminateMcpBridge(
          requestedRoot,
          bridge,
          execReturn(response),
          resolveVenv,
          canonicalize
        ),
        false
      )
    }
  })

  it('rejects any action response that does not echo PID, create time, root, and venv', async () => {
    const invalid = [
      terminationEnvelope({ pid: bridge.pid + 1 }),
      terminationEnvelope({ created_at: bridge.createdAt + 0.01 }),
      terminationEnvelope({ root: requestedRoot }),
      terminationEnvelope({ venv: path.join(canonicalRoot, 'venv') })
    ]

    for (const response of invalid) {
      assert.equal(
        await terminateMcpBridge(
          requestedRoot,
          bridge,
          execReturn(response),
          resolveVenv,
          canonicalize
        ),
        false
      )
    }
  })

  it('never invokes Python without exact owner and actionability proof', async () => {
    let calls = 0

    const exec = (async () => {
      calls += 1

      return { stdout: JSON.stringify(terminationEnvelope()), stderr: '' }
    }) as any

    const unproven = {
      ...bridge,
      owner: 'unknown' as const
    }

    assert.equal(
      await terminateMcpBridge(requestedRoot, unproven, exec, resolveVenv, canonicalize),
      false
    )
    assert.equal(calls, 0)
  })

  it('fails closed before execution when root, interpreter, or venv canonicalization fails', async () => {
    let calls = 0

    const exec = (async () => {
      calls += 1

      return { stdout: JSON.stringify(terminationEnvelope()), stderr: '' }
    }) as any

    assert.equal(
      await terminateMcpBridge(requestedRoot, bridge, exec, resolveVenv, () => {
        throw new Error('unreadable')
      }),
      false
    )
    assert.equal(
      await terminateMcpBridge(requestedRoot, bridge, exec, () => null, canonicalize),
      false
    )
    assert.equal(
      await terminateMcpBridge(requestedRoot, bridge, exec, resolveVenv, value => {
        if (value === requestedRoot) {return canonicalRoot}
        throw new Error('venv unreadable')
      }),
      false
    )
    assert.equal(calls, 0)
  })
})
