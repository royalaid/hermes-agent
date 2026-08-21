/**
 * SuperF4-style exact process termination for Windows update force-release.
 *
 * Behavioral reference (not copied): stefansundin/superf4 commit
 * 6b677d422553e6b908b9eeaff4333b8b457e7bef, superf4.c lines 236-289.
 * SuperF4 is GPL-3.0 — do not copy its implementation. This module follows
 * Microsoft Win32 documentation for:
 *   OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE)
 *   TerminateProcess
 *   WaitForSingleObject
 *   CloseHandle
 * and optionally AdjustTokenPrivileges(SeDebugPrivilege) when present.
 *
 * The PowerShell child that performs TerminateProcess is itself bounded by a
 * hard wall-clock budget. On expiry the child tree is killed and the call
 * returns only after the child PID is confirmed gone (or a Windows Job Object
 * KILL_ON_JOB_CLOSE terminal is applied). No late mutation after return.
 */

import { execFile, type ChildProcess } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder, ForceReleaseTerminateResult } from './windows-update-force-release'

const execFileAsync = promisify(execFile)

export type RunPowerShell = (
  script: string,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<{ stdout: string; stderr: string; code: number; pid?: number }>

/** Shared kill/identity-confirm reserve used by the terminate boundary. */
export const TERMINATE_KILL_CONFIRM_MS = 1_500
export const TERMINATE_KILL_CONFIRM_MIN_MS = 400
export const TERMINATE_KILL_CONFIRM_RATIO = 0.3

export function terminateKillReserveMs(budgetMs: number): number {
  const budget = Math.max(1, Math.trunc(budgetMs))
  return Math.min(
    TERMINATE_KILL_CONFIRM_MS,
    Math.max(TERMINATE_KILL_CONFIRM_MIN_MS, Math.floor(budget * TERMINATE_KILL_CONFIRM_RATIO))
  )
}

function powershellExecutable(): string {
  const windowsRoot = process.env.SystemRoot || 'C:\\Windows'
  return path.join(windowsRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

export type ProcessIdentity = { pid: number; createdAt?: number }

async function readProcessCreatedAt(
  pid: number,
  timeoutMs = 2_000
): Promise<number | null> {
  if (!Number.isInteger(pid) || pid <= 0) return null
  const budget = Math.trunc(timeoutMs)
  if (budget <= 0) return null
  if (process.platform !== 'win32') return null
  try {
    const { stdout } = await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `$p = Get-Process -Id ${Math.trunc(pid)} -ErrorAction Stop; [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()`
      ],
      { encoding: 'utf8', windowsHide: true, timeout: budget }
    )
    const value = Number(String(stdout).trim())
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

export type ProcessLiveness = 'live' | 'absent' | 'unknown'

export async function probeProcessLiveness(
  pid: number,
  timeoutMs = 2_000
): Promise<ProcessLiveness> {
  if (!Number.isInteger(pid) || pid <= 0) return 'absent'
  const budget = Math.trunc(timeoutMs)
  if (budget <= 0) return 'unknown'
  if (process.platform !== 'win32') {
    try {
      process.kill(pid, 0)
      return 'live'
    } catch (error: any) {
      if (error?.code === 'ESRCH') return 'absent'
      return 'unknown'
    }
  }
  try {
    await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        // Exit 0 = live, 3 = explicitly not found, anything else = unknown.
        `$p = Get-Process -Id ${Math.trunc(pid)} -ErrorAction SilentlyContinue; if ($null -ne $p) { exit 0 } else { exit 3 }`
      ],
      { windowsHide: true, timeout: budget }
    )
    return 'live'
  } catch (error: any) {
    const code = typeof error?.code === 'number' ? error.code : null
    if (code === 3) return 'absent'
    // timeout / access / spawn / killed probe => unknown, never absent
    return 'unknown'
  }
}

/** @deprecated Prefer probeProcessLiveness; true only when explicitly live. */
export async function processIsLive(pid: number, timeoutMs = 2_000): Promise<boolean> {
  return (await probeProcessLiveness(pid, timeoutMs)) === 'live'
}

export async function identitiesStillPresent(
  identities: readonly ProcessIdentity[],
  { deadlineAt }: { deadlineAt?: number } = {}
): Promise<ProcessIdentity[]> {
  const survivors: ProcessIdentity[] = []
  const remaining = () =>
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? Math.max(0, deadlineAt - Date.now())
      : 2_000

  for (const identity of identities) {
    const left = remaining()
    if (left <= 0) {
      // No time for an absence probe: keep known/unknown identities as survivors.
      survivors.push(identity)
      continue
    }
    const slice = Math.max(1, Math.floor(left / Math.max(1, identities.length - survivors.length)))
    const createdAt = await readProcessCreatedAt(identity.pid, slice)
    if (createdAt == null) {
      // Do not infer absence from a null create-time read. Probe liveness with
      // remaining budget; only explicit not-found proves absence.
      const liveLeft = remaining()
      if (liveLeft <= 0) {
        survivors.push(identity)
        continue
      }
      const liveness = await probeProcessLiveness(identity.pid, liveLeft)
      if (liveness === 'absent') {
        continue
      }
      // live or unknown => survivor
      survivors.push(identity)
      continue
    }
    // Unknown generation: any proven-live or unknown PID is a survivor.
    if (identity.createdAt == null || !Number.isFinite(identity.createdAt)) {
      survivors.push({ pid: identity.pid, createdAt })
      continue
    }
    if (Math.abs(createdAt - identity.createdAt) <= 1.5) {
      survivors.push(identity)
    }
  }
  return survivors
}

/**
 * Snapshot a Windows process tree (root + descendants) as PID + create-time
 * identities so post-kill verification can detect PID reuse.
 */
export async function snapshotProcessTreeIdentities(
  rootPid: number,
  { timeoutMs = 1_500 }: { timeoutMs?: number } = {}
): Promise<ProcessIdentity[]> {
  if (!Number.isInteger(rootPid) || rootPid <= 0) return []
  if (process.platform !== 'win32') {
    const createdAt = await readProcessCreatedAt(rootPid)
    return createdAt == null ? [] : [{ pid: rootPid, createdAt }]
  }

  // Never force a floor larger than the caller-supplied remaining budget.
  if (timeoutMs <= 0) {
    // Zero budget: do not probe. Return unknown-generation root identity only.
    return [{ pid: rootPid }]
  }
  const budget = Math.max(1, Math.trunc(timeoutMs))
  try {
    const { stdout } = await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `
$ErrorActionPreference = 'Stop'
$root = ${Math.trunc(rootPid)}
$ids = New-Object 'System.Collections.Generic.List[int]'
function Add-Tree([int]$pidVal) {
  if ($pidVal -le 0 -or $ids.Contains($pidVal)) { return }
  $ids.Add($pidVal) | Out-Null
  Get-CimInstance Win32_Process -Filter ("ParentProcessId = $pidVal") -ErrorAction SilentlyContinue | ForEach-Object {
    Add-Tree ([int]$_.ProcessId)
  }
}
Add-Tree $root
$rows = @()
foreach ($id in $ids) {
  try {
    $p = Get-Process -Id $id -ErrorAction Stop
    $created = [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
    $rows += ("{0}|{1}" -f $id, $created)
  } catch {}
}
$rows -join ';'
`.trim()
      ],
      { encoding: 'utf8', windowsHide: true, timeout: budget }
    )
    const identities: ProcessIdentity[] = []
    for (const part of String(stdout || '')
      .trim()
      .split(';')
      .filter(Boolean)) {
      const [pidText, createdText] = part.split('|')
      const pid = Number(pidText)
      const createdAt = Number(createdText)
      if (Number.isInteger(pid) && pid > 0 && Number.isFinite(createdAt) && createdAt > 0) {
        identities.push({ pid, createdAt })
      }
    }
    return identities
  } catch {
    const createdAt = await readProcessCreatedAt(rootPid)
    return createdAt == null ? [{ pid: rootPid }] : [{ pid: rootPid, createdAt }]
  }
}

