# Contract test for scripts/fork_integration/hermes-integration-release-windows.py.
#
# Pins the earliest fail-closed gate reachable in a fake install: with no
# integration worktree present, `--dry-run` must exit nonzero and report
# "integration worktree is absent" -- without ever touching the real
# %USERPROFILE%\AppData\Local\hermes. Deeper contract coverage (populated
# fake worktrees, full reconstruction flows) arrives with U2's fixtures.
#
# DEVIATION FROM THE ORIGINAL PLAN TEXT (verified, not guessed): the script
# does NOT read an HERMES_HOME environment variable anywhere --
# `HOME = Path.home(); HERMES_HOME = HOME / "AppData" / "Local" / "hermes"`
# is fixed at import time from Path.home() alone. Setting $env:HERMES_HOME
# has zero effect on the script's behavior (confirmed by grepping the
# imported script and by a direct subprocess probe: `HERMES_HOME` in the
# child env left `Path.home()` unchanged, while `USERPROFILE` did not). On
# Windows, Path.home() resolves via %USERPROFILE%, so faking the install
# means overriding USERPROFILE, not HERMES_HOME. Both are set below --
# USERPROFILE because it is what actually works, HERMES_HOME kept in case a
# future revision of the script starts honoring it.

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$releaseScript = Join-Path $repoRoot 'scripts\fork_integration\hermes-integration-release-windows.py'
$failures = 0

function Assert-True {
    param($Condition, [string]$Label)
    if (-not $Condition) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}

if (-not (Test-Path -LiteralPath $releaseScript)) {
    throw "release script not found: $releaseScript"
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw 'no python interpreter found on PATH'
}

$fakeHome = Join-Path ([System.IO.Path]::GetTempPath()) ('fork-integration-release-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $fakeHome | Out-Null

$originalUserProfile = $env:USERPROFILE
$originalHermesHome = $env:HERMES_HOME

try {
    # Fake, empty install: no AppData\Local\hermes\worktrees\... under here at all.
    $env:USERPROFILE = $fakeHome
    $env:HERMES_HOME = Join-Path $fakeHome 'AppData\Local\hermes'

    $output = & $pythonCommand.Source $releaseScript '--dry-run' 2>&1 | Out-String
    $exitCode = $LASTEXITCODE

    Assert-True ($exitCode -ne 0) "dry-run against an absent worktree exits nonzero (got $exitCode)"
    Assert-True ($output -match 'integration worktree is absent') 'dry-run output names the fail-closed gate ("integration worktree is absent")'
} finally {
    if ($null -eq $originalUserProfile) {
        Remove-Item Env:\USERPROFILE -ErrorAction SilentlyContinue
    } else {
        $env:USERPROFILE = $originalUserProfile
    }
    if ($null -eq $originalHermesHome) {
        Remove-Item Env:\HERMES_HOME -ErrorAction SilentlyContinue
    } else {
        $env:HERMES_HOME = $originalHermesHome
    }
    if (Test-Path -LiteralPath $fakeHome) {
        Remove-Item -LiteralPath $fakeHome -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failures -gt 0) {
    Write-Host "FAILED: $failures assertion(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host 'All fork-integration release contract tests passed.' -ForegroundColor Green
exit 0
