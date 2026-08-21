import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { describe, it, vi } from 'vitest'

import {
  mergeInstallHolders,
  orderHoldersLeafFirst,
  raceWithBudget,
  runWindowsUpdateForceRelease,
  type ForceReleaseHolder,
  type ForceReleaseTerminateResult,
  type WindowsUpdateForceReleaseDeps
} from './windows-update-force-release'
import {
  buildRestartManagerScript,
  parseRestartManagerOutput,
  RESTART_MANAGER_ROW_SPLIT_EXPRESSION
} from './windows-restart-manager'
import {
  buildForceReleaseRequest,
  canonicalForceReleasePayload,
  canonicalNumericToken,
  formatElevatedForceReleaseFailure,
  parseForceReleaseResponse,
  verifyForceReleaseRequest
} from './windows-elevated-force-release'
import { parseTerminateScriptOutput } from './windows-process-terminate'

const execFileAsync = promisify(execFile)

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

  it('enforces a hard wall-clock budget when dependencies hang past five seconds', async () => {
    const started = Date.now()
    const hung = () =>
      new Promise<ForceReleaseHolder[]>(resolve => {
        setTimeout(() => resolve([holder({ pid: 1, createdAt: 2 })]), 8_000)
      })

    const outcome = await runWindowsUpdateForceRelease({
      deadlineMs: 400,
      settleMs: 0,
      isResourceLocked: async () => true,
      listScannerHolders: async () => hung(),
      listRestartManagerHolders: async () => [],
      terminateHolder: async () => ({ kind: 'terminated' })
    })

    const elapsed = Date.now() - started
    assert.notEqual(outcome.kind, 'clear')
    assert.ok(elapsed < 2_000, `elapsed ${elapsed}ms must stay near the 400ms budget`)
    assert.ok(elapsed >= 300, `elapsed ${elapsed}ms should wait roughly the budget`)
  })

  it('passes remaining budget into terminateHolder', async () => {
    const budgets: number[] = []
    const target = holder({ pid: 3, createdAt: 4 })
    let locked = true
    const { deps } = makeDeps({
      deadlineMs: 1_000,
      settleMs: 0,
      isResourceLocked: async () => locked,
      listScannerHolders: async () => [target],
      terminateHolder: async (_holder, budgetMs) => {
        budgets.push(budgetMs)
        locked = false
        return { kind: 'terminated' }
      }
    })

    const outcome = await runWindowsUpdateForceRelease(deps)
    assert.equal(outcome.kind, 'clear')
    assert.ok(budgets.length >= 1)
    assert.ok(budgets[0]! <= 1_000)
    assert.ok(budgets[0]! > 0)
  })
})

describe('raceWithBudget', () => {
  it('returns the timeout fallback when work exceeds the budget', async () => {
    const started = Date.now()
    const value = await raceWithBudget(
      new Promise<string>(resolve => setTimeout(() => resolve('late'), 1_000)),
      100,
      () => 'fallback'
    )
    const elapsed = Date.now() - started
    assert.equal(value, 'fallback')
    assert.ok(elapsed < 500)
  })
})

describe('restart manager script contract', () => {
  it('emits a literal pipe split that does not over-escape regex', () => {
    const script = buildRestartManagerScript(['C:\\h\\venv\\Scripts\\hermes.exe'])
    assert.match(script, /\$part\.Split\(\[char\]'\|', 3\)/)
    assert.doesNotMatch(script, /\$part -split '\\\\\|'/)
    assert.doesNotMatch(script, /\$part -split '\\\|'/)
    assert.match(script, /StringBuilder\(CCH_RM_SESSION_KEY \+ 1\)/)
    assert.equal(RESTART_MANAGER_ROW_SPLIT_EXPRESSION, "$part.Split([char]'|', 3)")
  })

  it('parses RM JSON rows into force-release holders', () => {
    const holders = parseRestartManagerOutput(
      JSON.stringify([{ pid: 12, createdAt: 34, name: 'python.exe' }]),
      ['C:\\h\\venv\\Scripts\\hermes.exe']
    )
    assert.equal(holders.length, 1)
    assert.equal(holders[0]?.source, 'restart-manager')
    assert.equal(holders[0]?.pid, 12)
    assert.match(String(holders[0]?.resource), /hermes\.exe/)
  })

  it('splits generated RM delimiter rows correctly on real Windows PowerShell', async () => {
    if (process.platform !== 'win32') return

    const probe = `
$ErrorActionPreference = 'Stop'
$part = '12|34|name'
$bits = $part.Split([char]'|', 3)
if ($bits.Count -ne 3) { Write-Output ("bad-count=" + $bits.Count); exit 2 }
if ($bits[0] -ne '12' -or $bits[1] -ne '34' -or $bits[2] -ne 'name') {
  Write-Output ("bad-values=" + ($bits -join ','))
  exit 3
}
Write-Output 'ok'
exit 0
`.trim()

    const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const { stdout } = await execFileAsync(
      ps,
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', probe],
      { encoding: 'utf8', windowsHide: true, timeout: 10_000 }
    )
    assert.match(String(stdout), /ok/)
  })
})

