# Compatibility entry point for checkouts and automation that still invoke
# scripts\desktop-update.ps1. The canonical Windows Desktop updater lives at
# scripts\desktop-update\windows.ps1; keep all protocol and UX behavior there.

param(
    [string]$InstallRoot,
    [string]$Branch = "main",
    [int]$DesktopPid = 0,
    [string]$RelaunchExe = "",
    [string]$RelaunchAppPath = "",
    [string]$BridgeLeaseId = "",
    [switch]$NoUi,
    [switch]$NoMarkerCleanup,
    [switch]$SelfTestUi
)

if (-not $SelfTestUi -and -not $InstallRoot) {
    throw "-InstallRoot is required"
}

$canonicalScript = Join-Path $PSScriptRoot "desktop-update\windows.ps1"
if (-not (Test-Path -LiteralPath $canonicalScript -PathType Leaf)) {
    Write-Error "Canonical Windows Desktop updater is missing: $canonicalScript"
    exit 1
}

& $canonicalScript @PSBoundParameters
if ($null -eq $LASTEXITCODE) { exit 1 }
exit $LASTEXITCODE
