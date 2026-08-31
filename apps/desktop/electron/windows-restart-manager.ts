/**
 * Windows Restart Manager lock-owner discovery for update force-release.
 *
 * Prefer RmGetList for exact locked resources over command-line heuristics.
 * Implemented via PowerShell + P/Invoke from Microsoft RM docs (not SuperF4).
 */

import { execFile } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder } from './windows-update-force-release'

const execFileAsync = promisify(execFile)

export type RunPowerShell = (
  script: string,
  timeoutMs?: number
) => Promise<{ stdout: string; stderr: string; code: number }>

function powershellExecutable(): string {
  const windowsRoot = process.env.SystemRoot || 'C:\\Windows'

  return path.join(windowsRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

async function defaultRunPowerShell(
  script: string,
  timeoutMs = 4_000
): Promise<{ stdout: string; stderr: string; code: number }> {
  const budget = Math.max(1, Math.trunc(timeoutMs))

  try {
    const { stdout, stderr } = await execFileAsync(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', timeout: budget, windowsHide: true, maxBuffer: 2 * 1024 * 1024 }
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

function escapePsSingleQuoted(value: string): string {
  return `'${String(value).replace(/'/g, "''")}'`
}

/**
 * Emitted PowerShell must split RM rows on a literal pipe.
 * Prefer String.Split over -split regex so TS template escaping cannot
 * accidentally emit a character-class / alternation pattern.
 */
export const RESTART_MANAGER_ROW_SPLIT_EXPRESSION = "$part.Split([char]'|', 4)"

export function buildRestartManagerScript(resources: readonly string[]): string {
  const list = resources.map(escapePsSingleQuoted).join(',')

  return `
$ErrorActionPreference = 'Stop'
$resources = @(${list})
if (-not $resources -or $resources.Count -eq 0) { Write-Output '[]'; exit 0 }
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class HermesRm {
  public const int CCH_RM_SESSION_KEY = 32;
  public const int CCH_RM_MAX_APP_NAME = 255;
  public const int CCH_RM_MAX_SVC_NAME = 63;
  public enum RM_APP_TYPE { RmUnknownApp=0, RmMainWindow=1, RmOtherWindow=2, RmService=3, RmExplorer=4, RmConsole=5, RmCritical=1000 }
  [StructLayout(LayoutKind.Sequential)]
  public struct FILETIME { public uint dwLowDateTime; public uint dwHighDateTime; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct RM_UNIQUE_PROCESS { public int dwProcessId; public FILETIME ProcessStartTime; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct RM_PROCESS_INFO {
    public RM_UNIQUE_PROCESS Process;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=CCH_RM_MAX_APP_NAME+1)] public string strAppName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=CCH_RM_MAX_SVC_NAME+1)] public string strServiceShortName;
    public RM_APP_TYPE ApplicationType;
    public uint AppStatus;
    public uint TSSessionId;
    [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
  }
  // strSessionKey is an OUT buffer of CCH_RM_SESSION_KEY+1 WCHARs per RM docs.
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, StringBuilder strSessionKey);
  [DllImport("rstrtmgr.dll")] public static extern int RmEndSession(uint pSessionHandle);
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames, uint nApplications, IntPtr rgApplications, uint nServices, string[] rgsServiceNames);
  [DllImport("rstrtmgr.dll")] public static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo, [In,Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);
  public static string QueryOne(string file) {
    uint handle;
    StringBuilder key = new StringBuilder(CCH_RM_SESSION_KEY + 1);
    int rc = RmStartSession(out handle, 0, key);
    if (rc != 0) return "";
    try {
      rc = RmRegisterResources(handle, 1, new string[]{ file }, 0, IntPtr.Zero, 0, null);
      if (rc != 0) return "";
      uint needed = 0, count = 0, reboot = 0;
      rc = RmGetList(handle, out needed, ref count, null, ref reboot);
      if (rc == 234) { // ERROR_MORE_DATA
        count = needed;
        RM_PROCESS_INFO[] arr = new RM_PROCESS_INFO[count];
        rc = RmGetList(handle, out needed, ref count, arr, ref reboot);
        if (rc != 0) return "";
        var parts = new System.Collections.Generic.List<string>();
        string safeFile = (file ?? "").Replace("|","/");
        for (int i=0;i<count;i++) {
          var p = arr[i];
          long fileTime = ((long)p.Process.ProcessStartTime.dwHighDateTime << 32) | (uint)p.Process.ProcessStartTime.dwLowDateTime;
          // FILETIME is 100ns since 1601; convert to unix seconds
          long unix = (fileTime - 116444736000000000L) / 10000000L;
          string name = (p.strAppName ?? "").Replace("|","/");
          parts.Add(p.Process.dwProcessId.ToString() + "|" + unix.ToString() + "|" + name + "|" + safeFile);
        }
        return string.Join(";", parts);
      }
      return "";
    } finally { RmEndSession(handle); }
  }
}
"@
$items = @()
foreach ($resource in $resources) {
  try {
    $raw = [HermesRm]::QueryOne([string]$resource)
  } catch {
    continue
  }
  if (-not $raw) { continue }
  foreach ($part in ($raw -split ';')) {
    if (-not $part) { continue }
    $bits = ${RESTART_MANAGER_ROW_SPLIT_EXPRESSION}
    if ($bits.Count -lt 2) { continue }
    $pidVal = 0; $created = 0.0
    if (-not [int]::TryParse($bits[0], [ref]$pidVal)) { continue }
    if (-not [double]::TryParse($bits[1], [ref]$created)) { continue }
    if ($pidVal -le 0 -or $created -le 0) { continue }
    $name = if ($bits.Count -ge 3) { $bits[2] } else { 'unknown' }
    $res = if ($bits.Count -ge 4 -and $bits[3]) { $bits[3] } else { [string]$resource }
    $items += [pscustomobject]@{ pid = $pidVal; createdAt = $created; name = $name; resource = $res }
  }
}
$items | ConvertTo-Json -Compress -Depth 3
`.trim()
}

export function parseRestartManagerOutput(
  stdout: string,
  resources: readonly string[]
): ForceReleaseHolder[] {
  const text = String(stdout || '').trim()

  if (!text || text === '[]') {return []}

  let parsed: any

  try {
    parsed = JSON.parse(text)
  } catch {
    return []
  }

  const rows = Array.isArray(parsed) ? parsed : [parsed]
  const fallbackResource = resources[0]
  const holders: ForceReleaseHolder[] = []

  for (const row of rows) {
    const pid = Number(row?.pid)
    const createdAt = Number(row?.createdAt)
    const name = typeof row?.name === 'string' && row.name ? row.name : 'unknown'

    const resource =
      typeof row?.resource === 'string' && row.resource
        ? row.resource
        : fallbackResource

    if (!Number.isInteger(pid) || pid <= 0) {continue}

    if (!Number.isFinite(createdAt) || createdAt <= 0) {continue}
    holders.push({
      pid,
      createdAt,
      name,
      cmdline: name,
      source: 'restart-manager',
      resource,
      role: 'other'
    })
  }

  return holders
}

export async function listRestartManagerHoldersForResources(
  resources: readonly string[],
  {
    platform = process.platform,
    run = defaultRunPowerShell,
    timeoutMs = 4_000
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    timeoutMs?: number
  } = {}
): Promise<ForceReleaseHolder[]> {
  if (platform !== 'win32' || resources.length === 0) {
    return []
  }

  const existing = resources.filter(Boolean)

  if (existing.length === 0) {return []}

  const budget = Math.max(1, Math.trunc(timeoutMs))
  const script = buildRestartManagerScript(existing)
  const result = await run(script, budget)

  return parseRestartManagerOutput(result.stdout, existing)
}
