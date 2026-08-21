#requires -Version 5.1
<#
.SYNOPSIS
  Elevated helper: revalidate and terminate exact install holders for Hermes Desktop update.

.DESCRIPTION
  Reads a nonce-scoped request file written by Desktop, re-enumerates current
  install holders (Restart Manager + process path under install root), enables
  SeDebugPrivilege when available, terminates eligible holders via
  OpenProcess/TerminateProcess, proves every relevant resource is clear, and
  exits. Does not accept arbitrary command text. Does not mutate the virtual
  environment.
#>
param(
  [Parameter(Mandatory = $true)][string]$RequestPath,
  [Parameter(Mandatory = $true)][string]$ResponsePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Response([hashtable]$Payload) {
  $json = $Payload | ConvertTo-Json -Compress -Depth 6
  $dir = Split-Path -Parent $ResponsePath
  if ($dir -and -not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  # Use a non-automatic variable; $PID is read-only in PowerShell.
  $tmp = "$ResponsePath.$([System.Diagnostics.Process]::GetCurrentProcess().Id).tmp"
  Set-Content -LiteralPath $tmp -Value $json -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $ResponsePath -Force
}

function Format-CanonicalNumber([double]$Value) {
  if ([double]::IsNaN($Value) -or [double]::IsInfinity($Value)) { return '0' }
  if ($Value -eq 0) { return '0' }
  $truncated = [math]::Truncate($Value)
  if ($Value -eq $truncated -and [math]::Abs($Value) -lt 9007199254740991) {
    return [string][int64]$truncated
  }
  # Match JS Number#toString / ECMA round-trip and .NET "R" under invariant culture.
  return $Value.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture)
}

function Test-FileUnlocked([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $true }
  try {
    $fs = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
    $fs.Close()
    return $true
  } catch {
    return $false
  }
}

try {
  if (-not (Test-Path -LiteralPath $RequestPath)) {
    throw "request missing: $RequestPath"
  }

  $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $nonce = [string]$request.nonce
  $secretPath = Join-Path (Split-Path -Parent $RequestPath) ("force-release-$nonce.secret")
  if (-not (Test-Path -LiteralPath $secretPath)) {
    throw 'secret missing'
  }
  $secret = (Get-Content -LiteralPath $secretPath -Raw -Encoding UTF8).Trim()

  if ([int]$request.schemaVersion -ne 1) { throw 'bad schema' }
  $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if ($nowMs -gt [int64]$request.expiresAt) { throw 'request expired' }

  $installRoot = [string]$request.installRoot
  if (-not (Test-Path -LiteralPath $installRoot)) { throw 'install root missing' }
  $installRootFull = [IO.Path]::GetFullPath($installRoot)

  $expectedHash = [string]$request.installRootHash
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($installRootFull)
    $actualHash = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
  } finally {
    $sha.Dispose()
  }
  if ($actualHash -ne $expectedHash) { throw 'install root hash mismatch' }

  # MAC over canonical body (must match Electron canonicalForceReleasePayload)
  $holderLines = @()
  foreach ($h in @($request.holders)) {
    $res = if ($h.PSObject.Properties['resource'] -and $h.resource) { [string]$h.resource } else { '' }
    $holderLines += ("{0}`t{1}`t{2}`t{3}" -f [int]$h.pid, (Format-CanonicalNumber ([double]$h.createdAt)), [string]$h.name, $res)
  }
  $canonical = @(
    [string][int]$request.schemaVersion
    $nonce
    (Format-CanonicalNumber ([double]$request.issuedAt))
    (Format-CanonicalNumber ([double]$request.expiresAt))
    $installRoot
    $expectedHash
    ($holderLines -join "`n")
  ) -join "`n"
  $macSrc = $secret + "`n" + $canonical
  $sha2 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $mac = ($sha2.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($macSrc)) | ForEach-Object { $_.ToString('x2') }) -join ''
  } finally {
    $sha2.Dispose()
  }
  if ($mac -ne [string]$request.requestMac) { throw 'mac mismatch' }

  Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class HermesElevatedTerminate {
  public const uint PROCESS_TERMINATE = 0x0001;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
  public const uint TOKEN_QUERY = 0x0008;
  public const uint SE_PRIVILEGE_ENABLED = 0x00000002;
  public const int CCH_RM_SESSION_KEY = 32;
  public const int CCH_RM_MAX_APP_NAME = 255;
  public const int CCH_RM_MAX_SVC_NAME = 63;
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct LUID { public uint LowPart; public int HighPart; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct LUID_AND_ATTRIBUTES { public LUID Luid; public uint Attributes; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct TOKEN_PRIVILEGES { public uint PrivilegeCount; public LUID_AND_ATTRIBUTES Privileges; }
  [StructLayout(LayoutKind.Sequential)] public struct FILETIME { public uint dwLowDateTime; public uint dwHighDateTime; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct RM_UNIQUE_PROCESS { public int dwProcessId; public FILETIME ProcessStartTime; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct RM_PROCESS_INFO {
    public RM_UNIQUE_PROCESS Process;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=CCH_RM_MAX_APP_NAME+1)] public string strAppName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=CCH_RM_MAX_SVC_NAME+1)] public string strServiceShortName;
    public int ApplicationType;
    public uint AppStatus;
    public uint TSSessionId;
    [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
  }
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool LookupPrivilegeValue(string lpSystemName, string lpName, out LUID lpLuid);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool AdjustTokenPrivileges(IntPtr TokenHandle, bool DisableAllPrivileges, ref TOKEN_PRIVILEGES NewState, uint BufferLength, IntPtr PreviousState, IntPtr ReturnLength);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr hObject);
  [DllImport("kernel32.dll")] public static extern IntPtr GetCurrentProcess();
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, StringBuilder strSessionKey);
  [DllImport("rstrtmgr.dll")] public static extern int RmEndSession(uint pSessionHandle);
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] public static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames, uint nApplications, IntPtr rgApplications, uint nServices, string[] rgsServiceNames);
  [DllImport("rstrtmgr.dll")] public static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo, [In,Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);
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
  public static int Terminate(int pid) {
    TryEnableDebugPrivilege();
    IntPtr h = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, false, pid);
    if (h == IntPtr.Zero) return Marshal.GetLastWin32Error() == 0 ? -1 : Marshal.GetLastWin32Error();
    try {
      if (!TerminateProcess(h, 1)) return Marshal.GetLastWin32Error() == 0 ? -1 : Marshal.GetLastWin32Error();
      WaitForSingleObject(h, 2000);
      return 0;
    } finally { CloseHandle(h); }
  }
  public static List<string> QueryRestartManager(string[] files) {
    var rows = new List<string>();
    if (files == null || files.Length == 0) return rows;
    uint handle;
    StringBuilder key = new StringBuilder(CCH_RM_SESSION_KEY + 1);
    int rc = RmStartSession(out handle, 0, key);
    if (rc != 0) return rows;
    try {
      rc = RmRegisterResources(handle, (uint)files.Length, files, 0, IntPtr.Zero, 0, null);
      if (rc != 0) return rows;
      uint needed = 0, count = 0, reboot = 0;
      rc = RmGetList(handle, out needed, ref count, null, ref reboot);
      if (rc != 234) return rows;
      count = needed;
      RM_PROCESS_INFO[] arr = new RM_PROCESS_INFO[count];
      rc = RmGetList(handle, out needed, ref count, arr, ref reboot);
      if (rc != 0) return rows;
      for (int i = 0; i < count; i++) {
        var p = arr[i];
        long fileTime = ((long)p.Process.ProcessStartTime.dwHighDateTime << 32) | (uint)p.Process.ProcessStartTime.dwLowDateTime;
        long unix = (fileTime - 116444736000000000L) / 10000000L;
        string name = (p.strAppName ?? "").Replace("|", "/");
        rows.Add(p.Process.dwProcessId.ToString() + "|" + unix.ToString() + "|" + name);
      }
    } finally { RmEndSession(handle); }
    return rows;
  }
}
"@

  $selfPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
  $exclude = New-Object 'System.Collections.Generic.HashSet[int]'
  [void]$exclude.Add([int]$selfPid)
  try {
    $ppid = (Get-CimInstance Win32_Process -Filter "ProcessId = $selfPid" -ErrorAction SilentlyContinue).ParentProcessId
    if ($ppid) { [void]$exclude.Add([int]$ppid) }
  } catch {}

  $resourceList = New-Object System.Collections.Generic.List[string]
  foreach ($rel in @('venv\Scripts\hermes.exe', 'venv\Scripts\python.exe', 'venv\python.exe')) {
    $candidate = Join-Path $installRootFull $rel
    if (Test-Path -LiteralPath $candidate) { $resourceList.Add($candidate) | Out-Null }
  }
  foreach ($h in @($request.holders)) {
    if ($h.PSObject.Properties['resource'] -and $h.resource) {
      $resPath = [string]$h.resource
      if ($resPath -and (Test-Path -LiteralPath $resPath)) {
        if (-not ($resourceList -contains $resPath)) { $resourceList.Add($resPath) | Out-Null }
      }
    }
  }

  # Authorized claim map from the signed request (pid -> expected create time + metadata).
  $claims = @{}
  foreach ($h in @($request.holders)) {
    $claimPid = [int]$h.pid
    if ($claimPid -le 0 -or $exclude.Contains($claimPid)) { continue }
    $claims[$claimPid] = [pscustomobject]@{
      pid = $claimPid
      createdAt = [double]$h.createdAt
      name = [string]$h.name
      resource = if ($h.PSObject.Properties['resource'] -and $h.resource) { [string]$h.resource } else { '' }
    }
  }

  # Re-enumerate live holders: Restart Manager on install resources + process Path under install root.
  $live = @{}
  if ($resourceList.Count -gt 0) {
    foreach ($row in [HermesElevatedTerminate]::QueryRestartManager([string[]]$resourceList.ToArray())) {
      if (-not $row) { continue }
      $bits = $row.Split([char]'|', 3)
      if ($bits.Count -lt 2) { continue }
      $rmPid = 0
      $rmCreated = 0.0
      if (-not [int]::TryParse($bits[0], [ref]$rmPid)) { continue }
      if (-not [double]::TryParse($bits[1], [ref]$rmCreated)) { continue }
      if ($rmPid -le 0 -or $exclude.Contains($rmPid)) { continue }
      $rmName = if ($bits.Count -ge 3) { $bits[2] } else { 'unknown' }
      $live[$rmPid] = [pscustomobject]@{
        pid = $rmPid
        createdAt = $rmCreated
        name = $rmName
        resource = $resourceList[0]
        source = 'restart-manager'
      }
    }
  }

  $installPrefix = $installRootFull.TrimEnd('\') + '\'
  try {
    Get-CimInstance Win32_Process | ForEach-Object {
      $procPid = [int]$_.ProcessId
      if ($procPid -le 0 -or $exclude.Contains($procPid)) { return }
      $exe = $null
      try { $exe = $_.ExecutablePath } catch { $exe = $null }
      if (-not $exe) { return }
      try {
        $fullExe = [IO.Path]::GetFullPath($exe)
      } catch { return }
      if (-not $fullExe.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { return }
      try {
        $proc = Get-Process -Id $procPid -ErrorAction Stop
        $created = [DateTimeOffset]::new($proc.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
      } catch { return }
      if (-not $live.ContainsKey($procPid)) {
        $live[$procPid] = [pscustomobject]@{
          pid = $procPid
          createdAt = [double]$created
          name = [string]$_.Name
          resource = $fullExe
          source = 'path'
        }
      }
    }
  } catch {}

  # Eligible targets: currently live holders that still match a signed claim (or are live under the MAC-bound install root).
  # Fail-closed: only terminate processes that are currently holding install resources after re-enumeration.
  $targets = @()
  foreach ($entry in $live.Values) {
    $targetPid = [int]$entry.pid
    if ($exclude.Contains($targetPid)) { continue }
    if ($claims.ContainsKey($targetPid)) {
      $claim = $claims[$targetPid]
      if ([math]::Abs([double]$entry.createdAt - [double]$claim.createdAt) -gt 1.5) {
        continue
      }
      $targets += [pscustomobject]@{
        pid = $targetPid
        createdAt = [double]$entry.createdAt
        name = $entry.name
        resource = if ($claim.resource) { $claim.resource } else { $entry.resource }
      }
      continue
    }
    # Live install-root holder not in the original claim set still must be cleared (fail closed).
    $targets += [pscustomobject]@{
      pid = $targetPid
      createdAt = [double]$entry.createdAt
      name = $entry.name
      resource = $entry.resource
    }
  }

  $terminated = New-Object System.Collections.Generic.List[int]
  $survivors = New-Object System.Collections.Generic.List[object]

  foreach ($target in $targets) {
    $holderPid = [int]$target.pid
    $expected = [double]$target.createdAt
    $resource = [string]$target.resource
    try {
      $proc = Get-Process -Id $holderPid -ErrorAction Stop
      $actual = [DateTimeOffset]::new($proc.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
      if ([math]::Abs($actual - $expected) -gt 1.5) {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = 'create-time-mismatch'
          resource = $resource
        }) | Out-Null
        continue
      }
      $rc = [HermesElevatedTerminate]::Terminate($holderPid)
      if ($rc -eq 0) {
        $terminated.Add($holderPid) | Out-Null
      } elseif ($rc -eq 5) {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = 'protected win32=5'
          resource = $resource
          win32Error = 5
        }) | Out-Null
      } else {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = "win32=$rc"
          resource = $resource
          win32Error = [int]$rc
        }) | Out-Null
      }
    } catch {
      # Already gone counts as success for this PID.
      $terminated.Add($holderPid) | Out-Null
    }
  }

  # Prove every relevant resource is clear. Fail closed if any remain locked.
  $cleared = $true
  $lockedResources = New-Object System.Collections.Generic.List[string]
  foreach ($resPath in $resourceList) {
    if (-not (Test-FileUnlocked $resPath)) {
      $cleared = $false
      $lockedResources.Add($resPath) | Out-Null
    }
  }

  if (-not $cleared -and $survivors.Count -eq 0) {
    foreach ($resPath in $lockedResources) {
      $survivors.Add([pscustomobject]@{
        pid = 0
        detail = 'resource-still-locked'
        resource = $resPath
      }) | Out-Null
    }
  }

  Write-Response @{
    schemaVersion = 1
    nonce = $nonce
    ok = $true
    cleared = [bool]$cleared
    terminated = @($terminated)
    survivors = @($survivors)
  }
  if ($cleared) { exit 0 } else { exit 2 }
}
catch {
  $nonceVal = 'unknown'
  try { $nonceVal = [string](Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json).nonce } catch {}
  Write-Response @{
    schemaVersion = 1
    nonce = $nonceVal
    ok = $false
    cleared = $false
    error = [string]$_.Exception.Message
  }
  exit 1
}
