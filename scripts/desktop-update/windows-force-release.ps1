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
  $excludeList = @()
  if ($request.PSObject.Properties['excludePids'] -and $request.excludePids) {
    foreach ($ex in @($request.excludePids)) {
      try {
        $exPid = [int]$ex
        if ($exPid -gt 0) { $excludeList += $exPid }
      } catch {}
    }
  }
  $excludeLine = (($excludeList | Sort-Object -Unique) -join ',')
  $canonical = @(
    [string][int]$request.schemaVersion
    $nonce
    (Format-CanonicalNumber ([double]$request.issuedAt))
    (Format-CanonicalNumber ([double]$request.expiresAt))
    $installRoot
    $expectedHash
    ($holderLines -join "`n")
    $excludeLine
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
  public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint FILE_SHARE_ALL = 0x00000007;
  public const uint OPEN_EXISTING = 3;
  public const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
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
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetProcessTimes(IntPtr process, out FILETIME creation, out FILETIME exit, out FILETIME kernel, out FILETIME user);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr CreateFile(string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes, uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern uint GetFinalPathNameByHandle(IntPtr file, StringBuilder path, uint pathLength, uint flags);
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
  public static double ToUnixSeconds(FILETIME time) {
    long ticks = ((long)time.dwHighDateTime << 32) | time.dwLowDateTime;
    return (ticks - 116444736000000000L) / 10000000.0;
  }
  public static IntPtr OpenAuthenticated(int pid, double expectedUnix, out int error) {
    TryEnableDebugPrivilege();
    error = 0;
    IntPtr h = OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, pid);
    if (h == IntPtr.Zero) { error = Marshal.GetLastWin32Error(); return IntPtr.Zero; }
    FILETIME creation, exit, kernel, user;
    if (!GetProcessTimes(h, out creation, out exit, out kernel, out user)) {
      error = Marshal.GetLastWin32Error(); CloseHandle(h); return IntPtr.Zero;
    }
    if (Math.Abs(ToUnixSeconds(creation) - expectedUnix) > 1.5) {
      error = 0x10001; CloseHandle(h); return IntPtr.Zero;
    }
    return h;
  }
  public static string ReadImagePath(IntPtr process) {
    uint size = 32768;
    var buffer = new StringBuilder((int)size);
    return QueryFullProcessImageName(process, 0, buffer, ref size) ? buffer.ToString() : "";
  }
  public static string ReadFinalPath(string path) {
    if (String.IsNullOrWhiteSpace(path)) return "";
    IntPtr h = CreateFile(path, 0, FILE_SHARE_ALL, IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero);
    if (h == new IntPtr(-1)) return "";
    try {
      var buffer = new StringBuilder(32768);
      uint length = GetFinalPathNameByHandle(h, buffer, (uint)buffer.Capacity, 0);
      if (length == 0 || length >= buffer.Capacity) return "";
      string value = buffer.ToString();
      if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) value = @"\\" + value.Substring(8);
      else if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) value = value.Substring(4);
      return value.TrimEnd('\\');
    } finally { CloseHandle(h); }
  }
  public static bool IsSameOrUnderRoot(string path, string root) {
    if (String.IsNullOrWhiteSpace(path) || String.IsNullOrWhiteSpace(root)) return false;
    string cleanRoot = root.TrimEnd('\\');
    return path.Equals(cleanRoot, StringComparison.OrdinalIgnoreCase) || path.StartsWith(cleanRoot + "\\", StringComparison.OrdinalIgnoreCase);
  }
  public static int TerminateHandle(IntPtr h) {
    if (!TerminateProcess(h, 1)) return Marshal.GetLastWin32Error() == 0 ? -1 : Marshal.GetLastWin32Error();
    return WaitForSingleObject(h, 2000) == 0 ? 0 : -258;
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
  # Signed exclude list from Desktop (Desktop main PID, updater helper, etc.).
  if ($request.PSObject.Properties['excludePids'] -and $request.excludePids) {
    foreach ($ex in @($request.excludePids)) {
      try {
        $exPid = [int]$ex
        if ($exPid -gt 0) { [void]$exclude.Add($exPid) }
      } catch {}
    }
  }

  $installRootFinal = [HermesElevatedTerminate]::ReadFinalPath($installRootFull)
  if ([string]::IsNullOrWhiteSpace($installRootFinal)) { throw 'install root final path unavailable' }

  $resourceList = New-Object System.Collections.Generic.List[string]
  foreach ($rel in @('venv\Scripts\hermes.exe', 'venv\Scripts\python.exe', 'venv\python.exe')) {
    $candidate = Join-Path $installRootFull $rel
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    $candidateFinal = [HermesElevatedTerminate]::ReadFinalPath($candidate)
    if (
      [string]::IsNullOrWhiteSpace($candidateFinal) -or
      -not [HermesElevatedTerminate]::IsSameOrUnderRoot($candidateFinal, $installRootFinal)
    ) { throw 'default resource outside install root' }
    if (-not ($resourceList -contains $candidateFinal)) { $resourceList.Add($candidateFinal) | Out-Null }
  }

  # Authorized claim map from the signed request. Every target must name one
  # exact resource whose handle-resolved final path remains under installRoot.
  $claims = @{}
  foreach ($h in @($request.holders)) {
    $claimPid = [int]$h.pid
    if ($claimPid -le 0 -or $exclude.Contains($claimPid)) { continue }
    if (-not $h.PSObject.Properties['resource'] -or [string]::IsNullOrWhiteSpace([string]$h.resource)) {
      throw 'holder resource claim missing'
    }
    $claimResourceFinal = [HermesElevatedTerminate]::ReadFinalPath([string]$h.resource)
    if (
      [string]::IsNullOrWhiteSpace($claimResourceFinal) -or
      -not [HermesElevatedTerminate]::IsSameOrUnderRoot($claimResourceFinal, $installRootFinal)
    ) { throw 'holder resource outside install root' }
    if (-not ($resourceList -contains $claimResourceFinal)) { $resourceList.Add($claimResourceFinal) | Out-Null }
    $claims[$claimPid] = [pscustomobject]@{
      pid = $claimPid
      createdAt = [double]$h.createdAt
      name = [string]$h.name
      resource = $claimResourceFinal
    }
  }

  # Re-enumerate live holders ONLY against authenticated claims:
  # Restart Manager on install resources, then identity match against signed claims.
  # Do NOT path-scan the entire installRoot for unclaimed processes (that can
  # target Desktop electron.exe under a dev SOURCE_REPO_ROOT).
  $live = @{}
  if ($resourceList.Count -gt 0) {
    foreach ($resPath in $resourceList) {
      foreach ($row in [HermesElevatedTerminate]::QueryRestartManager([string[]]@($resPath))) {
        if (-not $row) { continue }
        $bits = $row.Split([char]'|', 3)
        if ($bits.Count -lt 2) { continue }
        $rmPid = 0
        $rmCreated = 0.0
        if (-not [int]::TryParse($bits[0], [ref]$rmPid)) { continue }
        if (-not [double]::TryParse($bits[1], [ref]$rmCreated)) { continue }
        if ($rmPid -le 0 -or $exclude.Contains($rmPid)) { continue }
        if (-not $claims.ContainsKey($rmPid)) { continue }
        $rmName = if ($bits.Count -ge 3) { $bits[2] } else { 'unknown' }
        $live[$rmPid] = [pscustomobject]@{
          pid = $rmPid
          createdAt = $rmCreated
          name = $rmName
          resource = $resPath
          source = 'restart-manager'
        }
      }
    }
  }

  # Eligible targets: current Restart Manager holders that still match a signed
  # claim. A live PID/create-time claim alone is not termination authority after
  # the process releases the install resource.
  # Fail-closed: never terminate unauthenticated/unclaimed installRoot processes.
  $targets = @()
  foreach ($entry in $live.Values) {
    $targetPid = [int]$entry.pid
    if ($exclude.Contains($targetPid)) { continue }
    if (-not $claims.ContainsKey($targetPid)) { continue }
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
  }

  $terminated = New-Object System.Collections.Generic.List[int]
  $survivors = New-Object System.Collections.Generic.List[object]

  foreach ($target in $targets) {
    $holderPid = [int]$target.pid
    $expected = [double]$target.createdAt
    $resource = [string]$target.resource
    $processHandle = [IntPtr]::Zero
    try {
      $openError = 0
      $processHandle = [HermesElevatedTerminate]::OpenAuthenticated($holderPid, $expected, [ref]$openError)
      if ($processHandle -eq [IntPtr]::Zero) {
        $detail = if ($openError -eq 0x10001) { 'create-time-mismatch' } else { "open-failed win32=$openError" }
        $survivor = [ordered]@{ pid = $holderPid; detail = $detail; resource = $resource }
        if ($openError -gt 0 -and $openError -ne 0x10001) { $survivor.win32Error = [int]$openError }
        $survivors.Add([pscustomobject]$survivor) | Out-Null
        continue
      }

      $imagePath = [HermesElevatedTerminate]::ReadImagePath($processHandle)
      $imageFinal = [HermesElevatedTerminate]::ReadFinalPath($imagePath)
      if (
        [string]::IsNullOrWhiteSpace($imageFinal) -or
        -not [HermesElevatedTerminate]::IsSameOrUnderRoot($imageFinal, $installRootFinal)
      ) {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = 'executable-outside-install-root'
          resource = $resource
        }) | Out-Null
        continue
      }

      # Re-enumerate the exact resource after opening/authenticating the process
      # handle. Termination below uses this same handle, never a PID reopen.
      $ownsResourceNow = $false
      foreach ($row in [HermesElevatedTerminate]::QueryRestartManager([string[]]@($resource))) {
        if (-not $row) { continue }
        $bits = $row.Split([char]'|', 3)
        if ($bits.Count -lt 2) { continue }
        $rmPid = 0
        $rmCreated = 0.0
        if (-not [int]::TryParse($bits[0], [ref]$rmPid)) { continue }
        if (-not [double]::TryParse($bits[1], [ref]$rmCreated)) { continue }
        if ($rmPid -eq $holderPid -and [math]::Abs($rmCreated - $expected) -le 1.5) {
          $ownsResourceNow = $true
          break
        }
      }
      if (-not $ownsResourceNow) {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = 'current-lock-ownership-mismatch'
          resource = $resource
        }) | Out-Null
        continue
      }

      $rc = [HermesElevatedTerminate]::TerminateHandle($processHandle)
      if ($rc -eq 0) {
        $terminated.Add($holderPid) | Out-Null
      } else {
        $survivors.Add([pscustomobject]@{
          pid = $holderPid
          detail = "win32=$rc"
          resource = $resource
          win32Error = [int]$rc
        }) | Out-Null
      }
    } catch {
      $survivors.Add([pscustomobject]@{
        pid = $holderPid
        detail = 'elevated-authorization-failed'
        resource = $resource
      }) | Out-Null
    } finally {
      if ($processHandle -ne [IntPtr]::Zero) {
        [HermesElevatedTerminate]::CloseHandle($processHandle) | Out-Null
      }
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
