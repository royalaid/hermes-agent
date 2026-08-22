import assert from 'node:assert/strict'
import { type ChildProcess, execFile, spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { describe, it, vi } from 'vitest'

import {
  type DirectBoundaryRequest,
  parseTerminateScriptOutput,
  terminateWindowsHolderExact,
  terminateWindowsHolderWithinDeadline
} from './windows-process-terminate'
import {
  buildRestartManagerScript,
  listRestartManagerHoldersForResources,
  parseRestartManagerOutput,
  RESTART_MANAGER_ROW_SPLIT_EXPRESSION
} from './windows-restart-manager'
import {
  type ForceReleaseHolder,
  type ForceReleaseTerminateResult,
  mergeInstallHolders,
  orderHoldersLeafFirst,
  raceWithBudget,
  runWindowsUpdateForceRelease,
  type WindowsUpdateForceReleaseDeps
} from './windows-update-force-release'

const execFileAsync = promisify(execFile)

const holder = (overrides: Partial<ForceReleaseHolder> = {}): ForceReleaseHolder => ({
  pid: 57_012,
  createdAt: 1_700_000_000,
  creationFileTime: '133456736000000000',
  name: 'hermes.exe',
  cmdline: 'redacted',
  source: 'restart-manager',
  resource: String.raw`C:\Hermes\venv\Scripts\hermes.exe`,
  resources: [String.raw`C:\Hermes\venv\Scripts\hermes.exe`],
  role: 'other',
  ...overrides
})

function makeDeps(overrides: Partial<WindowsUpdateForceReleaseDeps> = {}): {
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
      calls.push('wait:' + ms)
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
      calls.push('terminate:' + target.pid)

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

describe('holder discovery and ordering', () => {
  it('orders exact holders leaf-first', () => {
    const wrapper = holder({ pid: 10, role: 'wrapper' })
    const worker = holder({ pid: 11, wrapperPid: 10, role: 'worker' })
    const child = holder({ pid: 12, parentPid: 11, role: 'worker' })

    assert.deepEqual(
      orderHoldersLeafFirst([wrapper, worker, child]).map(entry => entry.pid),
      [12, 11, 10]
    )
  })

  it('uses scanner evidence only to enrich an exact RM generation', () => {
    const scanned = holder({
      source: 'scanner',
      creationFileTime: undefined,
      resource: String.raw`C:\Hermes\venv\Scripts\hermes.exe`,
      resources: undefined
    })

    const fromRm = holder({
      source: 'restart-manager',
      creationFileTime: '133456736000000123',
      resource: String.raw`C:\Hermes\venv\Lib\site-packages\one.pyd`,
      resources: [String.raw`C:\Hermes\venv\Lib\site-packages\one.pyd`]
    })

    const merged = mergeInstallHolders([scanned, fromRm])

    assert.equal(merged.length, 1)
    assert.equal(merged[0]?.creationFileTime, '133456736000000123')
    assert.equal(merged[0]?.source, 'restart-manager')
    assert.deepEqual(merged[0]?.resources, [String.raw`C:\Hermes\venv\Lib\site-packages\one.pyd`])
    assert.doesNotMatch(String(merged[0]?.resource), /;\s/)
  })

  it('never promotes a scanner-only PID or resource into termination authority', () => {
    const scanned = holder({
      source: 'scanner',
      creationFileTime: undefined,
      resources: [String.raw`C:\Hermes\venv\Scripts\fabricated.exe`]
    })

    assert.deepEqual(mergeInstallHolders([scanned]), [])
  })

  it('does not merge the same PID with different exact generations', () => {
    const first = holder({ pid: 77, creationFileTime: '133456736000000001' })
    const reused = holder({ pid: 77, creationFileTime: '133456736000000002' })

    assert.equal(mergeInstallHolders([first, reused]).length, 2)
  })

  it('does not use scanner-only parent metadata as leaf-first authority', () => {
    const parent = holder({ pid: 100, creationFileTime: '133456736000000100' })
    const child = holder({ pid: 101, creationFileTime: '133456736000000200' })

    const scannerHint = holder({
      pid: 101,
      source: 'scanner',
      creationFileTime: undefined,
      parentPid: 100
    })

    assert.deepEqual(
      orderHoldersLeafFirst(mergeInstallHolders([parent, child, scannerHint])).map(entry => entry.pid),
      [100, 101]
    )
  })

  it('orders by a fresh RM parent edge but rejects a parent generation newer than its child', () => {
    const parent = holder({ pid: 200, creationFileTime: '133456736000000100' })

    const child = holder({
      pid: 201,
      parentPid: 200,
      creationFileTime: '133456736000000200'
    })

    assert.deepEqual(
      orderHoldersLeafFirst(mergeInstallHolders([parent, child])).map(entry => entry.pid),
      [201, 200]
    )

    const reusedParent = holder({ pid: 300, creationFileTime: '133456736000000300' })

    const olderChild = holder({
      pid: 301,
      parentPid: 300,
      creationFileTime: '133456736000000200'
    })

    assert.deepEqual(
      orderHoldersLeafFirst(mergeInstallHolders([reusedParent, olderChild])).map(entry => entry.pid),
      [300, 301]
    )
  })
})

describe('ordinary force-release state machine', () => {
  it('returns clear without discovery when resources are already unlocked', async () => {
    const { calls, deps, setLocked } = makeDeps()
    setLocked(false)

    assert.deepEqual(await runWindowsUpdateForceRelease(deps), { kind: 'clear' })
    assert.deepEqual(calls, [])
  })

  it('terminates two exact holders leaf-first inside one shared deadline', async () => {
    const root = holder({ pid: 100, role: 'wrapper' })
    const leaf = holder({ pid: 101, parentPid: 100, role: 'worker' })
    const terminated: number[] = []
    let locked = true

    const { deps } = makeDeps({
      isResourceLocked: async () => locked,
      listRestartManagerHolders: async () => [root, leaf].filter(entry => !terminated.includes(entry.pid)),
      terminateHolder: async (target, budget, _signal, deadlineAt) => {
        assert.ok(budget > 0 && budget <= 5_000)
        assert.equal(deadlineAt, 5_000)
        terminated.push(target.pid)
        locked = terminated.length < 2

        return { kind: 'terminated' }
      }
    })

    assert.equal((await runWindowsUpdateForceRelease(deps)).kind, 'clear')
    assert.deepEqual(terminated, [101, 100])
  })

  it('keeps two leaf-first holders on one shrinking near-expiry deadline', async () => {
    const parent = holder({ pid: 200, creationFileTime: '133456736000000100' })

    const leaf = holder({
      pid: 201,
      parentPid: 200,
      creationFileTime: '133456736000000200'
    })

    let clock = 0
    let locked = true

    const calls: Array<{ pid: number; budget: number; deadlineAt?: number }> = []

    const { deps } = makeDeps({
      now: () => clock,
      deadlineMs: 400,
      isResourceLocked: async () => locked,
      listRestartManagerHolders: async () => [parent, leaf],
      terminateHolder: async (target, budget, _signal, deadlineAt) => {
        calls.push({ pid: target.pid, budget, deadlineAt })
        clock += 250
        locked = calls.length < 2

        return { kind: 'terminated' }
      }
    })

    assert.equal((await runWindowsUpdateForceRelease(deps)).kind, 'clear')
    assert.deepEqual(
      calls.map(call => call.pid),
      [201, 200]
    )
    assert.deepEqual(
      calls.map(call => call.deadlineAt),
      [400, 400]
    )
    assert.deepEqual(
      calls.map(call => call.budget),
      [400, 150]
    )
  })

  it('offers elevation only for the dedicated fully-authenticated permission result', async () => {
    const target = holder()

    const authenticated = makeDeps({
      listRestartManagerHolders: async () => [target],
      terminateHolder: async () => ({ kind: 'permission-required', win32Error: 5 })
    })

    const generic = makeDeps({
      listRestartManagerHolders: async () => [target],
      terminateHolder: async () => ({ kind: 'access-denied', win32Error: 5 })
    })

    assert.equal((await runWindowsUpdateForceRelease(authenticated.deps)).kind, 'needs-elevation')
    assert.equal((await runWindowsUpdateForceRelease(generic.deps)).kind, 'blocked')
  })

  it.each([
    ['stale generation', { kind: 'create-time-mismatch' } as ForceReleaseTerminateResult],
    ['scope mismatch', { kind: 'failed', detail: 'scope-mismatch' } as ForceReleaseTerminateResult],
    ['ownership absent', { kind: 'failed', detail: 'ownership-stale' } as ForceReleaseTerminateResult],
    ['ownership unreadable', { kind: 'failed', detail: 'ownership-unknown' } as ForceReleaseTerminateResult],
    ['malformed helper result', { kind: 'failed', detail: 'boundary-failed' } as ForceReleaseTerminateResult],
    ['deadline', { kind: 'failed', detail: 'deadline-exhausted' } as ForceReleaseTerminateResult]
  ])('keeps Administrator unavailable for %s', async (_label, result) => {
    const { deps } = makeDeps({
      listRestartManagerHolders: async () => [holder()],
      terminateHolder: async () => result
    })

    assert.notEqual((await runWindowsUpdateForceRelease(deps)).kind, 'needs-elevation')
  })

  it('does not expose paths, command lines, or raw holder names in user messages', async () => {
    const sensitive = holder({
      name: 'private-name',
      cmdline: 'private-command',
      resource: String.raw`C:\private\machine\path.exe`
    })

    const { deps } = makeDeps({
      listRestartManagerHolders: async () => [sensitive],
      terminateHolder: async () => ({ kind: 'failed', detail: 'ownership-stale' })
    })

    const outcome = await runWindowsUpdateForceRelease(deps)
    assert.notEqual(outcome.kind, 'clear')

    if (outcome.kind !== 'clear') {
      assert.doesNotMatch(outcome.message, /private-name|private-command|private\\machine/i)
      assert.ok(outcome.message.length < 512)
    }
  })
})

describe('Restart Manager exact identity contract', () => {
  it('parses only rows with an exact raw FILETIME', () => {
    const resources = [String.raw`C:\Hermes\venv\Scripts\hermes.exe`]

    const rows = parseRestartManagerOutput(
      JSON.stringify([
        {
          pid: 12,
          createdAt: 1_700_000_000.125,
          creationFileTime: '133456736001250000',
          name: 'python.exe',
          resource: resources[0],
          resources
        },
        { pid: 13, createdAt: 1_700_000_000, name: 'missing-exact-filetime' }
      ]),
      resources
    )

    assert.equal(rows.length, 1)
    assert.equal(rows[0]?.creationFileTime, '133456736001250000')
    assert.deepEqual(rows[0]?.resources, resources)
  })

  it('emits raw RM FILETIME and literal delimiter parsing', () => {
    const script = buildRestartManagerScript([String.raw`C:\Hermes\venv\Scripts\hermes.exe`])

    assert.match(script, /creationFileTime/)
    assert.match(script, /fileTime\.ToString/)
    assert.match(script, /\$part\.Split\(\[char\]'\|', 5\)/)
    assert.equal(RESTART_MANAGER_ROW_SPLIT_EXPRESSION, "$part.Split([char]'|', 5)")
  })

  it.each([
    ['native failure', { stdout: '', stderr: 'private failure detail', code: 1 }],
    ['missing response', { stdout: '', stderr: '', code: 0 }],
    ['malformed response', { stdout: '{not-json', stderr: '', code: 0 }],
    ['invalid identity rows', { stdout: '[{"pid":12}]', stderr: '', code: 0 }]
  ])('fails closed for %s without returning raw probe detail', async (_label, response) => {
    const resource = String.raw`C:\Hermes\venv\locked.pyd`
    await assert.rejects(
      listRestartManagerHoldersForResources([resource], {
        platform: 'win32',
        run: async () => response
      }),
      error =>
        error instanceof Error &&
        error.message === 'restart-manager-probe-failed' &&
        !error.message.includes('private failure detail')
    )
  })
})

describe('direct native boundary protocol', () => {
  it.each([
    ['RESULT:TERMINATED', 0, { kind: 'terminated' }],
    ['RESULT:ALREADY_GONE', 0, { kind: 'already-gone' }],
    ['RESULT:GENERATION_MISMATCH', 3, { kind: 'create-time-mismatch' }],
    ['RESULT:PERMISSION_REQUIRED', 5, { kind: 'permission-required', win32Error: 5 }],
    ['RESULT:IMAGE_OUT_OF_SCOPE', 1, { kind: 'failed', detail: 'scope-mismatch' }],
    ['RESULT:RESOURCE_OUT_OF_SCOPE', 1, { kind: 'failed', detail: 'scope-mismatch' }],
    ['RESULT:OWNERSHIP_STALE', 1, { kind: 'failed', detail: 'ownership-stale' }],
    ['RESULT:OWNERSHIP_UNKNOWN', 1, { kind: 'failed', detail: 'ownership-unknown' }],
    ['RESULT:TIMEOUT', 1, { kind: 'failed', detail: 'deadline-exhausted' }]
  ])('maps fixed result %s without raw diagnostics', (stdout, code, expected) => {
    assert.deepEqual(parseTerminateScriptOutput(stdout, code), expected)
  })

  it('rejects malformed, duplicated, and raw-error output', () => {
    assert.deepEqual(parseTerminateScriptOutput('RESULT:TERMINATED\nRESULT:TERMINATED', 0), {
      kind: 'failed',
      detail: 'boundary-failed'
    })
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED path=C:\\private', 5), {
      kind: 'failed',
      detail: 'boundary-failed'
    })
  })

  it('passes only exact bounded claims to the injected native runner', async () => {
    let captured: DirectBoundaryRequest | undefined

    const target = holder({
      pid: 55,
      creationFileTime: '133456736000000777',
      resource: String.raw`C:\Hermes\venv\one.exe`,
      resources: [String.raw`C:\Hermes\venv\one.exe`, String.raw`C:\Hermes\venv\two.pyd`]
    })

    const result = await terminateWindowsHolderExact(target, {
      platform: 'win32',
      installRoot: String.raw`C:\Hermes`,
      timeoutMs: 2_000,
      run: async request => {
        captured = request

        return { stdout: 'RESULT:TERMINATED\n', stderr: 'ignored-private-text', code: 0 }
      }
    })

    assert.deepEqual(result, { kind: 'terminated' })
    assert.equal(captured?.pid, 55)
    assert.equal(captured?.creationFileTime, '133456736000000777')
    assert.deepEqual(captured?.resources, [String.raw`C:\Hermes\venv\one.exe`, String.raw`C:\Hermes\venv\two.pyd`])
  })

  it.each([
    ['missing exact FILETIME', holder({ creationFileTime: undefined })],
    ['missing resource', holder({ resource: undefined, resources: [] })],
    ['invalid PID', holder({ pid: 0 })],
    ['oversized holder set', holder({ resources: Array.from({ length: 33 }, (_, index) => 'C:\\Hermes\\' + index) })]
  ])('fails closed before spawning for %s', async (_label, target) => {
    const run = vi.fn()

    const result = await terminateWindowsHolderExact(target, {
      platform: 'win32',
      installRoot: String.raw`C:\Hermes`,
      timeoutMs: 2_000,
      run
    })

    assert.equal(result.kind, 'failed')
    assert.equal(run.mock.calls.length, 0)
  })

  it('uses the caller shared absolute deadline', async () => {
    const requests: DirectBoundaryRequest[] = []
    const deadlineAt = Date.now() + 2_000

    const result = await terminateWindowsHolderWithinDeadline(holder(), {
      platform: 'win32',
      budgetMs: 3_000,
      deadlineAt,
      installRoot: String.raw`C:\Hermes`,
      run: async request => {
        requests.push(request)

        return { stdout: 'RESULT:OWNERSHIP_STALE\n', stderr: '', code: 1 }
      }
    })

    assert.deepEqual(result, { kind: 'failed', detail: 'ownership-stale' })
    assert.equal(requests.length, 1)
    assert.ok(requests[0]!.deadlineAt <= deadlineAt)
  })

  it(
    'aborts and drains a stalled direct runner at the shared deadline with no delayed work',
    { timeout: 5_000 },
    async () => {
      let delayed = false
      const started = Date.now()

      const result = await terminateWindowsHolderExact(holder(), {
        platform: 'win32',
        installRoot: String.raw`C:\Hermes`,
        timeoutMs: 900,
        run: request =>
          new Promise(resolve => {
            const delayedTimer = setTimeout(() => {
              delayed = true
            }, 1_200)

            request.signal?.addEventListener(
              'abort',
              () => {
                clearTimeout(delayedTimer)
                resolve({ stdout: '', stderr: '', code: 1 })
              },
              { once: true }
            )
          })
      })

      assert.deepEqual(result, { kind: 'failed', detail: 'deadline-exhausted' })
      assert.ok(Date.now() - started < 1_500)
      await new Promise(resolve => setTimeout(resolve, 400))
      assert.equal(delayed, false)
    }
  )
})

