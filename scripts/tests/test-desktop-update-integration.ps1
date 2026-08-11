# Native-Windows end-to-end proof for the Desktop updater transaction.
#
# Unlike test-desktop-update-handoff.ps1, this test does not replace the
# managed Hermes updater.  It gives the production handoff a disposable
# managed checkout, a copied venv, a local bare Git remote, and a private
# HERMES_HOME, then lets desktop-update.ps1 run the real Python update,
# receipt, deferred-gateway, rebuild, relaunch, and acknowledgment paths.

param(
    [string]$SeedVenv = "",
    [switch]$KeepTemp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows
    )) {
    throw 'This integration test requires native Windows.'
}

$repoRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
)
if ([string]::IsNullOrWhiteSpace($SeedVenv)) {
    $SeedVenv = Join-Path $repoRoot '.venv'
}
$SeedVenv = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath $SeedVenv -ErrorAction Stop).ProviderPath
)
$seedPython = Join-Path $SeedVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $seedPython -PathType Leaf)) {
    throw "Seed venv has no Windows interpreter: $seedPython"
}
$seedVenvConfig = Join-Path $SeedVenv 'pyvenv.cfg'
$seedVenvConfigLines = [System.IO.File]::ReadAllLines($seedVenvConfig, [System.Text.Encoding]::UTF8)
$seedVenvConfigHash = (Get-FileHash -LiteralPath $seedVenvConfig -Algorithm SHA256).Hash
$seedVenvValues = @{}
$seedVenvMetadataLines = New-Object System.Collections.Generic.List[string]
foreach ($line in $seedVenvConfigLines) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) {
        $seedVenvMetadataLines.Add($line)
        continue
    }
    $separator = $trimmed.IndexOf('=')
    if ($separator -le 0) { throw 'Seed pyvenv.cfg contains a malformed entry.' }
    $key = $trimmed.Substring(0, $separator).Trim().ToLowerInvariant()
    if ($key -ne 'home' -and $key -ne 'executable') {
        $seedVenvMetadataLines.Add($line)
        continue
    }
    if ($seedVenvValues.ContainsKey($key)) { throw "Seed pyvenv.cfg repeats $key." }
    $value = $trimmed.Substring($separator + 1).Trim()
    if (-not $value -or -not [System.IO.Path]::IsPathRooted($value)) {
        throw "Seed pyvenv.cfg has an invalid $key."
    }
    $seedVenvValues[$key] = $value
}
if (-not $seedVenvValues.ContainsKey('home')) {
    throw 'Seed venv does not identify its base interpreter home.'
}
$baseHome = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath $seedVenvValues['home'] -ErrorAction Stop).ProviderPath
).TrimEnd([char[]]@('\', '/'))
$basePythonPath = if ($seedVenvValues.ContainsKey('executable')) {
    $seedVenvValues['executable']
} else {
    Join-Path $seedVenvValues['home'] 'python.exe'
}
$basePython = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath $basePythonPath -ErrorAction Stop).ProviderPath
)
if (-not (Test-Path -LiteralPath $basePython -PathType Leaf) -or
    -not [string]::Equals((Split-Path -Leaf $basePython), 'python.exe', [StringComparison]::OrdinalIgnoreCase) -or
    -not [string]::Equals(
        (Split-Path -Parent $basePython).TrimEnd([char[]]@('\', '/')),
        $baseHome,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Seed venv base interpreter is not the exact python.exe under home: $basePython"
}

$uvSource = Join-Path $SeedVenv 'Scripts\uv.exe'
if (-not (Test-Path -LiteralPath $uvSource -PathType Leaf)) {
    $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvCommand) { $uvSource = $uvCommand.Source }
}
if (-not (Test-Path -LiteralPath $uvSource -PathType Leaf)) {
    throw 'A uv.exe seed is required (install uv or place it in the seed venv).'
}

$gitCommand = Get-Command git.exe -ErrorAction Stop
$powerShellExe = (Get-Process -Id $PID -ErrorAction Stop).Path
$sourceSha = (& $gitCommand.Source -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Could not resolve the source checkout HEAD.'
}

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$suiteRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $tempBase ("hermes-desktop-update-e2e-{0}" -f [Guid]::NewGuid().ToString('N')))
)
if (-not $suiteRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe temporary root: $suiteRoot"
}