describe('elevated force-release request contract', () => {
  it('binds request MAC to install root + exact holder claims', () => {
    const secret = 's'.repeat(32)
    const request = buildForceReleaseRequest({
      installRoot: 'C:\\Users\\gwmai\\AppData\\Local\\hermes',
      holders: [{ pid: 9, createdAt: 100, name: 'hermes.exe', cmdline: 'hermes.exe tools', source: 'scanner' }],
      secret,
      now: 1_000,
      ttlMs: 60_000,
      nonce: 'abc123'
    })

    assert.equal(request.nonce, 'abc123')
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\hermes', 1_500).ok,
      true
    )
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\other', 1_500).ok,
      false
    )
    assert.equal(verifyForceReleaseRequest(request, 'wrong', request.installRoot, 1_500).ok, false)
    assert.equal(verifyForceReleaseRequest(request, secret, request.installRoot, 100_000).ok, false)
  })

  it('canonical payload is stable for helper MAC verification', () => {
    const payload = canonicalForceReleasePayload({
      schemaVersion: 1,
      nonce: 'n',
      issuedAt: 1,
      expiresAt: 2,
      installRoot: 'C:\\h',
      installRootHash: 'abc',
      holders: [{ pid: 1, createdAt: 2, name: 'x', resource: 'y' }]
    })
    assert.equal(payload, ['1', 'n', '1', '2', 'C:\\h', 'abc', '1\t2\tx\ty'].join('\n'))
  })

  it('uses round-trip numeric tokens that match PowerShell R-format floats', async () => {
    const sample = 1755738237.4531252
    assert.equal(canonicalNumericToken(sample), '1755738237.4531252')
    assert.equal(canonicalNumericToken(1000), '1000')

    if (process.platform !== 'win32') return

    const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const script = `
$n = [double]1755738237.4531252
$js = '1755738237.4531252'
$r = $n.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
if ($r -ne $js) { Write-Output ("mismatch r=$r"); exit 2 }
$intTok = if (1000 -eq [math]::Truncate(1000)) { [string][int64]1000 } else { 'nope' }
if ($intTok -ne '1000') { Write-Output ("int=$intTok"); exit 3 }
Write-Output 'parity-ok'
exit 0
`.trim()
    const { stdout } = await execFileAsync(
      ps,
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', windowsHide: true, timeout: 10_000 }
    )
    assert.match(String(stdout), /parity-ok/)
  })

  it('rejects response nonce mismatch', () => {
    const raw = JSON.stringify({ schemaVersion: 1, nonce: 'other', ok: true, cleared: true })
    assert.equal(parseForceReleaseResponse(raw, 'expected'), null)
  })

  it('surfaces survivor pid/resource/win32 details in elevated failure text', () => {
    const failure = formatElevatedForceReleaseFailure({
      schemaVersion: 1,
      nonce: 'n',
      ok: true,
      cleared: false,
      survivors: [{ pid: 55, detail: 'protected win32=5', resource: 'C:\\h\\venv\\Scripts\\hermes.exe', win32Error: 5 }]
    })
    assert.match(failure.message, /PID 55/)
    assert.match(failure.message, /hermes\.exe/)
    assert.match(failure.message, /protected|win32=5/i)
    assert.equal(failure.protectedHolders, true)
  })
})

describe('terminate script output parser', () => {
  it('classifies create-time mismatch, access denied, and protected', () => {
    assert.deepEqual(parseTerminateScriptOutput('CREATE_TIME_MISMATCH actual=1 expected=2', 3), {
      kind: 'create-time-mismatch'
    })
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED', 5), {
      kind: 'access-denied',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('PROTECTED win32=5', 5), {
      kind: 'protected',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('TERMINATED', 0), { kind: 'terminated' })
    assert.deepEqual(parseTerminateScriptOutput('ALREADY_GONE', 0), { kind: 'already-gone' })
    assert.equal(parseTerminateScriptOutput('FAILED win32=87', 1).kind, 'failed')
    assert.equal(parseTerminateScriptOutput('FAILED win32=6', 1).kind, 'failed')
  })
})

describe('elevated helper script shape', () => {
  it('does not assign the read-only $pid automatic variable', () => {
    const helperPath = path.resolve(__dirname, '../../../scripts/desktop-update/windows-force-release.ps1')
    const text = fs.readFileSync(helperPath, 'utf8')
    assert.match(text, /\$holderPid\s*=/)
    assert.doesNotMatch(text, /\$pid\s*=\s*\[int\]\$holder\.pid/)
    assert.match(text, /Format-CanonicalNumber/)
    assert.match(text, /QueryRestartManager/)
    assert.match(text, /resource-still-locked|Test-FileUnlocked/)
  })

  it('parses under Windows PowerShell without script errors', async () => {
    if (process.platform !== 'win32') return
    const helperPath = path.resolve(__dirname, '../../../scripts/desktop-update/windows-force-release.ps1')
    const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const script = `
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(${JSON.stringify(helperPath)}, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors -and $errors.Count -gt 0) {
  $errors | ForEach-Object { Write-Output $_.ToString() }
  exit 2
}
# $pid assignment must remain impossible under StrictMode
try {
  Set-StrictMode -Version Latest
  $pid = 1
  Write-Output 'pid-assignable'
  exit 3
} catch {
  Write-Output 'pid-readonly-ok'
}
Write-Output 'parse-ok'
exit 0
`.trim()
    const { stdout } = await execFileAsync(
      ps,
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', windowsHide: true, timeout: 15_000 }
    )
    assert.match(String(stdout), /pid-readonly-ok/)
    assert.match(String(stdout), /parse-ok/)
  })
})
