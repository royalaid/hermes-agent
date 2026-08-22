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
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<{ stdout: string; stderr: string; code: number }>

function fixedOsEnvironment(): NodeJS.ProcessEnv {
  const allowed = new Set(['COMSPEC', 'PATHEXT', 'PSMODULEPATH', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'WINDIR'])

  const result: NodeJS.ProcessEnv = {}

  for (const [name, value] of Object.entries(process.env)) {
    if (value !== undefined && allowed.has(name.toUpperCase())) {
      result[name] = value
    }
  }

  return result
}

export class RestartManagerProbeError extends Error {
  readonly code = 'restart-manager-probe-failed'

  constructor() {
    super('restart-manager-probe-failed')
  }
}

function powershellExecutable(): string {
  const windowsRoot = process.env.SystemRoot || 'C:\\Windows'

  return path.join(windowsRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

async function defaultRunPowerShell(
  script: string,
  timeoutMs = 4_000,
  signal?: AbortSignal
): Promise<{ stdout: string; stderr: string; code: number }> {
  const budget = Math.max(1, Math.trunc(timeoutMs))

  try {
    const { stdout, stderr } = await execFileAsync(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      {
        encoding: 'utf8',
        timeout: budget,
        windowsHide: true,
        maxBuffer: 2 * 1024 * 1024,
        signal,
        env: fixedOsEnvironment()
      }
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
export const RESTART_MANAGER_ROW_SPLIT_EXPRESSION = "$part.Split([char]'|', 5)"

export function buildRestartManagerScript(resources: readonly string[]): string {
  const list = resources.map(escapePsSingleQuoted).join(',')

  return `
$ErrorActionPreference = 'Stop'
$resources = @(${list})
if (-not $resources -or $resources.Count -eq 0) { Write-Output '[]'; exit 0 }
Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class HermesRm {
  public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint TH32CS_SNAPPROCESS = 0x00000002;
  public const uint WAIT_OBJECT_0 = 0;
  public const uint WAIT_TIMEOUT = 258;
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
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct PROCESSENTRY32 {
    public uint dwSize;
    public uint cntUsage;
    public uint th32ProcessID;
    public UIntPtr th32DefaultHeapID;
    public uint th32ModuleID;
    public uint cntThreads;
    public uint th32ParentProcessID;
    public int pcPriClassBase;
    public uint dwFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string szExeFile;
  }
  // strSessionKey is an OUT buffer of CCH_RM_SESSION_KEY+1 WCHARs per RM docs.
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, [Out] StringBuilder strSessionKey);
  [DllImport("rstrtmgr.dll")] public static extern int RmEndSession(uint pSessionHandle);
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, [MarshalAs(UnmanagedType.LPArray, ArraySubType=UnmanagedType.LPWStr)] string[] rgsFilenames, uint nApplications, IntPtr rgApplications, uint nServices, [MarshalAs(UnmanagedType.LPArray, ArraySubType=UnmanagedType.LPWStr)] string[] rgsServiceNames);
  [DllImport("rstrtmgr.dll")] public static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo, [In,Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetProcessTimes(IntPtr process, out FILETIME creation, out FILETIME exit, out FILETIME kernel, out FILETIME user);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool Process32First(IntPtr snapshot, ref PROCESSENTRY32 entry);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool Process32Next(IntPtr snapshot, ref PROCESSENTRY32 entry);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr handle);
  public static ulong FileTimeValue(FILETIME value) {
    return ((ulong)value.dwHighDateTime << 32) | value.dwLowDateTime;
  }
  public static int ParentPidForExactProcess(int pid, ulong expectedFileTime) {
    IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, pid);
    if (process == IntPtr.Zero) return -1;
    try {
      if (WaitForSingleObject(process, 0) != WAIT_TIMEOUT) return -1;
      FILETIME creation, exit, kernel, user;
      if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) return -1;
      if (FileTimeValue(creation) != expectedFileTime) return -1;

      IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
      if (snapshot == new IntPtr(-1)) return -1;
      try {
        PROCESSENTRY32 entry = new PROCESSENTRY32();
        entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
        if (!Process32First(snapshot, ref entry)) return -1;
        do {
          if (entry.th32ProcessID != (uint)pid) continue;
          if (WaitForSingleObject(process, 0) != WAIT_TIMEOUT) return -1;
          if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) return -1;
          if (FileTimeValue(creation) != expectedFileTime) return -1;
          return entry.th32ParentProcessID <= Int32.MaxValue ? (int)entry.th32ParentProcessID : -1;
        } while (Process32Next(snapshot, ref entry));
        return -1;
      } finally { CloseHandle(snapshot); }
    } finally { CloseHandle(process); }
  }
  public static string QueryOne(string file) {
    uint handle;
    StringBuilder key = new StringBuilder(CCH_RM_SESSION_KEY + 1);
    int rc = RmStartSession(out handle, 0, key);
    if (rc != 0) throw new Win32Exception(rc, "RM_START_FAILED");
    try {
      rc = RmRegisterResources(handle, 1, new string[]{ file }, 0, IntPtr.Zero, 0, null);
      if (rc != 0) throw new Win32Exception(rc, "RM_REGISTER_FAILED");
      RM_PROCESS_INFO[] arr = null;
      uint finalCount = 0;
      for (int attempt=0; attempt<8; attempt++) {
        uint needed = 0, count = 0, reboot = 0;
        rc = RmGetList(handle, out needed, ref count, null, ref reboot);
        if (rc == 0 && needed == 0) return "";
        if ((rc != 234 && rc != 0) || needed == 0 || needed > 4096) {
          throw new Win32Exception(rc, "RM_LIST_FAILED");
        }
        count = needed;
        RM_PROCESS_INFO[] candidate = new RM_PROCESS_INFO[count];
        rc = RmGetList(handle, out needed, ref count, candidate, ref reboot);
        if (rc == 234) continue;
        if (rc != 0 || count > candidate.Length) throw new Win32Exception(rc, "RM_LIST_FAILED");
        arr = candidate;
        finalCount = count;
        break;
      }
      if (arr != null) {
        var parts = new System.Collections.Generic.List<string>();
        string safeFile = (file ?? "").Replace("|","/");
        for (int i=0;i<finalCount;i++) {
          var p = arr[i];
          ulong fileTime = FileTimeValue(p.Process.ProcessStartTime);
          int parentPid = ParentPidForExactProcess(p.Process.dwProcessId, fileTime);
          string name = (p.strAppName ?? "").Replace("|","/");
          parts.Add(p.Process.dwProcessId.ToString() + "|" + fileTime.ToString(System.Globalization.CultureInfo.InvariantCulture) + "|" + parentPid.ToString() + "|" + name + "|" + safeFile);
        }
        return string.Join(";", parts);
      }
      throw new Win32Exception(234, "RM_LIST_FAILED");
    } finally { RmEndSession(handle); }
  }
}
"@
$items = @()
$rawRowCount = 0
foreach ($resource in $resources) {
  try {
    $raw = [HermesRm]::QueryOne([string]$resource)
  } catch {
    throw 'Restart Manager query failed'
  }
  if (-not $raw) { continue }
  foreach ($part in ($raw -split ';')) {
    if (-not $part) { continue }
    $rawRowCount++
    $bits = ${RESTART_MANAGER_ROW_SPLIT_EXPRESSION}
    if ($bits.Count -lt 2) { continue }
    $pidVal = 0; $parentPid = -1; $fileTime = 0L
    if (-not [int]::TryParse($bits[0], [ref]$pidVal)) { continue }
    if ($bits[1] -notmatch '^\\d{15,20}$') { continue }
    if (-not [long]::TryParse($bits[1], [ref]$fileTime)) { continue }
    if ($pidVal -le 0 -or $fileTime -le 116444736000000000L) { continue }
    $created = ([double]$fileTime - 116444736000000000.0) / 10000000.0
    if ($bits.Count -ge 3) { [void][int]::TryParse($bits[2], [ref]$parentPid) }
    $name = if ($bits.Count -ge 4) { $bits[3] } else { 'unknown' }
    $res = if ($bits.Count -ge 5 -and $bits[4]) { $bits[4] } else { [string]$resource }
    $items += [pscustomobject]@{ pid = $pidVal; createdAt = $created; creationFileTime = $bits[1]; parentPid = $(if ($parentPid -gt 0) { $parentPid } else { $null }); name = $name; resource = $res; resources = @($res) }
  }
}
if ($rawRowCount -gt 0 -and $items.Count -eq 0) {
  throw 'Restart Manager returned malformed process identity'
}
if ($items.Count -eq 0) { Write-Output '[]' } else { $items | ConvertTo-Json -Compress -Depth 3 }
`.trim()
}

export function parseRestartManagerOutput(stdout: string, resources: readonly string[]): ForceReleaseHolder[] {
  const text = String(stdout || '').trim()

  if (!text || text === '[]') {
    return []
  }

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
    const creationFileTime = typeof row?.creationFileTime === 'string' ? row.creationFileTime : ''
    const name = typeof row?.name === 'string' && row.name ? row.name : 'unknown'
    const parentPid = Number(row?.parentPid)

    const resource = typeof row?.resource === 'string' && row.resource ? row.resource : fallbackResource

    if (!Number.isInteger(pid) || pid <= 0) {
      continue
    }

    if (!Number.isFinite(createdAt) || createdAt <= 0) {
      continue
    }

    if (!/^\d{15,20}$/.test(creationFileTime)) {
      continue
    }

    holders.push({
      pid,
      createdAt,
      creationFileTime,
      name,
      cmdline: name,
      source: 'restart-manager',
      ...(Number.isInteger(parentPid) && parentPid > 0 ? { parentPid } : {}),
      resource,
      resources: resource ? [resource] : [],
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
    timeoutMs = 4_000,
    signal
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    timeoutMs?: number
    signal?: AbortSignal
  } = {}
): Promise<ForceReleaseHolder[]> {
  if (platform !== 'win32' || resources.length === 0) {
    return []
  }

  const existing = resources.filter(Boolean)

  if (existing.length === 0) {
    return []
  }

  const budget = Math.max(1, Math.trunc(timeoutMs))
  const script = buildRestartManagerScript(existing)
  const result = await run(script, budget, signal)

  if (result.code !== 0) {
    throw new RestartManagerProbeError()
  }

  const text = String(result.stdout || '').trim()

  if (!text) {
    throw new RestartManagerProbeError()
  }

  const holders = parseRestartManagerOutput(text, existing)

  if (text !== '[]' && holders.length === 0) {
    throw new RestartManagerProbeError()
  }

  return holders
}