/**
 * Kill a Windows process tree and wait until every pre-captured identity is gone
 * or reused. Uses one absolute deadline shared by taskkill + identity polling.
 * Returns confirmed=false when any identity remains — callers must treat that as
 * a hard terminal-boundary failure, not a settled success.
 */
export async function killProcessTreeAndAwaitGone(
  pid: number,
  {
    confirmMs = TERMINATE_KILL_CONFIRM_MS,
    pollMs = 50,
    preSnapshot,
    deadlineAt
  }: {
    confirmMs?: number
    pollMs?: number
    preSnapshot?: readonly ProcessIdentity[]
    /** Absolute Date.now() deadline; overrides confirmMs window when provided. */
    deadlineAt?: number
  } = {}
): Promise<{ confirmed: boolean; identities: ProcessIdentity[]; survivors: ProcessIdentity[] }> {
  if (!Number.isInteger(pid) || pid <= 0) {
    return { confirmed: true, identities: [], survivors: [] }
  }

  const absoluteDeadline =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? deadlineAt
      : Date.now() + Math.max(0, Math.trunc(confirmMs))

  const remaining = () => Math.max(0, absoluteDeadline - Date.now())
  if (remaining() <= 0) {
    // No time for probes: fail closed with known/unknown identities as survivors.
    const identities =
      preSnapshot && preSnapshot.length > 0 ? [...preSnapshot] : [{ pid }]
    return { confirmed: false, identities, survivors: identities }
  }

  const identities =
    preSnapshot && preSnapshot.length > 0
      ? [...preSnapshot]
      : await snapshotProcessTreeIdentities(pid, { timeoutMs: Math.min(500, remaining()) })

  // Always include the root identity. Prefer a real create-time; if unavailable,
  // keep an unknown-generation identity (createdAt omitted) so liveness alone
  // keeps confirmation false while the PID lives.
  if (!identities.some(entry => entry.pid === pid)) {
    const left = remaining()
    const createdAt = left > 0 ? await readProcessCreatedAt(pid, left) : null
    identities.push(createdAt != null ? { pid, createdAt } : { pid })
  }

  const taskkillBudget = Math.min(remaining(), Math.max(0, Math.trunc(confirmMs)))
  if (taskkillBudget > 0) {
    try {
      await execFileAsync('taskkill', ['/PID', String(pid), '/T', '/F'], {
        windowsHide: true,
        timeout: taskkillBudget
      })
    } catch {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        void 0
      }
    }
  }

  // Also hard-kill every known identity in case /T missed a detached Start-Process child.
  for (const identity of identities) {
    const left = remaining()
    if (left <= 0) break
    if (identity.pid === pid) continue
    try {
      await execFileAsync('taskkill', ['/PID', String(identity.pid), '/T', '/F'], {
        windowsHide: true,
        timeout: Math.min(400, left)
      })
    } catch {
      try {
        process.kill(identity.pid, 'SIGKILL')
      } catch {
        void 0
      }
    }
  }

  if (remaining() <= 0) {
    return { confirmed: false, identities, survivors: identities }
  }

  let survivors = await identitiesStillPresent(identities, { deadlineAt: absoluteDeadline })
  while (survivors.length > 0 && remaining() > 0) {
    await new Promise(resolve => setTimeout(resolve, Math.max(1, Math.min(pollMs, remaining()))))
    if (remaining() <= 0) break
    survivors = await identitiesStillPresent(identities, { deadlineAt: absoluteDeadline })
  }

  if (remaining() <= 0 && survivors.length > 0) {
    return { confirmed: false, identities, survivors }
  }

  return {
    confirmed: survivors.length === 0,
    identities,
    survivors
  }
}

