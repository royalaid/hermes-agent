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
 */

import { execFile } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder, ForceReleaseTerminateResult } from './windows-update-force-release'

const execFileAsync = promisify(execFile)

export type RunPowerShell = (script: string, timeoutMs?: number) => Promise<{ stdout: string; stderr: string; code: number }>

function powershellExecutable(): string {
  const windowsRoot = process.env.SystemRoot || 'C:\\Windows'
  return path.join(windowsRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

async function defaultRunPowerShell(script: string, timeoutMs = 4_000): Promise<{ stdout: string; stderr: string; code: number }> {
  try {
    const { stdout, stderr } = await execFileAsync(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', timeout: timeoutMs, windowsHide: true, maxBuffer: 1024 * 1024 }
    )
    return { stdout: String(stdout ?? ''), stderr: String(stderr ?? ''), code: 0 }
  } catch (error: any) {
    return {
      stdout: String(error?.stdout ?? ''),
      stderr: String(error?.stderr ?? error?.message ?? ''),
      code: typeof error?.code === 'number' ? error.code : 1
    }
  }
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
      if (err == 87 || err == 87) return -87;
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
} catch {
  Write-Output 'ALREADY_GONE'
  exit 0
}
$actualUnix = [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
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
if ($err -eq 87) { Write-Output 'ALREADY_GONE'; exit 0 }
Write-Output ("FAILED win32=" + $err)
exit 1
`.trim()
}

export function parseTerminateScriptOutput(stdout: string, code: number): ForceReleaseTerminateResult {
  const text = String(stdout || '').trim()
  if (/ALREADY_GONE/i.test(text) || code === 0 && /TERMINATED/i.test(text)) {
    if (/TERMINATED/i.test(text)) return { kind: 'terminated' }
    if (/ALREADY_GONE/i.test(text)) return { kind: 'already-gone' }
  }
  if (/CREATE_TIME_MISMATCH/i.test(text) || code === 3) {
    return { kind: 'create-time-mismatch' }
  }
  if (/ACCESS_DENIED/i.test(text) || code === 5) {
    return { kind: 'access-denied', win32Error: 5 }
  }
  if (/PROTECTED|ERROR_ACCESS_DENIED|win32=5/i.test(text)) {
    return { kind: 'access-denied', win32Error: 5 }
  }
  const win32 = text.match(/win32=(\d+)/i)
  if (win32) {
    const err = Number(win32[1])
    if (err === 5) return { kind: 'access-denied', win32Error: 5 }
    // Protected process / critical system process class
    if (err === 87 || err === 6) return { kind: 'already-gone' }
    return { kind: 'failed', detail: text || `win32=${err}` }
  }
  if (code === 0 && /TERMINATED/i.test(text)) return { kind: 'terminated' }
  return { kind: 'failed', detail: text || `exit ${code}` }
}

export async function terminateWindowsHolderExact(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunPowerShell,
    waitMs = 1_500
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    waitMs?: number
  } = {}
): Promise<ForceReleaseTerminateResult> {
  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }
  if (!Number.isInteger(target.pid) || target.pid <= 0) {
    return { kind: 'failed', detail: 'invalid pid' }
  }
  if (!Number.isFinite(target.createdAt) || target.createdAt <= 0) {
    return { kind: 'failed', detail: 'invalid createdAt' }
  }

  const script = buildExactTerminateScript(target.pid, target.createdAt, waitMs)
  const result = await run(script, Math.max(2_000, waitMs + 1_000))
  return parseTerminateScriptOutput(result.stdout + '\n' + result.stderr, result.code)
}
