#requires -Version 5.1
<#
.SYNOPSIS
  Elevated helper: revalidate and terminate exact install holders for Hermes Desktop update.

.DESCRIPTION
  Reads a nonce-scoped request file written by Desktop, re-checks each PID/create-time
  claim, enables SeDebugPrivilege when available, terminates eligible holders via
  OpenProcess/TerminateProcess, proves the response, and exits.

  Does not accept arbitrary command text. Does not mutate the virtual environment.
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
  $tmp = "$ResponsePath.$PID.tmp"
  Set-Content -LiteralPath $tmp -Value $json -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $ResponsePath -Force
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

  $expectedHash = [string]$request.installRootHash
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($installRoot)))
    $actualHash = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
  } finally {
    $sha.Dispose()
  }
  if ($actualHash -ne $expectedHash) { throw 'install root hash mismatch' }

  # MAC over canonical body (must match Electron canonicalForceReleasePayload)
  $holderLines = @()
  foreach ($h in @($request.holders)) {
    $res = if ($h.resource) { [string]$h.resource } else { '' }
    $holderLines += ("{0}`t{1}`t{2}`t{3}" -f [int]$h.pid, [double]$h.createdAt, [string]$h.name, $res)
  }
  $canonical = @(
    [string][int]$request.schemaVersion
    $nonce
    [string][int64]$request.issuedAt
    [string][int64]$request.expiresAt
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
using System.Runtime.InteropServices;
public static class HermesElevatedTerminate {
  public const uint PROCESS_TERMINATE = 0x0001;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint TOKEN_ADJUST_PRIVILEGES = 0x0020;
  public const uint TOKEN_QUERY = 0x0008;
  public const uint SE_PRIVILEGE_ENABLED = 0x00000002;
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct LUID { public uint LowPart; public int HighPart; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct LUID_AND_ATTRIBUTES { public LUID Luid; public uint Attributes; }
  [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct TOKEN_PRIVILEGES { public uint PrivilegeCount; public LUID_AND_ATTRIBUTES Privileges; }
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool LookupPrivilegeValue(string lpSystemName, string lpName, out LUID lpLuid);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool AdjustTokenPrivileges(IntPtr TokenHandle, bool DisableAllPrivileges, ref TOKEN_PRIVILEGES NewState, uint BufferLength, IntPtr PreviousState, IntPtr ReturnLength);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr hObject);
  [DllImport("kernel32.dll")] public static extern IntPtr GetCurrentProcess();
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
}
"@

  $terminated = New-Object System.Collections.Generic.List[int]
  $survivors = New-Object System.Collections.Generic.List[object]

  foreach ($holder in @($request.holders)) {
    $pid = [int]$holder.pid
    $expected = [double]$holder.createdAt
    try {
      $proc = Get-Process -Id $pid -ErrorAction Stop
      $actual = [DateTimeOffset]::new($proc.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
      if ([math]::Abs($actual - $expected) -gt 1.5) {
        $survivors.Add([pscustomobject]@{ pid = $pid; detail = 'create-time-mismatch' })
        continue
      }
      $rc = [HermesElevatedTerminate]::Terminate($pid)
      if ($rc -eq 0) {
        $terminated.Add($pid) | Out-Null
      } else {
        $survivors.Add([pscustomobject]@{ pid = $pid; detail = "win32=$rc" })
      }
    } catch {
      # Already gone counts as success for this PID.
      $terminated.Add($pid) | Out-Null
    }
  }

  $shim = Join-Path $installRoot 'venv\Scripts\hermes.exe'
  $cleared = $true
  if (Test-Path -LiteralPath $shim) {
    try {
      $fs = [System.IO.File]::Open($shim, 'Open', 'ReadWrite', 'None')
      $fs.Close()
    } catch {
      $cleared = $false
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
  exit 0
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