function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode != null || child.signalCode != null) {
    return Promise.resolve(true)
  }
  return new Promise(resolve => {
    let settled = false
    const done = (ok: boolean) => {
      if (settled) return
      settled = true
      resolve(ok)
    }
    const timer = setTimeout(() => done(false), Math.max(1, timeoutMs))
    child.once('exit', () => {
      clearTimeout(timer)
      done(true)
    })
    child.once('error', () => {
      clearTimeout(timer)
      done(false)
    })
  })
}

/**
 * Run PowerShell under a hard budget. On abort/timeout the child tree is killed
 * and the promise resolves only after every pre-captured tree identity is
 * confirmed gone. Confirmation failure is a hard boundary failure.
 */
async function defaultRunPowerShell(
  script: string,
  timeoutMs = 4_000,
  signal?: AbortSignal
): Promise<{ stdout: string; stderr: string; code: number; pid?: number }> {
  const budget = Math.max(1, Math.trunc(timeoutMs))
  if (signal?.aborted) {
    return { stdout: '', stderr: 'aborted', code: 1 }
  }

  // Reserve part of the budget for kill + confirmed absence of the whole tree.
  const startedAt = Date.now()
  const absoluteDeadline = startedAt + budget
  const killReserveMs = terminateKillReserveMs(budget)
  const runMs = Math.max(1, budget - killReserveMs)

  return await new Promise(resolve => {
    let settled = false
    let childPid: number | undefined
    let treeSnapshot: ProcessIdentity[] = []
    let killing = false

    const finish = async (result: { stdout: string; stderr: string; code: number; pid?: number }) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', onAbort)

      if (killing && typeof childPid === 'number') {
        const killResult = await killProcessTreeAndAwaitGone(childPid, {
          confirmMs: killReserveMs,
          preSnapshot: treeSnapshot,
          deadlineAt: absoluteDeadline
        })
        if (!killResult.confirmed) {
          resolve({
            stdout: result.stdout,
            stderr: `unconfirmed-tree-survivors:${killResult.survivors.map(s => s.pid).join(',')}`,
            code: 1,
            pid: childPid
          })
          return
        }
      }
      resolve({ ...result, pid: childPid })
    }

    const child = execFile(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      {
        encoding: 'utf8',
        timeout: runMs,
        windowsHide: true,
        maxBuffer: 1024 * 1024,
        killSignal: 'SIGTERM'
      },
      (error: any, stdout, stderr) => {
        void (async () => {
          if (!error) {
            await finish({ stdout: String(stdout ?? ''), stderr: String(stderr ?? ''), code: 0 })
            return
          }
          if (
            typeof childPid === 'number' &&
            (error?.killed || /ETIMEDOUT|timeout/i.test(String(error?.message ?? '')))
          ) {
            killing = true
            await finish({
              stdout: String(error?.stdout ?? stdout ?? ''),
              stderr: String(error?.stderr ?? stderr ?? error?.message ?? 'timeout'),
              code: typeof error?.code === 'number' ? error.code : 1
            })
            return
          }
          await finish({
            stdout: String(error?.stdout ?? stdout ?? ''),
            stderr: String(error?.stderr ?? stderr ?? error?.message ?? ''),
            code: typeof error?.code === 'number' ? error.code : 1
          })
        })()
      }
    )

    if (typeof child.pid === 'number' && child.pid > 0) {
      childPid = child.pid
      // Capture identities promptly after spawn with a bounded slice; abort must
      // not wait for a fresh snapshot before killing.
      void snapshotProcessTreeIdentities(childPid, { timeoutMs: Math.min(400, killReserveMs) }).then(snapshot => {
        if (!killing && snapshot.length > 0) {
          treeSnapshot = snapshot
        }
      })
    }

    const onAbort = () => {
      void (async () => {
        killing = true
        if (typeof childPid === 'number') {
          // Kill immediately with already-captured identities (+ root). Never
          // delay kill for a fresh unbounded snapshot, and never synthesize
          // create-time. Unknown generation keeps confirmation fail-safe.
          if (treeSnapshot.length === 0) {
            treeSnapshot = [{ pid: childPid }]
          }
          await finish({ stdout: '', stderr: 'aborted', code: 1, pid: childPid })
          return
        }
        try {
          child.kill('SIGKILL')
        } catch {
          void 0
        }
        await waitForChildExit(child, Math.max(1, absoluteDeadline - Date.now()))
        await finish({ stdout: '', stderr: 'aborted', code: 1, pid: childPid })
      })()
    }

    if (signal) {
      if (signal.aborted) {
        onAbort()
      } else {
        signal.addEventListener('abort', onAbort, { once: true })
      }
    }
  })
}