$hermesTestHome = Join-Path $suiteRoot 'home'
$installRoot = Join-Path $hermesTestHome 'hermes-agent'
$authorRoot = Join-Path $suiteRoot 'author'
$remoteRoot = Join-Path $suiteRoot 'upstream.git'
$branch = 'e2e-update-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
$leaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
$leasePath = Join-Path $hermesTestHome '.hermes-venv-quiesce'
$markerPath = Join-Path $hermesTestHome '.hermes-update-in-progress'
$receiptPath = Join-Path $hermesTestHome '.hermes-update-receipt.json'
$resultPath = Join-Path $hermesTestHome '.hermes-update-result.json'
$consumerCapturePath = Join-Path $hermesTestHome 'immediate-consumer-result.json'
$consumerPidPath = Join-Path $hermesTestHome 'immediate-consumer-pid.txt'
$relaunchExe = Join-Path $hermesTestHome 'HermesE2E.exe'
$handoffLog = Join-Path $hermesTestHome 'logs\desktop-update-handoff.log'
$foreignGatewayRoot = Join-Path $suiteRoot 'foreign-gateway'
$foreignGatewayReady = Join-Path $foreignGatewayRoot 'ready.txt'
$foreignGatewayStop = Join-Path $foreignGatewayRoot 'stop.txt'

$savedEnvironment = @{}
foreach ($name in @(
    'HERMES_HOME', 'HERMES_PROFILE', 'HERMES_DESKTOP_UPDATE_TEST',
    'HERMES_INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS', 'GIT_CONFIG_GLOBAL',
    'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM', 'UV_CACHE_DIR',
    'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_DOWNLOADS', 'npm_config_cache', 'CI'
)) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
}

