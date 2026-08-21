import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { describe, it, vi } from 'vitest'

import {
  mergeInstallHolders,
  orderHoldersLeafFirst,
  attachHolderTreeRelationships,
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
import {
  buildExactTerminateScript,
  parseTerminateScriptOutput,
  runPowerShellWithHardBoundary
} from './windows-process-terminate'

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
    assert.match(script, /\$part\.Split\(\[char\]'\|', 4\)/)
    assert.doesNotMatch(script, /\$part -split '\\\\\|'/)
    assert.doesNotMatch(script, /\$part -split '\\\|'/)
    assert.match(script, /StringBuilder\(CCH_RM_SESSION_KEY \+ 1\)/)
    assert.equal(RESTART_MANAGER_ROW_SPLIT_EXPRESSION, "$part.Split([char]'|', 4)")
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
$part = '12|34|name|C:\h\venv\Scripts\hermes.exe'
$bits = $part.Split([char]'|', 4)
if ($bits.Count -ne 4) { Write-Output ("bad-count=" + $bits.Count); exit 2 }
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
    assert.equal(payload, ['1', 'n', '1', '2', 'C:\\h', 'abc', '1\t2\tx\ty', ''].join('\n'))
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
    assert.match(text, /excludePids/)
    assert.doesNotMatch(text, /Get-CimInstance Win32_Process\s*\|\s*ForEach-Object/)
    assert.match(text, /never terminate unauthenticated/i)
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

describe('merge create-time tolerance and production leaf-first', () => {
  it('dedupes scanner fractional create-time with RM integer seconds', () => {
    const fromScan = holder({
      pid: 7,
      createdAt: 100.4,
      source: 'scanner',
      resource: 'venv\\Scripts\\hermes.exe'
    })
    const fromRm = holder({
      pid: 7,
      createdAt: 100,
      source: 'restart-manager',
      resource: 'venv\\Scripts\\python.exe'
    })
    const merged = mergeInstallHolders([fromScan, fromRm])
    assert.equal(merged.length, 1)
    assert.equal(merged[0]?.pid, 7)
    assert.match(String(merged[0]?.resource), /python\.exe/)
    assert.match(String(merged[0]?.resource), /hermes\.exe/)
  })

  it('orders production generic holders leaf-first when parentPid evidence is present', () => {
    const root = holder({ pid: 10, createdAt: 1, role: 'other' })
    const child = holder({ pid: 11, createdAt: 2, parentPid: 10, role: 'other' })
    const ordered = orderHoldersLeafFirst(attachHolderTreeRelationships([root, child]))
    assert.deepEqual(
      ordered.map(entry => entry.pid),
      [11, 10]
    )
  })
})

describe('termination cancellation hard boundary', () => {
  it('budget-owned terminate returns without scheduling post-return mutation', async () => {
    const target = holder({ pid: 404, createdAt: 1 })
    let mutated = false
    const started = Date.now()
    const outcome = await runWindowsUpdateForceRelease({
      deadlineMs: 400,
      settleMs: 0,
      isResourceLocked: async () => true,
      listScannerHolders: async () => [target],
      listRestartManagerHolders: async () => [],
      terminateHolder: async (_holder, budget) => {
        // Production contract: terminate owns the budget and must not leave
        // pending mutation work that fires after it returns.
        const slice = Math.max(1, Math.min(budget, 80))
        await new Promise(resolve => setTimeout(resolve, slice))
        // A cancelled late arm would mutate if it were left live.
        const late = setTimeout(() => {
          mutated = true
        }, 600)
        clearTimeout(late)
        return { kind: 'failed', detail: 'deadline-exhausted' }
      }
    })
    const elapsed = Date.now() - started
    assert.notEqual(outcome.kind, 'clear')
    await new Promise(resolve => setTimeout(resolve, 700))
    assert.equal(mutated, false)
    assert.ok(elapsed < 2_000, `elapsed ${elapsed}`)
  })

  it(
    'kills a real PowerShell root+descendant tree and leaves the delayed sentinel untouched',
    { timeout: 15_000 },
    async () => {
      if (process.platform !== 'win32') return
      const fs = await import('node:fs')
      const os = await import('node:os')
      const {
        identitiesStillPresent,
        killProcessTreeAndAwaitGone,
        TERMINATE_KILL_CONFIRM_MS,
        terminateKillReserveMs,
        terminateWindowsHolderExact
      } = await import('./windows-process-terminate')
      const { execFile } = await import('node:child_process')
      const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

      const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-force-tree-'))
      const sentinel = path.join(tmp, 'sentinel.txt')
      const writerPidPath = path.join(tmp, 'writer.pid')

      // Root spawns a descendant writer that records its PID then would write sentinel later.
      const longScript = `
$ErrorActionPreference = 'Stop'
$sentinel = ${JSON.stringify(sentinel)}
$writerPidPath = ${JSON.stringify(writerPidPath)}
$writer = Start-Process -FilePath ${JSON.stringify(ps)} -ArgumentList @(
  '-NoLogo','-NoProfile','-NonInteractive','-Command',
  ('Start-Sleep -Milliseconds 1500; Set-Content -LiteralPath ''' + $sentinel + ''' -Value LATE_MUTATION')
) -PassThru -WindowStyle Hidden
Set-Content -LiteralPath $writerPidPath -Value ([string]$writer.Id)
Start-Sleep -Seconds 20
Write-Output ('ROOT=' + $PID + ';CHILD=' + $writer.Id)
`.trim()

      let childPid: number | undefined
      let treeSnapshot: Array<{ pid: number; createdAt: number }> = []
      const budgetMs = 5_000
      const started = Date.now()
      const controller = new AbortController()

      const run = async (_script: string, timeoutMs?: number, signal?: AbortSignal) => {
        return await new Promise<{ stdout: string; stderr: string; code: number; pid?: number }>((resolve) => {
          let settled = false
          const finish = (result: { stdout: string; stderr: string; code: number; pid?: number }) => {
            if (settled) return
            settled = true
            resolve(result)
          }

          const child = execFile(
            ps,
            ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', longScript],
            { encoding: 'utf8', windowsHide: true, timeout: Math.max(1, timeoutMs ?? budgetMs) },
            () => undefined
          )
          childPid = child.pid
          const absoluteDeadline = started + budgetMs

          // Capture root immediately; poll briefly for writer pid file.
          void (async () => {
            if (typeof childPid === 'number') {
              treeSnapshot = [{ pid: childPid, createdAt: Date.now() / 1000 }]
            }
            const pollUntil = Date.now() + 600
            while (Date.now() < pollUntil) {
              try {
                if (fs.existsSync(writerPidPath)) {
                  const writerPid = Number(fs.readFileSync(writerPidPath, 'utf8').trim())
                  if (Number.isInteger(writerPid) && writerPid > 0) {
                    treeSnapshot.push({ pid: writerPid, createdAt: Date.now() / 1000 })
                    break
                  }
                }
              } catch {
                void 0
              }
              await new Promise(r => setTimeout(r, 40))
            }
            controller.abort()
          })()

          const onAbort = () => {
            void (async () => {
              if (typeof childPid !== 'number') {
                finish({ stdout: '', stderr: 'aborted', code: 1 })
                return
              }
              const killed = await killProcessTreeAndAwaitGone(childPid, {
                confirmMs: terminateKillReserveMs(budgetMs),
                preSnapshot: treeSnapshot,
                deadlineAt: absoluteDeadline
              })
              finish({
                stdout: '',
                stderr: killed.confirmed
                  ? 'aborted'
                  : `unconfirmed-tree-survivors:${killed.survivors.map(s => s.pid).join(',')}`,
                code: 1,
                pid: childPid
              })
            })()
          }
          if (signal?.aborted) onAbort()
          else signal?.addEventListener('abort', onAbort, { once: true })
        })
      }

      const result = await terminateWindowsHolderExact(holder({ pid: 1, createdAt: 1 }), {
        run,
        timeoutMs: budgetMs,
        waitMs: 100,
        signal: controller.signal,
        platform: 'win32'
      })
      const elapsed = Date.now() - started
      assert.equal(result.kind, 'failed')
      assert.notEqual(result.detail, 'unconfirmed-tree-survivors')
      assert.ok(elapsed <= budgetMs, `terminateWindowsHolderExact elapsed ${elapsed} must be <= ${budgetMs}`)
      assert.ok(typeof childPid === 'number' && childPid > 0)
      assert.ok(treeSnapshot.length >= 2, `expected root+writer snapshot, got ${JSON.stringify(treeSnapshot)}`)
      assert.ok(TERMINATE_KILL_CONFIRM_MS >= 1_000)

      assert.deepEqual(await identitiesStillPresent(treeSnapshot), [])

      // Delayed descendant write would have landed by now if the tree survived.
      await new Promise(resolve => setTimeout(resolve, 1_800))
      assert.deepEqual(await identitiesStillPresent(treeSnapshot), [])
      assert.equal(fs.existsSync(sentinel), false)

      try {
        fs.rmSync(tmp, { recursive: true, force: true })
      } catch {
        void 0
      }
    }
  )

  it(
    'uses the production job-object runner to prevent late descendant mutation',
    { timeout: 15_000 },
    async () => {
      if (process.platform !== 'win32') return

      const os = await import('node:os')
      const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-force-job-boundary-'))
      const sentinel = path.join(tmp, 'sentinel.txt')
      const rootPidPath = path.join(tmp, 'root.pid')
      const writerPidPath = path.join(tmp, 'writer.pid')
      const quotePowerShellLiteral = (value: string) => `'${value.replace(/'/g, "''")}'`
      const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
      const script = `
$ErrorActionPreference = 'Stop'
$sentinel = ${quotePowerShellLiteral(sentinel)}
$rootPidPath = ${quotePowerShellLiteral(rootPidPath)}
$writerPidPath = ${quotePowerShellLiteral(writerPidPath)}
Set-Content -LiteralPath $rootPidPath -Value ([string]$PID)
$writer = Start-Process -FilePath ${quotePowerShellLiteral(ps)} -ArgumentList @(
  '-NoLogo','-NoProfile','-NonInteractive','-Command',
  ('Start-Sleep -Milliseconds 1500; Set-Content -LiteralPath ''' + $sentinel + ''' -Value LATE_MUTATION')
) -PassThru -WindowStyle Hidden
Set-Content -LiteralPath $writerPidPath -Value ([string]$writer.Id)
Start-Sleep -Seconds 20
`.trim()

      const controller = new AbortController()
      const started = Date.now()
      const runPromise = runPowerShellWithHardBoundary(script, 5_000, controller.signal)
      const waitForFile = async (filePath: string, timeoutMs: number) => {
        const deadline = Date.now() + timeoutMs
        while (Date.now() < deadline) {
          if (fs.existsSync(filePath)) return true
          await new Promise(resolve => setTimeout(resolve, 25))
        }
        return fs.existsSync(filePath)
      }

      try {
        const rootReady = await waitForFile(rootPidPath, 1_500)
        if (!rootReady) {
          const earlyResult = await runPromise
          throw new Error(`production root did not start: code=${earlyResult.code} stdout=${earlyResult.stdout} stderr=${earlyResult.stderr}`)
        }
        assert.equal(await waitForFile(writerPidPath, 1_500), true, 'production descendant did not start')
        const rootPid = Number(fs.readFileSync(rootPidPath, 'utf8').trim())
        const writerPid = Number(fs.readFileSync(writerPidPath, 'utf8').trim())
        assert.ok(Number.isInteger(rootPid) && rootPid > 0)
        assert.ok(Number.isInteger(writerPid) && writerPid > 0)

        // Abort only after the descendant is real, so the production runner's
        // tree snapshot and the delayed-write safety check cover both nodes.
        controller.abort()
        const result = await runPromise
        const elapsed = Date.now() - started
        assert.equal(result.code, 1)
        assert.doesNotMatch(result.stderr, /unconfirmed-tree-survivors/i)
        assert.ok(elapsed <= 5_000, `production runner elapsed ${elapsed}`)

        const { identitiesStillPresent } = await import('./windows-process-terminate')
        const identities = [
          { pid: rootPid },
          { pid: writerPid },
          ...(typeof result.pid === 'number' ? [{ pid: result.pid }] : [])
        ]
        assert.deepEqual(await identitiesStillPresent(identities), [])

        await new Promise(resolve => setTimeout(resolve, 1_800))
        assert.equal(fs.existsSync(sentinel), false)
        assert.deepEqual(await identitiesStillPresent(identities), [])
      } finally {
        controller.abort()
        await runPromise.catch(() => undefined)
        fs.rmSync(tmp, { recursive: true, force: true })
      }
    }
  )

  it(
    'multi-identity confirmation stays within budget and fails closed when probes cannot finish',
    { timeout: 8_000 },
    async () => {
      if (process.platform !== 'win32') return
      const { killProcessTreeAndAwaitGone } = await import('./windows-process-terminate')
      const identities = [
        { pid: 900001 },
        { pid: 900002 },
        { pid: 900003 },
        { pid: 900004 }
      ]
      const budgetMs = 250
      const started = Date.now()
      const result = await killProcessTreeAndAwaitGone(900001, {
        confirmMs: budgetMs,
        preSnapshot: identities,
        deadlineAt: started + budgetMs
      })
      const elapsed = Date.now() - started
      assert.ok(elapsed <= budgetMs + 150, `elapsed ${elapsed} exceeded budget ${budgetMs}`)
      // Phantom PIDs: either confirmed gone (fast ENOENT path) or unconfirmed if
      // budget exhausted mid-probe. Never invent a clear success after overrunning.
      assert.ok(result.confirmed === true || result.confirmed === false)
      if (elapsed > budgetMs) {
        assert.equal(result.confirmed, false)
      }
    }
  )

  it(
    'does not start a post-deadline root probe after snapshot timeout',
    { timeout: 5_000 },
    async () => {
      if (process.platform !== 'win32') return
      const { killProcessTreeAndAwaitGone } = await import('./windows-process-terminate')
      const budgetMs = 180
      const started = Date.now()
      let rootProbeCalls = 0
      const result = await killProcessTreeAndAwaitGone(900101, {
        confirmMs: budgetMs,
        deadlineAt: started + budgetMs,
        snapshotProcessTree: async () => {
          // Deliberately ignore the adapter timeout; the boundary must not
          // await this read or launch a fresh default-timeout fallback.
          await new Promise(resolve => setTimeout(resolve, budgetMs * 3))
          return []
        },
        readCreatedAt: async () => {
          rootProbeCalls += 1
          return 1
        }
      })
      const elapsed = Date.now() - started

      assert.equal(result.confirmed, false)
      assert.deepEqual(result.survivors.map(entry => entry.pid), [900101])
      assert.equal(rootProbeCalls, 0)
      assert.ok(elapsed <= budgetMs + 100, `snapshot boundary elapsed ${elapsed}ms > ${budgetMs}ms budget`)
    }
  )
})

describe('liveness probe classification', () => {
  it('classifies discriminated exit vs error outcomes without host PIDs', async () => {
    const {
      classifyLivenessProbeResult,
      probeProcessLiveness,
      identitiesStillPresent
    } = await import('./windows-process-terminate')

    // Authenticated exits only.
    assert.equal(classifyLivenessProbeResult({ kind: 'exit', code: 0 }), 'live')
    assert.equal(classifyLivenessProbeResult({ kind: 'exit', code: 3 }), 'absent')
    assert.equal(classifyLivenessProbeResult({ kind: 'exit', code: 1 }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'exit', code: 99 }), 'unknown')

    // Error metadata never proves absence — even numeric/string 3.
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: 3 }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: '3' }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: 'ETIMEDOUT' }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: 'EACCES' }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: 'EPERM' }), 'unknown')
    assert.equal(classifyLivenessProbeResult({ kind: 'error', code: 'ENOENT' }), 'unknown')
    assert.equal(
      classifyLivenessProbeResult({ kind: 'error', message: 'spawn powershell ENOENT' }),
      'unknown'
    )

    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'exit', code: 0 })),
      'live'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'exit', code: 3 })),
      'absent'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'error', code: 'ETIMEDOUT' })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'error', code: 'EACCES' })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'error', code: 'ENOENT' })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'error', code: 3 })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(7, 1_000, async () => ({ kind: 'error', code: '3' })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(7, 0, async () => ({ kind: 'exit', code: 0 })),
      'unknown'
    )
    assert.equal(
      await probeProcessLiveness(-1, 1_000, async () => ({ kind: 'exit', code: 0 })),
      'absent'
    )

    // identitiesStillPresent retains every unknown; drops only explicit exit-3 absent.
    // Force null create-time so the injectable liveness runner is exercised, not host PIDs.
    const noCreateTime = async () => null
    const unknownRunner = async () => ({ kind: 'error' as const, code: 'ETIMEDOUT' as const })
    const kept = await identitiesStillPresent([{ pid: 11 }, { pid: 12 }], {
      deadlineAt: Date.now() + 5_000,
      livenessRunner: unknownRunner,
      readCreatedAt: noCreateTime
    })
    assert.deepEqual(
      kept.map(entry => entry.pid),
      [11, 12]
    )

    const errorThreeRunner = async () => ({ kind: 'error' as const, code: 3 as const })
    const errorThreeKept = await identitiesStillPresent([{ pid: 31 }, { pid: 32 }], {
      deadlineAt: Date.now() + 5_000,
      livenessRunner: errorThreeRunner,
      readCreatedAt: noCreateTime
    })
    assert.deepEqual(
      errorThreeKept.map(entry => entry.pid),
      [31, 32]
    )

    const mixedRunner = async (pid: number) => {
      if (pid === 21) return { kind: 'exit' as const, code: 0 }
      if (pid === 22) return { kind: 'exit' as const, code: 3 }
      if (pid === 23) return { kind: 'error' as const, code: 'EPERM' }
      if (pid === 24) return { kind: 'error' as const, code: 3 }
      return { kind: 'error' as const, code: 'ENOENT' }
    }
    const mixed = await identitiesStillPresent(
      [{ pid: 21 }, { pid: 22 }, { pid: 23 }, { pid: 24 }, { pid: 25 }],
      {
        deadlineAt: Date.now() + 5_000,
        livenessRunner: mixedRunner,
        readCreatedAt: noCreateTime
      }
    )
    assert.deepEqual(
      mixed.map(entry => entry.pid).sort((a, b) => a - b),
      [21, 23, 24, 25]
    )
  })
})

describe('access-denied identity classification', () => {
  it('classifies access-denied identity failures for Administrator routing', () => {
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED', 5), {
      kind: 'access-denied',
      win32Error: 5
    })
    const script = buildExactTerminateScript(9, 100, 100)
    assert.match(script, /Access is denied|AccessDenied|denied/)
    assert.match(script, /ACCESS_DENIED/)
    // Get-Process/StartTime access denial must not collapse to ALREADY_GONE only.
    assert.match(script, /ALREADY_GONE/)
    assert.ok(script.includes("if ($msg -match 'Access"))
  })
})