/**
 * Build a self-contained PowerShell script that terminates one PID only when
 * its create-time ticks still match the expected generation.
 */
export function buildExactTerminateScript(pid: number, createdAtUnixSeconds: number, waitMs = 1_500): string {
  // createdAt from psutil is epoch seconds (float). Compare at second resolution.
  const expected = Number(createdAtUnixSeconds)
  return `
$ErrorActionPreference = 'Stop'
$pidTarget = ${Math.trunc(pid)}
$expectedUnix = [double]${expected}
$waitMs = ${Math.max(0, Math.trunc(waitMs))}
try {
  Add-Type -TypeDefinition @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
public static class HermesForceReleaseNative {
  public const uint PROCESS_TERMINATE = 0x0001;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
  public const uint TOKEN_QUERY = 0x0008;
  public const uint SE_PRIVILEGE_ENABLED = 0x00000002;
  [StructLayout(LayoutKind.Sequential, Pack = 1)]
  public struct LUID { public uint LowPart; public int HighPart; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)]
  public struct LUID_AND_ATTRIBUTES { public LUID Luid; public uint Attributes; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)]
  public struct TOKEN_PRIVILEGES { public uint PrivilegeCount; public LUID_AND_ATTRIBUTES Privileges; }
  [DllImport("advapi32.dll", SetLastError = true)]
  public static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);
  [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  public static extern bool LookupPrivilegeValue(string lpSystemName, string lpName, out LUID lpLuid);
  [DllImport("advapi32.dll", SetLastError = true)]
  public static extern bool AdjustTokenPrivileges(IntPtr TokenHandle, bool DisableAllPrivileges, ref TOKEN_PRIVILEGES NewState, uint BufferLength, IntPtr PreviousState, IntPtr ReturnLength);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool CloseHandle(IntPtr hObject);
  [DllImport("kernel32.dll")]
  public static extern IntPtr GetCurrentProcess();
  public static void TryEnableDebugPrivilege() {
    IntPtr token;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, out token)) return;
    try {
      LUID luid;
      if (!LookupPrivilegeValue(null, "SeDebugPrivilege", out luid)) return;
      TOKEN_PRIVILEGES tp = new TOKEN_PRIVILEGES();
      tp.PrivilegeCount = 1;
      tp.Privileges.Luid = luid;
      tp.Privileges.Attributes = SE_PRIVILEGE_ENABLED;
      AdjustTokenPrivileges(token, false, ref tp, 0, IntPtr.Zero, IntPtr.Zero);
    } finally { CloseHandle(token); }
  }
  public static int TerminateExact(int pid, int waitMs) {
    TryEnableDebugPrivilege();
    IntPtr handle = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, false, pid);
    if (handle == IntPtr.Zero) {
      int err = Marshal.GetLastWin32Error();
      return err == 0 ? -1 : -err;
    }
    try {
      if (!TerminateProcess(handle, 1)) {
        int err = Marshal.GetLastWin32Error();
        return err == 0 ? -1 : -err;
      }
      WaitForSingleObject(handle, (uint)Math.Max(0, waitMs));
      return 0;
    } finally { CloseHandle(handle); }
  }
}
"@
} catch {}
try {
  $p = Get-Process -Id $pidTarget -ErrorAction Stop
  try {
    $actualUnix = [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
  } catch {
    $msg = [string]$_.Exception.Message
    if ($msg -match 'Access is denied|AccessDenied|denied') {
      Write-Output 'ACCESS_DENIED'
      exit 5
    }
    throw
  }
} catch {
  $msg = [string]$_.Exception.Message
  if ($msg -match 'Access is denied|AccessDenied|denied') {
    Write-Output 'ACCESS_DENIED'
    exit 5
  }
  Write-Output 'ALREADY_GONE'
  exit 0
}
if ([math]::Abs($actualUnix - $expectedUnix) -gt 1.5) {
  Write-Output ("CREATE_TIME_MISMATCH actual=" + $actualUnix + " expected=" + $expectedUnix)
  exit 3
}
$code = [HermesForceReleaseNative]::TerminateExact($pidTarget, $waitMs)
if ($code -eq 0) {
  Write-Output 'TERMINATED'
  exit 0
}
$err = -[int]$code
if ($err -eq 5) { Write-Output 'ACCESS_DENIED'; exit 5 }
# ERROR_INVALID_HANDLE / ERROR_INVALID_PARAMETER after Get-Process saw the target
# are terminal failures, not proof the holder is gone.
if ($err -eq 6 -or $err -eq 87) {
  Write-Output ("FAILED win32=" + $err)
  exit 1
}
# Protected-process / critical system process class surfaces as access denied
# variants; callers that already elevated should treat these as blocked.
if ($err -eq 5) { Write-Output 'PROTECTED win32=5'; exit 5 }
Write-Output ("FAILED win32=" + $err)
exit 1
`.trim()
}

