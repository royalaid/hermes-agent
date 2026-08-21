param(
  [Parameter(Mandatory = $true)][string]$JobName,
  [Parameter(Mandatory = $true)][string]$HelperPath,
  [Parameter(Mandatory = $true)][string]$RequestPath,
  [Parameter(Mandatory = $true)][string]$ResponsePath
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HermesElevatedBoundaryJoin {
  const uint JOB_OBJECT_ALL_ACCESS = 0x001F001F;
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
  [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
  public static bool JoinCurrent(string name) {
    IntPtr job = OpenJobObject(JOB_OBJECT_ALL_ACCESS, false, name);
    if (job == IntPtr.Zero) return false;
    try { return AssignProcessToJobObject(job, GetCurrentProcess()); }
    finally { CloseHandle(job); }
  }
}
'@

if (-not [HermesElevatedBoundaryJoin]::JoinCurrent($JobName)) {
  exit 5
}

& $HelperPath -RequestPath $RequestPath -ResponsePath $ResponsePath
exit $LASTEXITCODE