describe.runIf(process.platform === 'win32')('real exact Windows native boundary', () => {
  const holderSource = String.raw`
using System;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Runtime.InteropServices;
using System.Threading;

public static class DisposableHolder {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern int RegisterApplicationRestart(string commandLine, int flags);

    public static int Main(string[] args) {
        RegisterApplicationRestart(null, 0);
        string resource = args[0];
        string ready = args[1];
        string released = args[2];
        string delayed = args[3];
        int releaseAfterMs = Int32.Parse(args[4]);
        int lifetimeMs = Int32.Parse(args[5]);
        DateTime started = DateTime.UtcNow;
        using (var stream = new FileStream(resource, FileMode.Open, FileAccess.ReadWrite, FileShare.Read))
        using (var mapping = MemoryMappedFile.CreateFromFile(
            stream,
            null,
            0,
            MemoryMappedFileAccess.ReadWrite,
            HandleInheritability.None,
            true
        )) {
            File.WriteAllText(ready, "ready");
            if (releaseAfterMs >= 0) {
                Thread.Sleep(releaseAfterMs);
                mapping.Dispose();
                stream.Dispose();
                File.WriteAllText(released, "released");
            } else {
                Thread.Sleep(lifetimeMs);
            }
        }
        int remaining = lifetimeMs - (int)(DateTime.UtcNow - started).TotalMilliseconds;
        if (remaining > 0) Thread.Sleep(remaining);
        File.WriteAllText(delayed, "late");
        return 0;
    }
}
`.trim()

  async function buildFixture(root: string): Promise<string> {
    const executable = path.join(root, 'holder.exe')

    const script = String.raw`
$ErrorActionPreference = 'Stop'
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:HERMES_TEST_HOLDER_SOURCE))
Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $env:HERMES_TEST_HOLDER_EXE -OutputType ConsoleApplication
`.trim()

    const encoded = Buffer.from(script, 'utf16le').toString('base64')
    await execFileAsync(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      {
        encoding: 'utf8',
        windowsHide: true,
        timeout: 20_000,
        env: {
          ...process.env,
          HERMES_TEST_HOLDER_EXE: executable,
          HERMES_TEST_HOLDER_SOURCE: Buffer.from(holderSource, 'utf8').toString('base64')
        }
      }
    )

    return executable
  }

  function powershellExecutable(): string {
    return path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  }

  function waitForFile(file: string, child: ChildProcess, timeoutMs = 5_000): Promise<void> {
    const deadline = Date.now() + timeoutMs

    return new Promise((resolve, reject) => {
      const poll = () => {
        if (fs.existsSync(file)) {
          return resolve()
        }

        if (child.exitCode != null) {
          return reject(new Error('fixture exited before readiness'))
        }

        if (Date.now() >= deadline) {
          return reject(new Error('fixture readiness timed out'))
        }

        setTimeout(poll, 20)
      }

      poll()
    })
  }

  async function exactHolder(resource: string, pid: number): Promise<ForceReleaseHolder> {
    const deadline = Date.now() + 5_000
    let diagnostic = ''

    while (Date.now() < deadline) {
      const holders = await listRestartManagerHoldersForResources([resource], {
        timeoutMs: 2_000,
        run: async (script, timeoutMs) => {
          try {
            const result = await execFileAsync(
              powershellExecutable(),
              ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
              { encoding: 'utf8', windowsHide: true, timeout: timeoutMs }
            )

            diagnostic = (String(result.stderr ?? '') + ' stdout=' + String(result.stdout ?? '')).slice(0, 512)

            return { stdout: String(result.stdout ?? ''), stderr: diagnostic, code: 0 }
          } catch (error: any) {
            diagnostic = (
              String(error?.stderr ?? '') +
              ' stdout=' +
              String(error?.stdout ?? '') +
              ' message=' +
              String(error?.message ?? 'restart-manager probe failed')
            ).slice(0, 512)

            return { stdout: String(error?.stdout ?? ''), stderr: diagnostic, code: 1 }
          }
        }
      })

      const match = holders.find(entry => entry.pid === pid)

      if (match) {
        return match
      }

      await new Promise(resolve => setTimeout(resolve, 50))
    }

    throw new Error('Restart Manager did not report the fixture holder: ' + diagnostic)
  }

  async function stop(child: ChildProcess): Promise<void> {
    if (child.exitCode != null) {
      return
    }

    const closed = new Promise<void>(resolve => child.once('close', () => resolve()))

    try {
      child.kill()
    } catch {
      void 0
    }

    await Promise.race([closed, new Promise(resolve => setTimeout(resolve, 2_000))])
  }

  it('terminates only the exact current in-root holder and has no delayed writer', { timeout: 25_000 }, async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-direct-boundary-'))
    const resource = path.join(root, 'locked.bin')
    const ready = path.join(root, 'ready')
    const released = path.join(root, 'released')
    const delayed = path.join(root, 'delayed')
    fs.writeFileSync(resource, 'fixture')
    let target: ChildProcess | undefined
    let unrelated: ChildProcess | undefined

    try {
      const executable = await buildFixture(root)
      target = spawn(executable, [resource, ready, released, delayed, '-1', '12000'], {
        detached: false,
        windowsHide: true,
        stdio: 'ignore'
      })
      unrelated = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
        detached: false,
        windowsHide: true,
        stdio: 'ignore'
      })
      await waitForFile(ready, target)
      const exact = await exactHolder(resource, target.pid!)

      const result = await terminateWindowsHolderExact(exact, {
        platform: 'win32',
        installRoot: root,
        timeoutMs: 5_000
      })

      assert.deepEqual(result, { kind: 'terminated' })

      if (target.exitCode == null) {
        await new Promise<void>(resolve => target!.once('close', () => resolve()))
      }

      assert.equal(unrelated.exitCode, null, 'unrelated process must survive')
      await new Promise(resolve => setTimeout(resolve, 7_500))
      assert.equal(fs.existsSync(delayed), false, 'terminated target cannot write after return')
      assert.equal(
        fs.readdirSync(root).some(name => name.startsWith('hermes-terminate-watcher-')),
        false
      )
    } finally {
      if (target) {
        await stop(target)
      }

      if (unrelated) {
        await stop(unrelated)
      }

      fs.rmSync(root, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 })
    }
  })

  it(
    'refuses PID reuse and out-of-root scope, and proves an exited generation absent',
    { timeout: 30_000 },
    async () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-direct-refusal-'))
      const installRoot = path.join(root, 'install')
      const binRoot = path.join(root, 'bin')
      fs.mkdirSync(installRoot)
      fs.mkdirSync(binRoot)
      const executable = await buildFixture(binRoot)

      const runCase = async (name: string, releaseAfterMs: number) => {
        const resource = path.join(installRoot, name + '-locked.bin')
        const ready = path.join(root, name + '-ready')
        const released = path.join(root, name + '-released')
        const delayed = path.join(root, name + '-delayed')
        fs.writeFileSync(resource, 'fixture')

        const child = spawn(executable, [resource, ready, released, delayed, String(releaseAfterMs), '10000'], {
          detached: false,
          windowsHide: true,
          stdio: 'ignore'
        })

        await waitForFile(ready, child)

        return { child, exact: await exactHolder(resource, child.pid!), released }
      }

      const generation = await runCase('generation', -1)
      const scope = await runCase('scope', -1)
      const gone = await runCase('gone', -1)
      const released = await runCase('released', 500)

      try {
        const mismatch = await terminateWindowsHolderExact(
          {
            ...generation.exact,
            creationFileTime: (BigInt(generation.exact.creationFileTime!) + 1n).toString()
          },
          { platform: 'win32', installRoot: root, timeoutMs: 5_000 }
        )

        assert.deepEqual(mismatch, { kind: 'create-time-mismatch' })
        assert.equal(generation.child.exitCode, null)

        await stop(gone.child)

        const absent = await terminateWindowsHolderExact(gone.exact, {
          platform: 'win32',
          installRoot: root,
          timeoutMs: 5_000
        })

        assert.deepEqual(absent, { kind: 'already-gone' })

        const outOfScope = await terminateWindowsHolderExact(scope.exact, {
          platform: 'win32',
          installRoot,
          timeoutMs: 5_000
        })

        assert.deepEqual(outOfScope, { kind: 'failed', detail: 'scope-mismatch' })
        assert.equal(scope.child.exitCode, null)

        await waitForFile(released.released, released.child)

        const ownershipStale = await terminateWindowsHolderExact(released.exact, {
          platform: 'win32',
          installRoot: root,
          timeoutMs: 5_000
        })

        assert.deepEqual(ownershipStale, { kind: 'failed', detail: 'ownership-stale' })
        assert.equal(released.child.exitCode, null)
      } finally {
        await stop(generation.child)
        await stop(scope.child)
        await stop(gone.child)
        await stop(released.child)
        fs.rmSync(root, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 })
      }
    }
  )
})

describe('budget helper', () => {
  it('returns fallback for non-mutating discovery that exceeds its budget', async () => {
    const started = Date.now()

    const value = await raceWithBudget(
      new Promise<string>(resolve => setTimeout(() => resolve('late'), 1_000)),
      100,
      () => 'fallback'
    )

    assert.equal(value, 'fallback')
    assert.ok(Date.now() - started < 500)
  })

  it('aborts and awaits terminal discovery cleanup before returning its fallback', async () => {
    let reaped = false

    const value = await raceWithBudget(
      signal =>
        new Promise<string>(resolve => {
          signal.addEventListener(
            'abort',
            () => {
              setTimeout(() => {
                reaped = true
                resolve('terminal')
              }, 25)
            },
            { once: true }
          )
        }),
      100,
      () => 'fallback',
      { drainMs: 50 }
    )

    assert.equal(value, 'fallback')
    assert.equal(reaped, true)
  })
})