function Invoke-Git {
    param(
        [AllowEmptyString()][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $allArguments = @()
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $allArguments += @('-C', $WorkingDirectory)
    }
    $allArguments += $Arguments
    $output = @(& $gitCommand.Source @allArguments 2>&1)
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "git $($Arguments -join ' ') failed ($code): $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine).Trim()
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $parent = Split-Path -Parent $Path
    if ($parent) { [System.IO.Directory]::CreateDirectory($parent) | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Assert-True {
    param([bool]$Condition, [string]$Label)
    if (-not $Condition) { throw "Assertion failed: $Label" }
    Write-Host "OK: $Label" -ForegroundColor Green
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Label)
    if ($Expected -ne $Actual) {
        throw "Assertion failed: $Label (expected '$Expected', got '$Actual')"
    }
    Write-Host "OK: $Label" -ForegroundColor Green
}

function Start-ForeignGatewayStub {
    [System.IO.Directory]::CreateDirectory((Join-Path $foreignGatewayRoot 'hermes_cli')) | Out-Null
    Write-Utf8NoBom (Join-Path $foreignGatewayRoot 'hermes_cli\__init__.py') ''
    Write-Utf8NoBom (Join-Path $foreignGatewayRoot 'hermes_cli\main.py') @'
import os
import time
from pathlib import Path

ready = Path(os.environ["HERMES_E2E_FOREIGN_GATEWAY_READY"])
stop = Path(os.environ["HERMES_E2E_FOREIGN_GATEWAY_STOP"])
ready.write_text(str(os.getpid()), encoding="utf-8")
while not stop.exists():
    time.sleep(0.05)
'@
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $basePython
    $start.Arguments = '-m hermes_cli.main gateway run'
    $start.WorkingDirectory = $foreignGatewayRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.EnvironmentVariables['HERMES_E2E_FOREIGN_GATEWAY_READY'] = $foreignGatewayReady
    $start.EnvironmentVariables['HERMES_E2E_FOREIGN_GATEWAY_STOP'] = $foreignGatewayStop
    $process = [System.Diagnostics.Process]::Start($start)
    if (-not $process) { throw 'Could not start the foreign gateway stub.' }
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path -LiteralPath $foreignGatewayReady -PathType Leaf) -and
        -not $process.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if ($process.HasExited -or -not (Test-Path -LiteralPath $foreignGatewayReady -PathType Leaf)) {
        throw 'The foreign gateway stub did not become ready.'
    }
    Assert-Equal $process.Id ([int]([System.IO.File]::ReadAllText($foreignGatewayReady))) 'foreign gateway stub reports its exact PID'
    return $process
}

function Stop-DisposableManagedGateway {
    param(
        [AllowNull()][string]$Python,
        [switch]$Required
    )
    if ([string]::IsNullOrWhiteSpace($Python) -or
        -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        return
    }

    $output = @(& $Python -m hermes_cli.main gateway stop 2>&1)
    $code = $LASTEXITCODE
    if ($output.Count -gt 0) { $output | Out-Host }
    if ($code -ne 0) {
        $message = "Disposable managed gateway cleanup failed (exit $code)."
        if ($Required) { throw $message }
        Write-Warning $message
    }
}

function Compile-DesktopConsumer {
    param([string]$Destination)
    $source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading;

public static class HermesDesktopE2E {
    public static int Main() {
        var home = AppDomain.CurrentDomain.BaseDirectory;
        var result = Path.Combine(home, ".hermes-update-result.json");
        var captured = Path.Combine(home, "immediate-consumer-result.json");
        File.WriteAllText(
            Path.Combine(home, "immediate-consumer-pid.txt"),
            Process.GetCurrentProcess().Id.ToString()
        );
        var deadline = DateTime.UtcNow.AddSeconds(20);
        while (DateTime.UtcNow < deadline) {
            if (File.Exists(result)) {
                var raw = File.ReadAllText(result);
                File.WriteAllText(captured, raw);
                Func<string, string> value = pattern => {
                    var match = Regex.Match(raw, pattern);
                    return match.Success ? match.Groups[1].Value : "";
                };
                var attempt = value("\\\"attempt_id\\\":\\\"([^\\\"]+)\\\"");
                var invocation = value("\\\"invocation_id\\\":\\\"([^\\\"]+)\\\"");
                var lease = value("\\\"lease_id\\\":\\\"([^\\\"]+)\\\"");
                var root = value("\\\"root\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                var executable = value("\\\"executable\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                var started = value("\\\"process_started_at\\\":(\\d+)");
                var build = value("\\\"resulting_head\\\":\\\"([0-9a-fA-F]+)\\\"");
                if (!String.IsNullOrEmpty(attempt) && !String.IsNullOrEmpty(build)) {
                    var ack = "{\"schema_version\":1,\"attempt_id\":\"" + attempt +
                        "\",\"invocation_id\":\"" + invocation + "\",\"lease_id\":\"" + lease +
                        "\",\"pid\":" + Process.GetCurrentProcess().Id + ",\"process_started_at\":" + started +
                        ",\"root\":\"" + root + "\",\"executable\":\"" + executable +
                        "\",\"build_id\":\"" + build + "\",\"build_source\":\"install-stamp\"" +
                        ",\"backend_ready\":true,\"backend_mode\":\"local\",\"acknowledged_at\":" +
                        DateTimeOffset.UtcNow.ToUnixTimeSeconds() + ",\"error\":null}";
                    var ackPath = Path.Combine(home, ".hermes-update-ack-" + attempt + ".json");
                    var temporary = ackPath + ".tmp-" + Guid.NewGuid().ToString("N");
                    File.WriteAllText(temporary, ack);
                    File.Move(temporary, ackPath);
                    Thread.Sleep(3000);
                    return 0;
                }
                return 13;
            }
            Thread.Sleep(20);
        }
        return 12;
    }
}
'@
    $sourcePath = [System.IO.Path]::ChangeExtension($Destination, '.cs')
    Write-Utf8NoBom $sourcePath $source
    $compiler = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw "Inbox C# compiler not found: $compiler"
    }
    & $compiler /nologo /target:exe "/out:$Destination" $sourcePath
    $compileCode = $LASTEXITCODE
    Remove-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue
    if ($compileCode -ne 0 -or -not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "Desktop acknowledgment consumer compilation failed ($compileCode)."
    }
}