export function parseTerminateScriptOutput(stdout: string, code: number): ForceReleaseTerminateResult {
  const text = String(stdout || '').trim()
  if (/PROTECTED/i.test(text)) {
    const win32 = text.match(/win32=(\d+)/i)
    return { kind: 'protected', win32Error: win32 ? Number(win32[1]) : 5 }
  }
  if (/ALREADY_GONE/i.test(text) || (code === 0 && /TERMINATED/i.test(text))) {
    if (/TERMINATED/i.test(text)) return { kind: 'terminated' }
    if (/ALREADY_GONE/i.test(text)) return { kind: 'already-gone' }
  }
  if (/CREATE_TIME_MISMATCH/i.test(text) || code === 3) {
    return { kind: 'create-time-mismatch' }
  }
  if (/ACCESS_DENIED/i.test(text) || code === 5) {
    return { kind: 'access-denied', win32Error: 5 }
  }
  const win32 = text.match(/win32=(\d+)/i)
  if (win32) {
    const err = Number(win32[1])
    if (err === 5) return { kind: 'access-denied', win32Error: 5 }
    // Do not treat 6/87 as already-gone: the process was observed live above.
    return { kind: 'failed', detail: text || `win32=${err}`, win32Error: err }
  }
  if (code === 0 && /TERMINATED/i.test(text)) return { kind: 'terminated' }
  return { kind: 'failed', detail: text || `exit ${code}` }
}

