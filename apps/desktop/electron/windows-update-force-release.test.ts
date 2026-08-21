import assert from 'node:assert/strict'

import { describe, it, vi } from 'vitest'

import {
  mergeInstallHolders,
  orderHoldersLeafFirst,
  runWindowsUpdateForceRelease,
  type ForceReleaseHolder,
  type ForceReleaseTerminateResult,
  type WindowsUpdateForceReleaseDeps
} from './windows-update-force-release'

const holder = (overrides: Partial<ForceReleaseHolder> = {}): ForceReleaseHolder => ({
  pid: 57012,
  createdAt: 1_700_000_000,
  name: 'hermes.exe',
  cmdline: 'hermes.exe tools',
  source: 'scanner',
  ...overrides
})

function makeDeps(
  overrides: Partial<WindowsUpdateForceReleaseDeps> = {}
): {
  calls: string[]
  deps: WindowsUpdateForceReleaseDeps
  setLocked: (locked: boolean) => void
} {
  const calls: string[] = []
  let locked = true
  let clock = 0

  const deps: WindowsUpdateForceReleaseDeps = {
    now: () => clock,
    wait: async ms => {
      calls.push(`wait:${ms}`)
      clock += Math.max(0, ms)
    },
    isResourceLocked: async () => locked,
    listScannerHolders: async () => {
      calls.push('scan')
      return []
    },
    listRestartManagerHolders: async () => {
      calls.push('rm')
      return []
    },
    terminateHolder: async target => {
      calls.push(`terminate:${target.pid}:${target.createdAt}`)
      return { kind: 'terminated' }
    },
    excludePids: new Set([42]),
    deadlineMs: 5_000,
    settleMs: 0,
    ...overrides
  }

  return {
    calls,
    deps,
    setLocked: next => {
      locked = next
    }
  }
}

describe('orderHoldersLeafFirst', () => {
  it('terminates workers before their wrappers and children before parents', () => {
    const wrapper = holder({ pid: 10, role: 'wrapper' })
    const worker = holder({ pid: 11, wrapperPid: 10, role: 'worker' })
    const child = holder({ pid: 12, parentPid: 11, role: 'worker' })
    const ordered = orderHoldersLeafFirst([wrapper, worker, child])

    assert.deepEqual(
      ordered.map(entry => entry.pid),
      [12, 11, 10]
    )
  })
})

describe('mergeInstallHolders', () => {
  it('dedupes by pid+createdAt and unions scanner with Restart Manager evidence', () => {
    const fromScan = holder({ pid: 7, createdAt: 100, source: 'scanner', resource: 'venv\\Scripts\\hermes.exe' })
    const fromRm = holder({
      pid: 7,
      createdAt: 100,
      source: 'restart-manager',
      resource: 'venv\\Lib\\site-packages\\foo.pyd'
    })
    const other = holder({ pid: 8, createdAt: 200, source: 'restart-manager' })

    const merged = mergeInstallHolders([fromScan, fromRm, other])

    assert.equal(merged.length, 2)
    const first = merged.find(entry => entry.pid === 7)
    assert.ok(first)
    assert.equal(first.source, 'scanner')
    assert.match(String(first.resource), /foo\.pyd/)
  })

  it('drops excluded helper and desktop PIDs', () => {
    const merged = mergeInstallHolders([holder({ pid: 42 }), holder({ pid: 99 })], new Set([42]))
    assert.deepEqual(
      merged.map(entry => entry.pid),
      [99]
    )
  })
})