function Write-MinimalNodeFixture {
    param([string]$Root)
$rootPackage = @'
{
  "name": "hermes-update-e2e-root",
  "version": "0.0.0",
  "private": true,
  "workspaces": ["ui-tui", "web", "apps/desktop"]
}
'@
    $tuiPackage = @'
{"name":"hermes-update-e2e-tui","version":"0.0.0","private":true}
'@
    $webPackage = @'
{
  "name": "hermes-update-e2e-web",
  "version": "0.0.0",
  "private": true,
  "devDependencies": {
    "typescript": "file:../scripts/tests/fixtures/windows-update-e2e-typescript",
    "vite": "file:../scripts/tests/fixtures/windows-update-e2e-vite"
  },
  "scripts": {"build": "node ../scripts/tests/fixtures/windows-update-e2e-node.cjs web"}
}
'@
$desktopPackage = @'
{
  "name": "hermes-update-e2e-desktop",
  "version": "0.0.0",
  "private": true,
  "scripts": {"pack": "node ../../scripts/tests/fixtures/windows-update-e2e-node.cjs desktop"}
}
'@
    $typescriptPackage = @'
{
  "name": "typescript",
  "version": "0.0.0",
  "bin": {"tsc": "bin.cjs"}
}
'@
    $vitePackage = @'
{
  "name": "vite",
  "version": "0.0.0",
  "bin": {"vite": "bin.cjs"}
}
'@
    $toolBin = @'
#!/usr/bin/env node
process.exit(0)
'@
    $nodeFixture = @'
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..', '..', '..');
const mode = process.argv[2];
if (mode === 'web') {
  const output = path.join(root, 'hermes_cli', 'web_dist', 'index.html');
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, '<!doctype html><title>Hermes updater e2e</title>\n');
} else if (mode === 'desktop') {
  const source = path.join(process.env.SystemRoot || process.env.WINDIR, 'System32', 'where.exe');
  const output = path.join(root, 'apps', 'desktop', 'release', 'win-unpacked', 'Hermes.exe');
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.copyFileSync(source, output);
} else {
  throw new Error(`unknown updater e2e fixture mode: ${mode}`);
}
'@
    Write-Utf8NoBom (Join-Path $Root 'package.json') $rootPackage
    Write-Utf8NoBom (Join-Path $Root 'ui-tui\package.json') $tuiPackage
    Write-Utf8NoBom (Join-Path $Root 'web\package.json') $webPackage
    Write-Utf8NoBom (Join-Path $Root 'apps\desktop\package.json') $desktopPackage
    Write-Utf8NoBom (Join-Path $Root 'scripts\tests\fixtures\windows-update-e2e-typescript\package.json') $typescriptPackage
    Write-Utf8NoBom (Join-Path $Root 'scripts\tests\fixtures\windows-update-e2e-typescript\bin.cjs') $toolBin
    Write-Utf8NoBom (Join-Path $Root 'scripts\tests\fixtures\windows-update-e2e-vite\package.json') $vitePackage
    Write-Utf8NoBom (Join-Path $Root 'scripts\tests\fixtures\windows-update-e2e-vite\bin.cjs') $toolBin
    Write-Utf8NoBom (Join-Path $Root 'scripts\tests\fixtures\windows-update-e2e-node.cjs') $nodeFixture
    foreach ($relative in @(
        'package-lock.json', 'ui-tui\package-lock.json', 'web\package-lock.json',
        'apps\desktop\package-lock.json'
    )) {
        $candidate = Join-Path $Root $relative
        if (Test-Path -LiteralPath $candidate) {
            Remove-Item -LiteralPath $candidate -Force
        }
    }
}