export async function terminateWindowsHolderExact(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunPowerShell,
    waitMs = 1_500,
    timeoutMs,
    signal
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    waitMs?: number
    /** Hard wall-clock budget for the PowerShell child including kill/confirm. */
    timeoutMs?: number
    /** When aborted, kill the child tree, await confirmed absence, then return. */
    signal?: AbortSignal
  } = {}
): Promise<ForceReleaseTerminateResult> {
  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }
  if (signal?.aborted) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }
  if (!Number.isInteger(target.pid) || target.pid <= 0) {
    return { kind: 'failed', detail: 'invalid pid' }
  }
  if (!Number.isFinite(target.createdAt) || target.createdAt <= 0) {
    return { kind: 'failed', detail: 'invalid createdAt' }
  }

  const budget = Math.max(1, Math.trunc(timeoutMs ?? Math.max(2_000, waitMs + 1_000)))
  // Keep TerminateProcess wait short enough that kill-reserve still fits.
  const killReserveMs = terminateKillReserveMs(budget)
  const runBudget = Math.max(1, budget - killReserveMs)
  const effectiveWait = Math.max(0, Math.min(Math.trunc(waitMs), Math.max(0, runBudget - 250)))
  if (budget <= 50) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }

  const script = buildExactTerminateScript(target.pid, target.createdAt, effectiveWait)
  const result = await run(script, budget, signal)
  if (signal?.aborted) {
    // Child tree must already be confirmed gone by run(); never claim mutation.
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }
  if (/unconfirmed-tree-survivors/i.test(result.stderr || '')) {
    return { kind: 'failed', detail: 'unconfirmed-tree-survivors' }
  }
  // Timed-out/killed child: do not parse a partial TerminateProcess success.
  if (/aborted|ETIMEDOUT|timeout/i.test(result.stderr || '') && !/TERMINATED|ACCESS_DENIED|PROTECTED|CREATE_TIME/i.test(result.stdout || '')) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }
  return parseTerminateScriptOutput(result.stdout + '\n' + result.stderr, result.code)
}