describe('runWindowsUpdateForceRelease', () => {
  it('returns clear immediately when the install resources are already unlocked', async () => {
    const { calls, deps, setLocked } = makeDeps()
    setLocked(false)

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(calls, [])
  })

  it('force-drains the hermes tools|head orphan class via scanner holders within five seconds', async () => {
    const orphan = holder({
      pid: 57012,
      createdAt: 1_700_000_123.5,
      name: 'hermes.exe',
      cmdline: 'hermes.exe tools',
      resource: 'venv\\Scripts\\hermes.exe'
    })
    const child = holder({
      pid: 57099,
      createdAt: 1_700_000_124,
      parentPid: 57012,
      name: 'python.exe',
      cmdline: 'python.exe -m hermes_cli.main tools'
    })

    let locked = true
    const terminated = new Set<number>()
    const { calls, deps } = makeDeps({
      isResourceLocked: async () => locked,
      listScannerHolders: async () => {
        calls.push('scan')
        return [orphan, child].filter(entry => !terminated.has(entry.pid))
      },
      listRestartManagerHolders: async () => {
        calls.push('rm')
        return []
      },
      terminateHolder: async target => {
        calls.push(`terminate:${target.pid}`)
        terminated.add(target.pid)
        if (terminated.has(orphan.pid) && terminated.has(child.pid)) {
          locked = false
        }
        return { kind: 'terminated' }
      },
      settleMs: 10
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'clear')
    assert.ok(calls.indexOf('terminate:57099') < calls.indexOf('terminate:57012'), 'leaf before root')
    assert.ok(calls.includes('scan'))
    assert.ok((deps.now?.() ?? 0) <= 5_000)
  })

  it('refuses PID reuse when create-time no longer matches', async () => {
    const stale = holder({ pid: 88, createdAt: 111 })
    const { deps } = makeDeps({
      listScannerHolders: async () => [stale],
      terminateHolder: async () => ({ kind: 'create-time-mismatch' })
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'blocked')
    if (outcome.kind === 'blocked') {
      assert.match(outcome.message, /create-time|PID reuse|no longer matches/i)
    }
  })

  it('escalates to elevation when OpenProcess/TerminateProcess returns access denied', async () => {
    const elevatedTarget = holder({ pid: 901, createdAt: 222, name: 'python.exe' })
    const { deps } = makeDeps({
      listScannerHolders: async () => [elevatedTarget],
      terminateHolder: async () => ({ kind: 'access-denied', win32Error: 5 })
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'needs-elevation')
    if (outcome.kind === 'needs-elevation') {
      assert.equal(outcome.holders[0]?.pid, 901)
      assert.match(outcome.message, /Administrator|elevat/i)
    }
  })

  it('surfaces protected-process terminal failure without claiming the venv is clear', async () => {
    const protectedHolder = holder({ pid: 77, createdAt: 333, resource: 'venv\\python.exe' })
    const { deps } = makeDeps({
      listScannerHolders: async () => [protectedHolder],
      terminateHolder: async () => ({ kind: 'protected', win32Error: 5 })
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'blocked')
    if (outcome.kind === 'blocked') {
      assert.match(outcome.message, /PID 77/)
      assert.match(outcome.message, /protected|unkillable|Win32/i)
    }
  })

  it('excludes the active updater helper and desktop main process', async () => {
    const helper = holder({ pid: 4242, createdAt: 1 })
    const desktop = holder({ pid: 42, createdAt: 2 })
    const real = holder({ pid: 99, createdAt: 3 })
    let locked = true
    const terminated: number[] = []
    const { deps } = makeDeps({
      excludePids: new Set([42, 4242]),
      isResourceLocked: async () => locked,
      listScannerHolders: async () => [helper, desktop, real],
      terminateHolder: async target => {
        terminated.push(target.pid)
        locked = false
        return { kind: 'terminated' }
      }
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(terminated, [99])
  })

  it('stops the quick path within five seconds even when holders respawn', async () => {
    const zombie = holder({ pid: 55, createdAt: 9 })
    const { deps } = makeDeps({
      listScannerHolders: async () => [zombie],
      terminateHolder: async () => ({ kind: 'terminated' } satisfies ForceReleaseTerminateResult),
      settleMs: 2_000,
      deadlineMs: 5_000
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.ok(outcome.kind === 'timeout' || outcome.kind === 'blocked' || outcome.kind === 'needs-elevation')
    assert.ok((deps.now?.() ?? 0) <= 5_000)
  })

  it('uses Restart Manager holders when the scanner is empty but the shim stays locked', async () => {
    const rmOnly = holder({
      pid: 606,
      createdAt: 404,
      source: 'restart-manager',
      resource: 'venv\\Scripts\\hermes.exe'
    })
    let locked = true
    const terminated: number[] = []
    const { deps } = makeDeps({
      isResourceLocked: async () => locked,
      listScannerHolders: async () => [],
      listRestartManagerHolders: async () => [rmOnly],
      terminateHolder: async target => {
        terminated.push(target.pid)
        locked = false
        return { kind: 'terminated' }
      }
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.equal(outcome.kind, 'clear')
    assert.deepEqual(terminated, [606])
  })

  it('never reports clear while any verified holder remains', async () => {
    const survivor = holder({ pid: 1, createdAt: 2 })
    const terminate = vi.fn(async (): Promise<ForceReleaseTerminateResult> => ({ kind: 'terminated' }))
    const { deps } = makeDeps({
      listScannerHolders: async () => [survivor],
      terminateHolder: terminate,
      settleMs: 0,
      deadlineMs: 100
    })

    const outcome = await runWindowsUpdateForceRelease(deps)

    assert.notEqual(outcome.kind, 'clear')
  })
})