$consumerPid = 0
$foreignGatewayProcess = $null
$foreignGatewayStartedAt = 0L
$managedPython = $null
$managedGatewayStopped = $false
$failed = $false
try {
    [System.IO.Directory]::CreateDirectory($suiteRoot) | Out-Null
    [System.IO.Directory]::CreateDirectory($hermesTestHome) | Out-Null
    Write-Utf8NoBom (Join-Path $hermesTestHome 'config.yaml') "updates:`n  refresh_cua_driver: false`n"

    # Hermetic process state: every production path resolves only this home.
    $env:HERMES_HOME = $hermesTestHome
    Remove-Item Env:HERMES_PROFILE -ErrorAction SilentlyContinue
    $env:HERMES_DESKTOP_UPDATE_TEST = '1'
    Remove-Item Env:HERMES_INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
    $env:GIT_CONFIG_GLOBAL = 'NUL'
    $env:GIT_CONFIG_SYSTEM = 'NUL'
    $env:GIT_CONFIG_NOSYSTEM = '1'
    $env:UV_CACHE_DIR = Join-Path $suiteRoot 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $suiteRoot 'uv-python'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    $env:npm_config_cache = Join-Path $suiteRoot 'npm-cache'
    $env:CI = '1'

    Write-Host "Preparing disposable Git topology under $suiteRoot"
    $null = Invoke-Git '' @('-c', 'core.longpaths=true', 'clone', '--no-hardlinks', '--no-checkout', $repoRoot, $authorRoot)
    $null = Invoke-Git $authorRoot @('config', 'core.longpaths', 'true')
    $null = Invoke-Git $authorRoot @('checkout', '--detach', $sourceSha)
    $null = Invoke-Git $authorRoot @('switch', '-c', $branch)
    $null = Invoke-Git $authorRoot @('config', 'user.name', 'Hermes Updater E2E')
    $null = Invoke-Git $authorRoot @('config', 'user.email', 'updater-e2e@example.invalid')

    # A local integration run must exercise the exact updater implementation in
    # the caller's working tree, including an uncommitted fix under test. Keep
    # this an explicit allowlist: recursive copies would leak unrelated caller
    # dirt into the disposable Git history. In clean CI every file matches HEAD
    # and this remains a no-op.
    $productionFilesUnderTest = @(
        'gateway/status.py',
        'hermes_cli/_scan_venv_blockers.py',
        'hermes_cli/gateway.py',
        'hermes_cli/gateway_windows.py',
        'hermes_cli/main.py',
        'hermes_cli/subcommands/gui.py',
        'hermes_cli/subcommands/update.py',
        'hermes_cli/update_cmd.py',
        'hermes_cli/update_deferred_gateway.py',
        'hermes_cli/update_lock.py',
        'hermes_cli/update_quiesce.py',
        'hermes_cli/update_readiness.py',
        'hermes_cli/update_receipt.py',
        'hermes_cli/update_transaction.py',
        'hermes_mcp_update_gate.py',
        'scripts/desktop-update.ps1'
    )
    foreach ($relativePath in $productionFilesUnderTest) {
        $sourcePath = Join-Path $repoRoot $relativePath
        $seedPath = Join-Path $authorRoot $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Updater production seed is missing: $relativePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination $seedPath -Force
        Assert-Equal (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash `
            (Get-FileHash -LiteralPath $seedPath -Algorithm SHA256).Hash `
            "seeded exact caller bytes for $relativePath"
    }
    $null = Invoke-Git $authorRoot (@('add', '--') + $productionFilesUnderTest)
    $seededDiff = Invoke-Git $authorRoot @('diff', '--cached', '--name-only', '--')
    $seededFiles = @()
    if (-not [string]::IsNullOrWhiteSpace($seededDiff)) {
        $seededFiles = @($seededDiff -split "`r?`n" | Where-Object { $_ })
    }
    $unexpectedSeededFiles = @(
        $seededFiles | Where-Object { $productionFilesUnderTest -notcontains $_ }
    )
    Assert-True ($unexpectedSeededFiles.Count -eq 0) `
        'dirty seed contains only the explicit updater production allowlist'
    if ($seededFiles.Count -gt 0) {
        $null = Invoke-Git $authorRoot @('commit', '-m', 'test: seed current updater implementation')
    }
    $baseSha = Invoke-Git $authorRoot @('rev-parse', 'HEAD')
    Assert-True ($baseSha -match '^[0-9a-f]{40}$') 'base commit has a full Git identity'

    $null = Invoke-Git '' @('init', '--bare', $remoteRoot)
    $null = Invoke-Git $authorRoot @('remote', 'set-url', 'origin', $remoteRoot)
    $null = Invoke-Git $authorRoot @('push', '-u', 'origin', $branch)
    $null = Invoke-Git '' @('-c', 'core.longpaths=true', 'clone', '--branch', $branch, '--single-branch', $remoteRoot, $installRoot)
    $null = Invoke-Git $installRoot @('config', 'core.longpaths', 'true')
    Assert-Equal $baseSha (Invoke-Git $installRoot @('rev-parse', 'HEAD')) 'managed checkout starts at the exact seeded updater base'

    Write-Host 'Copying the seed venv into the disposable managed checkout'
    $venvRoot = Join-Path $installRoot 'venv'
    & robocopy.exe $SeedVenv $venvRoot /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    $copyCode = $LASTEXITCODE
    if ($copyCode -ge 8) { throw "Seed venv copy failed (robocopy $copyCode)." }
    $global:LASTEXITCODE = 0
    $managedPython = Join-Path $venvRoot 'Scripts\python.exe'
    Assert-True (Test-Path -LiteralPath $managedPython -PathType Leaf) 'copied managed Python exists'
    $managedVenvConfig = Join-Path $venvRoot 'pyvenv.cfg'
    $homeOnlyConfigLines = @("home = $baseHome") + @($seedVenvMetadataLines)
    Write-Utf8NoBom $managedVenvConfig (($homeOnlyConfigLines -join "`n") + "`n")
    $managedVenvConfigLines = [System.IO.File]::ReadAllLines($managedVenvConfig, [System.Text.Encoding]::UTF8)
    Assert-Equal 1 (@($managedVenvConfigLines | Where-Object { $_ -match '^\s*home\s*=' }).Count) `
        'disposable pyvenv.cfg has exactly one home key'
    Assert-Equal 0 (@($managedVenvConfigLines | Where-Object { $_ -match '^\s*executable\s*=' }).Count) `
        'disposable pyvenv.cfg uses the uv home-only layout'
    Assert-Equal $seedVenvConfigHash (Get-FileHash -LiteralPath $seedVenvConfig -Algorithm SHA256).Hash `
        'seed pyvenv.cfg remains byte-for-byte unchanged'

    # Keep uv fully private.  A fresh stamp prevents an irrelevant network
    # self-update while the production updater still runs its runtime probe.
    $managedUv = Join-Path $hermesTestHome 'bin\uv.exe'
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $managedUv)) | Out-Null
    Copy-Item -LiteralPath $uvSource -Destination $managedUv
    $uvStamp = Join-Path $hermesTestHome 'cache\.uv_self_update_stamp'
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $uvStamp)) | Out-Null
    [System.IO.File]::WriteAllText($uvStamp, 'e2e')

    # The copied venv's editable pointer names the source worktree. Rebind it
    # to the disposable install before invoking any production command.
    Push-Location -LiteralPath $installRoot
    try {
        & $uvSource pip install --python $managedPython --no-deps -e $installRoot | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Could not bind the copied venv to the disposable checkout.' }
        $importRoot = (& $managedPython -c "from pathlib import Path; import hermes_cli.main; print(Path(hermes_cli.main.__file__).resolve().parents[1])").Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Disposable Hermes import probe failed.' }
    } finally {
        Pop-Location
    }
    Assert-Equal ([System.IO.Path]::GetFullPath($installRoot).TrimEnd('\')) ([System.IO.Path]::GetFullPath($importRoot).TrimEnd('\')) 'managed interpreter imports the disposable checkout'

    # Publish one newer commit only after the managed clone is pinned to the
    # base. Its tiny Node graph exercises real npm/web/Desktop stages without
    # downloading Electron; the packaged artifact is still a valid native PE.
    Write-MinimalNodeFixture $authorRoot
    $lockfileGenerator = @'
import subprocess
import sys

from hermes_cli.main import _resolve_node_runtime_npm
from hermes_constants import with_hermes_node_path

npm = _resolve_node_runtime_npm()
if not npm:
    raise SystemExit("managed npm is unavailable")
result = subprocess.run(
    [
        npm,
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--progress=false",
    ],
    cwd=sys.argv[1],
    env=with_hermes_node_path(),
    check=False,
)
raise SystemExit(result.returncode)
'@
    & $managedPython -c $lockfileGenerator $authorRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not generate the minimal Node lockfile ($LASTEXITCODE)."
    }
    Assert-True (Test-Path -LiteralPath (Join-Path $authorRoot 'package-lock.json') -PathType Leaf) `
        'target fixture contains an authoritative root Node lockfile'
    Write-Utf8NoBom (Join-Path $authorRoot 'WINDOWS_UPDATE_E2E_TARGET.txt') "target-$branch`n"
    $null = Invoke-Git $authorRoot @('add', '--all')
    $null = Invoke-Git $authorRoot @('commit', '-m', 'test: publish updater integration target')
    $targetSha = Invoke-Git $authorRoot @('rev-parse', 'HEAD')
    $null = Invoke-Git $authorRoot @('push', 'origin', $branch)
    Assert-True ($targetSha -match '^[0-9a-f]{40}$') 'target commit has a full Git identity'
    Assert-True ($targetSha -ne $baseSha) 'target commit advances the managed checkout'

    $foreignGatewayProcess = Start-ForeignGatewayStub
    $foreignGatewayStartedAt = $foreignGatewayProcess.StartTime.ToUniversalTime().Ticks

    Compile-DesktopConsumer $relaunchExe
    $leaseFixture = [System.IO.File]::ReadAllText(
        (Join-Path $repoRoot 'scripts\tests\fixtures\desktop-update-bridge-lease.json')
    ) | ConvertFrom-Json
    $now = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $leaseFixture.lease_id = $leaseId
    $leaseFixture.owner_pid = $PID
    $leaseFixture.created_at = $now
    $leaseFixture.expires_at = $now + 1200
    $leaseFixture.handoff_grace_until = $now + 90
    $leaseFixture.install_root = [System.IO.Path]::GetFullPath($installRoot).TrimEnd('\')
    Write-Utf8NoBom $leasePath ($leaseFixture | ConvertTo-Json -Compress)

    $handoffScript = Join-Path $installRoot 'scripts\desktop-update.ps1'
    $managedVenvConfigLines = [System.IO.File]::ReadAllLines($managedVenvConfig, [System.Text.Encoding]::UTF8)
    Assert-Equal 0 (@($managedVenvConfigLines | Where-Object { $_ -match '^\s*executable\s*=' }).Count) `
        'full transaction starts from a home-only disposable pyvenv.cfg'
    Write-Host 'Invoking the production Desktop updater transaction'
    & $powerShellExe -NoProfile -ExecutionPolicy Bypass -File $handoffScript `
        -InstallRoot $installRoot -Branch $branch -DesktopPid 0 `
        -RelaunchExe $relaunchExe -BridgeLeaseId $leaseId -NoUi | Out-Host
    $handoffCode = $LASTEXITCODE

    if (Test-Path -LiteralPath $consumerPidPath) {
        $consumerPid = [int]([System.IO.File]::ReadAllText($consumerPidPath).Trim())
    }
    Assert-Equal 0 $handoffCode 'production Desktop handoff exits successfully'
    Assert-True (Test-Path -LiteralPath $resultPath -PathType Leaf) 'terminal handoff result exists'
    Assert-True (Test-Path -LiteralPath $receiptPath -PathType Leaf) 'private update receipt exists'
    Assert-True (Test-Path -LiteralPath $consumerCapturePath -PathType Leaf) 'relaunched Desktop observed pending result'

    $installedHead = Invoke-Git $installRoot @('rev-parse', 'HEAD')
    $receipt = [System.IO.File]::ReadAllText($receiptPath) | ConvertFrom-Json
    $result = [System.IO.File]::ReadAllText($resultPath) | ConvertFrom-Json
    $pending = [System.IO.File]::ReadAllText($consumerCapturePath) | ConvertFrom-Json
    Assert-Equal $targetSha $installedHead 'managed checkout HEAD equals the published target'
    Assert-Equal $targetSha $receipt.target_sha 'receipt target SHA equals the published target'
    Assert-Equal $targetSha $receipt.resulting_head 'receipt resulting HEAD equals the installed HEAD'
    Assert-Equal $targetSha $result.receipt.resulting_head 'terminal result embeds the same receipt identity'
    Assert-Equal $result.invocation_id $receipt.invocation_id 'result and receipt share one invocation'
    Assert-Equal $leaseId $receipt.lease_id 'receipt is bound to the handed-off lease capability'
    Assert-Equal $branch $receipt.branch 'receipt records the exact update branch'
    Assert-Equal 'git' $receipt.mode 'receipt records a Git update'
    Assert-True ($receipt.success -eq $true) 'receipt reports successful mutation health'
    Assert-True ($receipt.gateway_resume_deferred -eq $true) 'receipt records deferred gateway recovery'

    Assert-Equal 'pending' $pending.state 'relaunched Desktop first observes pending handoff state'
    Assert-Equal $targetSha $pending.receipt.resulting_head 'pending state is receipt-correlated'
    Assert-Equal 'complete' $result.state 'terminal result is complete'
    Assert-True ($result.ok -eq $true) 'terminal result reports success'
    Assert-Equal 0 $result.exit_code 'terminal result carries exit code zero'
    Assert-Equal 'acknowledged' $result.relaunch.state 'relaunch readiness is acknowledged'
    Assert-Equal $targetSha $result.desktop.build_id 'Desktop acknowledgment uses installed build identity'
    Assert-Equal 'install-stamp' $result.desktop.build_source 'Desktop acknowledgment cites the install stamp'
    Assert-True ($result.desktop.backend_ready -eq $true) 'Desktop acknowledgment proves backend readiness'
    Assert-True ([int64]$result.relaunch.pid -gt 0) 'result records the exact relaunched process'
    Assert-Equal ([System.IO.Path]::GetFullPath($relaunchExe)) ([System.IO.Path]::GetFullPath([string]$result.relaunch.executable)) 'result records the exact relaunch executable'
    Assert-True (-not $foreignGatewayProcess.HasExited) 'foreign-root gateway survives the deferred updater transaction'
    Assert-Equal $foreignGatewayStartedAt $foreignGatewayProcess.StartTime.ToUniversalTime().Ticks 'foreign gateway retains the same process generation'

    # Retire the controlled foreign process before invoking gateway stop. The
    # production stop command may reap true current-profile orphans, so the
    # foreign-root survival witness must no longer be discoverable at cleanup.
    Write-Utf8NoBom $foreignGatewayStop 'stop'
    Assert-True ($foreignGatewayProcess.WaitForExit(10000)) 'controlled foreign gateway exits through its sentinel'
    $foreignGatewayProcess.Dispose()
    $foreignGatewayProcess = $null

    Stop-DisposableManagedGateway -Python $managedPython -Required
    $managedGatewayStopped = $true

    Assert-True (-not (Test-Path -LiteralPath $markerPath)) 'update marker is removed'
    Assert-True (-not (Test-Path -LiteralPath $leasePath)) 'bridge-quiesce lease is removed'
    Assert-True (@(Get-ChildItem -LiteralPath $hermesTestHome -Filter '.hermes-update-in-progress.cas-*' -File -ErrorAction SilentlyContinue).Count -eq 0) 'update-marker CAS artifacts are absent'
    Assert-True (@(Get-ChildItem -LiteralPath $hermesTestHome -Filter '.hermes-venv-quiesce.cas-*' -File -ErrorAction SilentlyContinue).Count -eq 0) 'lease CAS artifacts are absent'
    Assert-True (@(Get-ChildItem -LiteralPath $hermesTestHome -Filter '.hermes-gateway-resume-*.json' -File -ErrorAction SilentlyContinue).Count -eq 0) 'pending gateway plans are absent'
    Assert-True (@(Get-ChildItem -LiteralPath $hermesTestHome -Filter '.hermes-gateway-resume-*.consume-*' -File -ErrorAction SilentlyContinue).Count -eq 0) 'gateway-plan consume artifacts are absent'

    if ($consumerPid -gt 0) {
        Wait-Process -Id $consumerPid -Timeout 15 -ErrorAction SilentlyContinue
    }
    Write-Host 'PASS: native Windows Desktop updater integration' -ForegroundColor Green
} catch {
    $failed = $true
    Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red
    if (Test-Path -LiteralPath $handoffLog -PathType Leaf) {
        Write-Host '--- desktop-update-handoff.log (tail) ---'
        Get-Content -LiteralPath $handoffLog -Tail 120
    }
    throw
} finally {
    if ($foreignGatewayProcess) {
        try {
            Write-Utf8NoBom $foreignGatewayStop 'stop'
            if (-not $foreignGatewayProcess.WaitForExit(10000)) {
                $foreignGatewayProcess.Kill($true)
                $foreignGatewayProcess.WaitForExit(5000) | Out-Null
            }
        } finally {
            $foreignGatewayProcess.Dispose()
        }
    }
    if (-not $managedGatewayStopped) {
        try {
            Stop-DisposableManagedGateway -Python $managedPython
        } catch {
            Write-Warning "Could not stop disposable managed gateway: $($_.Exception.Message)"
        }
    }
    # The test consumer exits on its own after at most 20 seconds. Never kill a
    # bare PID here: the process can exit between observation and cleanup and
    # Windows may reuse the numeric ID for an unrelated process. Successful
    # runs already wait above; failed runs preserve the workspace and let the
    # bounded consumer finish naturally.
    foreach ($entry in $savedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$entry.Key, $entry.Value)
    }
    if ($KeepTemp -or $failed) {
        Write-Host "Preserved updater integration workspace: $suiteRoot"
    } elseif ($suiteRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $suiteRoot)) {
        try {
            Remove-Item -LiteralPath ('\\?\' + $suiteRoot) -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning "Could not remove updater integration workspace: $suiteRoot ($($_.Exception.Message))"
        }
    }
}
