# Contract tests for scripts/desktop-update.ps1.
#
# These tests run the handoff in a temporary install with a tiny fake
# hermes.exe. No real checkout, venv, Desktop process, or update is touched.

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$handoffScript = Join-Path $repoRoot 'scripts\desktop-update.ps1'
$leaseFixture = Join-Path $repoRoot 'scripts\tests\fixtures\desktop-update-bridge-lease.json'
$powershellExe = (Get-Process -Id $PID).Path
$failures = 0

function Assert-Equal {
    param($Expected, $Actual, [string]$Label)
    if ($Expected -ne $Actual) {
        Write-Host "FAIL: $Label (expected '$Expected', got '$Actual')" -ForegroundColor Red
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}

function Assert-True {
    param($Condition, [string]$Label)
    if (-not $Condition) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}

function Compile-TestExecutable([string]$Destination, [string]$Source, [string]$Label) {
    if ($PSVersionTable.PSEdition -eq 'Core') {
        # PowerShell 7's Add-Type cannot emit an executable. Use the inbox .NET
        # Framework compiler so the same behavior fixture runs under PS5 + pwsh.
        $sourcePath = [System.IO.Path]::ChangeExtension($Destination, '.cs')
        [System.IO.File]::WriteAllText($sourcePath, $Source)
        $windowsDirectory = [Environment]::GetFolderPath('Windows')
        $compiler = Join-Path $windowsDirectory 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        & $compiler /nologo /target:exe "/out:$Destination" $sourcePath
        $compileCode = $LASTEXITCODE
        Remove-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue
        if ($compileCode -ne 0 -or -not (Test-Path -LiteralPath $Destination)) {
            throw "$Label compilation failed with exit $compileCode"
        }
    } else {
        Add-Type -TypeDefinition $Source -Language CSharp -OutputAssembly $Destination -OutputType ConsoleApplication
    }
}

function New-FakeHermes([string]$Destination) {
    $source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

public static class FakeHermes {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsProcessInJob(IntPtr processHandle, IntPtr jobHandle, out bool result);

    static string Arg(string[] args, string name) {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    static string Escape(string value) {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    static void ReplaceLease(string path, string raw, string tag) {
        var temporary = path + ".cas-shadow-" + Process.GetCurrentProcess().Id + "-" + Guid.NewGuid().ToString("N");
        var backup = path + ".cas-previous-" + Process.GetCurrentProcess().Id + "-" + Guid.NewGuid().ToString("N");
        File.WriteAllText(temporary, raw);
        File.Replace(temporary, path, backup);
        File.Delete(backup);
    }

    static string Fingerprint(string leaseId) {
        using (var sha = SHA256.Create()) {
            return String.Concat(sha.ComputeHash(Encoding.UTF8.GetBytes(leaseId)).Select(b => b.ToString("x2")));
        }
    }

    static void WritePlan(string home, string root, string invocationId, string leaseId) {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var plan = "{\"schema_version\":1,\"invocation_id\":\"" + invocationId +
            "\",\"lease_fingerprint\":\"" + Fingerprint(leaseId) +
            "\",\"install_root\":\"" + Escape(root) +
            "\",\"created_at\":" + now + ",\"expires_at\":" + (now + 3960) +
            ",\"profiles\":[],\"cold_start_if_installed\":true,\"auth\":\"" + new string('b', 64) + "\"}";
        File.WriteAllText(Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".json"), plan);
    }

    static int Resume(string[] args, string leasePath, string leaseId, string invocationId, string mode) {
        if (String.IsNullOrEmpty(leasePath) || String.IsNullOrEmpty(leaseId) ||
            String.IsNullOrEmpty(invocationId) || !File.Exists(leasePath)) return 1;
        var home = Path.GetDirectoryName(leasePath);
        var capture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_CAPTURE");
        if (!String.IsNullOrEmpty(capture)) File.WriteAllText(capture, String.Join(" ", args));
        var raw = File.ReadAllText(leasePath);
        var ownerMatch = Regex.Match(raw, "\"owner_pid\"\\s*:\\s*(\\d+)");
        var parentPid = ownerMatch.Success ? Int32.Parse(ownerMatch.Groups[1].Value) : 0;
        raw = Regex.Replace(raw, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + Process.GetCurrentProcess().Id);
        ReplaceLease(leasePath, raw, "resume-adopt");
        var framePid = mode == "resume-wrong-frame" ? Process.GetCurrentProcess().Id + 1 : Process.GetCurrentProcess().Id;
        var frame = "{\"schema_version\":1,\"event\":\"deferred-gateway-lease-adopted\",\"invocation_id\":\"" +
            invocationId + "\",\"owner_pid\":" + framePid + "}";
        Console.WriteLine(frame);
        Console.Out.Flush();
        if (mode == "resume-duplicate-frame") Console.WriteLine(frame);
        if (mode == "resume-fail") {
            if (parentPid > 0 && File.Exists(leasePath)) {
                var returned = Regex.Replace(File.ReadAllText(leasePath), "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentPid);
                ReplaceLease(leasePath, returned, "resume-return");
            }
            Console.Error.WriteLine("simulated gateway recovery failure");
            return 1;
        }
        if (mode == "resume-wrong-frame" || mode == "resume-duplicate-frame") {
            Thread.Sleep(30000);
            return 0;
        }
        var pending = Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".json");
        var completed = Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".completed");
        if (!File.Exists(pending)) return 1;
        File.Move(pending, completed);
        if (File.Exists(leasePath)) File.Delete(leasePath);
        Console.WriteLine("Deferred gateway fleet resumed.");
        return 0;
    }

    public static int Main(string[] args) {
        if (args.Contains("--preflight")) {
            var output = Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_OUTPUT") ?? "";
            var argsCapture = Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_ARGS_CAPTURE");
            if (!String.IsNullOrEmpty(argsCapture))
                File.AppendAllText(
                    argsCapture,
                    String.Join(" ", args) + "\t" + Directory.GetCurrentDirectory() + Environment.NewLine
                );
            var preflightLeasePath = Environment.GetEnvironmentVariable("HERMES_TEST_LEASE_PATH");
            var capture = Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_LEASE_CAPTURE");
            if (!String.IsNullOrEmpty(preflightLeasePath) && !String.IsNullOrEmpty(capture) &&
                File.Exists(preflightLeasePath) && !File.Exists(capture)) File.Copy(preflightLeasePath, capture, true);
            var home = String.IsNullOrEmpty(preflightLeasePath) ? null : Path.GetDirectoryName(preflightLeasePath);
            var receipt = String.IsNullOrEmpty(home) ? null : Path.Combine(home, ".hermes-update-receipt.json");
            if (!String.IsNullOrEmpty(receipt) && File.Exists(receipt))
                output = output.Replace("\"last_update_receipt\":null", "\"last_update_receipt\":" + File.ReadAllText(receipt));
            Console.Write(output);
            Console.Error.Write(Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_STDERR") ?? "");
            int code;
            return int.TryParse(Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_CODE"), out code) ? code : 0;
        }

        var leasePath = Environment.GetEnvironmentVariable("HERMES_TEST_LEASE_PATH");
        var capturePath = Environment.GetEnvironmentVariable("HERMES_TEST_LEASE_CAPTURE");
        var leaseId = Arg(args, "--bridge-lease-id");
        var invocationId = Arg(args, "--invocation-id");
        var mode = Environment.GetEnvironmentVariable("HERMES_TEST_UPDATE_MODE") ?? "normal";
        if (args.Contains("--resume-deferred-gateway") && mode == "resume-trampoline" &&
            Process.GetCurrentProcess().MainModule.FileName.IndexOf(
                "\\venv\\Scripts\\", StringComparison.OrdinalIgnoreCase
            ) >= 0) {
            var redirectorCapture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_REDIRECTOR_CAPTURE");
            if (!String.IsNullOrEmpty(redirectorCapture))
                File.WriteAllText(redirectorCapture, Process.GetCurrentProcess().MainModule.FileName);
            return 77;
        }
        if (args.Contains("--resume-deferred-gateway"))
            return Resume(args, leasePath, leaseId, invocationId, mode);
        if (args.Contains("desktop")) {
            var buildCapture = Environment.GetEnvironmentVariable("HERMES_TEST_BUILD_SHA_CAPTURE");
            if (!String.IsNullOrEmpty(buildCapture))
                File.WriteAllText(buildCapture, Environment.GetEnvironmentVariable("GITHUB_SHA") ?? "");
            Console.WriteLine("Desktop build complete.");
            return 0;
        }
        var topologyCapture = Environment.GetEnvironmentVariable("HERMES_TEST_TOPOLOGY_CAPTURE");
        var parentLeaseOwnerPid = 0;
        if (!String.IsNullOrEmpty(topologyCapture)) {
            bool inJob;
            var membershipKnown = IsProcessInJob(
                Process.GetCurrentProcess().Handle,
                IntPtr.Zero,
                out inJob
            );
            File.WriteAllText(
                topologyCapture,
                (Environment.GetEnvironmentVariable("HERMES_INTERNAL_UPDATE_WRAPPER_PID") ?? "") +
                Environment.NewLine + Process.GetCurrentProcess().Id + Environment.NewLine +
                (membershipKnown ? (inJob ? "in-job" : "not-in-job") : "unreadable") + Environment.NewLine
            );
        }
        if (!String.IsNullOrEmpty(leasePath) && !String.IsNullOrEmpty(leaseId) && File.Exists(leasePath)) {
            var raw = File.ReadAllText(leasePath);
            var ownerMatch = Regex.Match(raw, "\"owner_pid\"\\s*:\\s*(\\d+)");
            if (ownerMatch.Success) Int32.TryParse(ownerMatch.Groups[1].Value, out parentLeaseOwnerPid);
            raw = Regex.Replace(raw, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + Process.GetCurrentProcess().Id);
            ReplaceLease(leasePath, raw, "update-adopt");
            Thread.Sleep(750);
        }
        if (!String.IsNullOrEmpty(leaseId) && !String.IsNullOrEmpty(invocationId) && !String.IsNullOrEmpty(leasePath)) {
            var home = Path.GetDirectoryName(leasePath);
            WritePlan(home, Path.Combine(home, "hermes-agent"), invocationId, leaseId);
        }
        var sentinel = Environment.GetEnvironmentVariable("HERMES_TEST_MUTATION_SENTINEL");
        if (!String.IsNullOrEmpty(sentinel)) File.WriteAllText(sentinel, String.Join(" ", args));
        if (!String.IsNullOrEmpty(leasePath) && !String.IsNullOrEmpty(capturePath) && File.Exists(leasePath))
            File.Copy(leasePath, capturePath, true);
        if (mode == "foreign-rewrite" && !String.IsNullOrEmpty(leasePath) && File.Exists(leasePath)) {
            var raw = File.ReadAllText(leasePath);
            raw = Regex.Replace(raw, "\"lease_id\"\\s*:\\s*\"[^\"]+\"", "\"lease_id\":\"foreign-lease-0123456789abcdef\"");
            ReplaceLease(leasePath, raw, "foreign");
            Thread.Sleep(30000);
            return 0;
        }
        if (mode == "stderr-heavy") Console.Error.Write(new string('x', 2 * 1024 * 1024));
        else if (mode == "silent") Thread.Sleep(1200);

        if (!String.IsNullOrEmpty(leaseId) && !String.IsNullOrEmpty(leasePath)) {
            var home = Path.GetDirectoryName(leasePath);
            var root = Path.Combine(home, "hermes-agent").Replace("\\", "\\\\");
            var branchIndex = Array.IndexOf(args, "--branch");
            var branch = branchIndex >= 0 && branchIndex + 1 < args.Length ? args[branchIndex + 1] : "main";
            var archive = mode == "archive";
            var sha = new string('a', 40);
            var archiveSha = new string('b', 64);
            var identity = archive
                ? "\"mode\":\"archive\",\"remote\":null,\"target_ref\":null,\"target_sha\":null,\"resulting_head\":null,\"archive_sha\":\"" + archiveSha + "\""
                : "\"mode\":\"git\",\"remote\":\"origin\",\"target_ref\":\"refs/remotes/origin/" + branch + "\",\"target_sha\":\"" + sha + "\",\"resulting_head\":\"" + sha + "\",\"archive_sha\":null";
            var receipt = "{\"schema_version\":1,\"invocation_id\":\"" + invocationId +
                "\",\"lease_id\":\"" + leaseId + "\"," + identity + ",\"root\":\"" + root +
                "\",\"branch\":\"" + branch + "\",\"timestamp\":" +
                DateTimeOffset.UtcNow.ToUnixTimeSeconds() +
                ",\"success\":true,\"gateway_resume_deferred\":true,\"health\":{\"critical_syntax\":true,\"critical_imports\":true,\"dependencies\":true,\"node_dependencies\":true}}";
            File.WriteAllText(Path.Combine(home, ".hermes-update-receipt.json"), receipt);
            if (File.Exists(leasePath) && parentLeaseOwnerPid > 0) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(returned, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentLeaseOwnerPid);
                ReplaceLease(leasePath, returned, "update-return");
            }
        }
        Console.WriteLine("Update complete.");
        return 0;
    }
}
'@
    Compile-TestExecutable $Destination $source 'fake Hermes'
}

function New-FakeDesktop([string]$Destination) {
    $source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading;

public static class FakeDesktop {
    public static int Main() {
        var home = AppDomain.CurrentDomain.BaseDirectory;
        var result = Path.Combine(home, ".hermes-update-result.json");
        var captured = Path.Combine(home, "immediate-consumer-result.json");
        var pid = Path.Combine(home, "immediate-consumer-pid.txt");
        File.WriteAllText(pid, Process.GetCurrentProcess().Id.ToString());
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (DateTime.UtcNow < deadline) {
            foreach (var requestPath in Directory.GetFiles(home, ".hermes-update-relaunch-request-*.json")) {
                var requestRaw = File.ReadAllText(requestPath);
                Func<string, string> requestValue = pattern => {
                    var match = Regex.Match(requestRaw, pattern);
                    return match.Success ? match.Groups[1].Value : "";
                };
                var requestAttempt = requestValue("\\\"attempt_id\\\":\\\"([^\\\"]+)\\\"");
                var requestRoot = requestValue("\\\"root\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                var requestExe = requestValue("\\\"executable\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                long requestedAt;
                if (!String.IsNullOrEmpty(requestAttempt) &&
                    Int64.TryParse(requestValue("\\\"requested_at\\\":(\\d+)"), out requestedAt)) {
                    var selfStartedAt = new DateTimeOffset(Process.GetCurrentProcess().StartTime.ToUniversalTime()).ToUnixTimeSeconds();
                    if (selfStartedAt < requestedAt) {
                        var exitAck = "{\"schema_version\":1,\"attempt_id\":\"" + requestAttempt +
                            "\",\"pid\":" + Process.GetCurrentProcess().Id +
                            ",\"process_started_at\":" + selfStartedAt +
                            ",\"root\":\"" + requestRoot + "\",\"executable\":\"" + requestExe +
                            "\",\"acknowledged_at\":" + DateTimeOffset.UtcNow.ToUnixTimeSeconds() +
                            ",\"action\":\"quit\"}";
                        var exitAckPath = Path.Combine(home, ".hermes-update-relaunch-exit-ack-" +
                            requestAttempt + "-" + Process.GetCurrentProcess().Id + ".json");
                        var exitTemp = exitAckPath + ".tmp-" + Guid.NewGuid().ToString("N");
                        File.WriteAllText(exitTemp, exitAck);
                        if (File.Exists(requestPath) && File.ReadAllText(requestPath) == requestRaw)
                            File.Move(exitTemp, exitAckPath);
                        else
                            File.Delete(exitTemp);
                        return 0;
                    }
                }
            }
            if (File.Exists(result)) {
                var raw = File.ReadAllText(result);
                File.WriteAllText(captured, raw);
                Func<string, string> capture = pattern => {
                    var match = Regex.Match(raw, pattern);
                    return match.Success ? match.Groups[1].Value : "";
                };
                var attempt = capture("\\\"attempt_id\\\":\\\"([^\\\"]+)\\\"");
                var invocation = capture("\\\"invocation_id\\\":\\\"([^\\\"]+)\\\"");
                var lease = capture("\\\"lease_id\\\":\\\"([^\\\"]+)\\\"");
                var root = capture("\\\"root\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                var executable = capture("\\\"executable\\\":\\\"((?:\\\\.|[^\\\"])*)\\\"");
                var started = capture("\\\"process_started_at\\\":(\\d+)");
                var build = capture("\\\"resulting_head\\\":\\\"([0-9a-fA-F]+)\\\"");
                if (String.IsNullOrEmpty(build))
                    build = capture("\\\"archive_sha\\\":\\\"([0-9a-fA-F]+)\\\"");
                if (!String.IsNullOrEmpty(attempt) && !String.IsNullOrEmpty(build)) {
                    var ack = "{\"schema_version\":1,\"attempt_id\":\"" + attempt +
                        "\",\"invocation_id\":\"" + invocation + "\",\"lease_id\":\"" + lease +
                        "\",\"pid\":" + Process.GetCurrentProcess().Id + ",\"process_started_at\":" + started +
                        ",\"root\":\"" + root + "\",\"executable\":\"" + executable +
                        "\",\"build_id\":\"" + build + "\",\"build_source\":\"install-stamp\"" +
                        ",\"backend_ready\":true,\"backend_mode\":\"local\",\"acknowledged_at\":" +
                        DateTimeOffset.UtcNow.ToUnixTimeSeconds() + ",\"error\":null}";
                    var ackPath = Path.Combine(home, ".hermes-update-ack-" + attempt + ".json");
                    var temp = ackPath + ".tmp-" + Guid.NewGuid().ToString("N");
                    File.WriteAllText(temp, ack);
                    File.Move(temp, ackPath);
                    Thread.Sleep(5000);
                    return 0;
                }
                return 13;
            }
            Thread.Sleep(10);
        }
        return 12;
    }
}
'@
    Compile-TestExecutable $Destination $source 'fake Desktop'
}

function New-TestInstall([string]$Tag, [string]$FakeHermes) {
    $testHome = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-desktop-update-test-{0}-{1}" -f $Tag, [Guid]::NewGuid().ToString('N'))
    $root = Join-Path $testHome 'hermes-agent'
    $shimDir = Join-Path $root 'venv\Scripts'
    $baseDir = Join-Path $root 'base-python'
    New-Item -ItemType Directory -Path $shimDir -Force | Out-Null
    New-Item -ItemType Directory -Path $baseDir -Force | Out-Null
    Copy-Item -LiteralPath $FakeHermes -Destination (Join-Path $shimDir 'hermes.exe')
    Copy-Item -LiteralPath $FakeHermes -Destination (Join-Path $shimDir 'python.exe')
    $basePython = Join-Path $baseDir 'python.exe'
    Copy-Item -LiteralPath $FakeHermes -Destination $basePython
    [System.IO.File]::WriteAllText(
        (Join-Path $root 'venv\pyvenv.cfg'),
        "home = $baseDir`nexecutable = $basePython`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    return [pscustomobject]@{
        Home = $testHome
        Root = $root
        Sentinel = Join-Path $testHome 'mutation-sentinel.txt'
        Lease = Join-Path $testHome '.hermes-venv-quiesce'
        LeaseCapture = Join-Path $testHome 'lease-during-mutation.json'
        PreflightLeaseCapture = Join-Path $testHome 'lease-before-mutation.json'
        PreflightArgsCapture = Join-Path $testHome 'preflight-args.txt'
        TopologyCapture = Join-Path $testHome 'contained-process-topology.txt'
        ResumeCapture = Join-Path $testHome 'deferred-resume-args.txt'
        ResumeRedirectorCapture = Join-Path $testHome 'deferred-resume-redirector.txt'
        BuildShaCapture = Join-Path $testHome 'desktop-build-sha.txt'
        UpdateMarker = Join-Path $testHome '.hermes-update-in-progress'
        Result = Join-Path $testHome '.hermes-update-result.json'
    }
}

function Write-TestPyvenvConfig([object]$Install, [string[]]$Lines) {
    [System.IO.File]::WriteAllText(
        (Join-Path $Install.Root 'venv\pyvenv.cfg'),
        (($Lines -join "`n") + "`n"),
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Assert-InvalidPyvenvRecoveryRejected([object]$Install, [int]$Code, [string]$Label) {
    Assert-Equal 13 $Code "$Label exits with failed fleet recovery"
    Assert-True (Test-Path -LiteralPath $Install.Sentinel) "$Label reaches the update before recovery validation"
    Assert-True (-not (Test-Path -LiteralPath $Install.ResumeCapture)) "$Label starts no recovery interpreter"
    Assert-True (-not (Test-Path -LiteralPath $Install.UpdateMarker)) "$Label releases the exact update marker"
    Assert-True (-not (Test-Path -LiteralPath $Install.Lease)) "$Label releases the exact bridge lease"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-update-in-progress.cas-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no update-marker CAS artifacts"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-venv-quiesce.cas-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no lease CAS artifacts"
    Assert-Equal 1 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.json' -File -ErrorAction SilentlyContinue).Count) "$Label preserves one exact pending recovery plan"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.completed' -File -ErrorAction SilentlyContinue).Count) "$Label does not claim the recovery plan was completed"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.consume-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no plan-consume artifacts"
}

function Write-TestLease([object]$Install, [string]$LeaseId) {
    $fixture = [System.IO.File]::ReadAllText($leaseFixture) | ConvertFrom-Json
    $now = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $fixture.lease_id = $LeaseId
    $fixture.owner_pid = $PID
    $fixture.created_at = $now
    $fixture.expires_at = $now + 1200
    $fixture.handoff_grace_until = $now + 90
    $fixture.install_root = [System.IO.Path]::GetFullPath($Install.Root).TrimEnd([char[]]@('\', '/'))
    $json = $fixture | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($Install.Lease, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function New-PreflightJson([object]$Install, [bool]$Ok, [bool]$Ready) {
    $payload = [ordered]@{
        schema_version = 1
        mode = 'preflight'
        ok = $Ok
        ready = $Ready
        blocked = (-not $Ready)
        reason = if ($Ready) { $null } elseif ($Ok) { 'venv-blocked' } else { 'probe-failed' }
        root = [System.IO.Path]::GetFullPath($Install.Root).TrimEnd([char[]]@('\', '/'))
        venv = [System.IO.Path]::GetFullPath((Join-Path $Install.Root 'venv')).TrimEnd([char[]]@('\', '/'))
        processes = @()
        mcp_bridges = @()
        pausable_gateways = 0
        pausable_gateway_processes = @()
        git = $null
        last_update_receipt = $null
        lease = $null
        actions = @()
        error = if ($Ok) { $null } else { [ordered]@{ code = 'probe-failed'; message = 'probe failed' } }
    }
    return $payload | ConvertTo-Json -Compress -Depth 5
}

function Invoke-TestHandoff(
    [object]$Install,
    [string]$PreflightOutput,
    [int]$PreflightCode,
    [string]$PreflightStderr = '',
    [string]$BridgeLeaseId = '',
    [string]$UpdateMode = 'normal',
    [string]$RelaunchExe = ''
) {
    $oldOutput = $env:HERMES_TEST_PREFLIGHT_OUTPUT
    $oldCode = $env:HERMES_TEST_PREFLIGHT_CODE
    $oldStderr = $env:HERMES_TEST_PREFLIGHT_STDERR
    $oldSentinel = $env:HERMES_TEST_MUTATION_SENTINEL
    $oldLeasePath = $env:HERMES_TEST_LEASE_PATH
    $oldLeaseCapture = $env:HERMES_TEST_LEASE_CAPTURE
    $oldPreflightLeaseCapture = $env:HERMES_TEST_PREFLIGHT_LEASE_CAPTURE
    $oldPreflightArgsCapture = $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE
    $oldTopologyCapture = $env:HERMES_TEST_TOPOLOGY_CAPTURE
    $oldResumeCapture = $env:HERMES_TEST_RESUME_CAPTURE
    $oldResumeRedirectorCapture = $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE
    $oldBuildShaCapture = $env:HERMES_TEST_BUILD_SHA_CAPTURE
    $oldUpdateMode = $env:HERMES_TEST_UPDATE_MODE
    $oldTestMode = $env:HERMES_DESKTOP_UPDATE_TEST
    $oldPublishFail = $env:HERMES_TEST_RESULT_PUBLISH_FAIL
    try {
        $env:HERMES_TEST_PREFLIGHT_OUTPUT = $PreflightOutput
        $env:HERMES_TEST_PREFLIGHT_CODE = "$PreflightCode"
        $env:HERMES_TEST_PREFLIGHT_STDERR = $PreflightStderr
        $env:HERMES_TEST_MUTATION_SENTINEL = $Install.Sentinel
        $env:HERMES_TEST_LEASE_PATH = $Install.Lease
        $env:HERMES_TEST_LEASE_CAPTURE = $Install.LeaseCapture
        $env:HERMES_TEST_PREFLIGHT_LEASE_CAPTURE = $Install.PreflightLeaseCapture
        $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE = $Install.PreflightArgsCapture
        $env:HERMES_TEST_TOPOLOGY_CAPTURE = $Install.TopologyCapture
        $env:HERMES_TEST_RESUME_CAPTURE = $Install.ResumeCapture
        $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE = $Install.ResumeRedirectorCapture
        $env:HERMES_TEST_BUILD_SHA_CAPTURE = $Install.BuildShaCapture
        $env:HERMES_TEST_UPDATE_MODE = $UpdateMode
        $env:HERMES_DESKTOP_UPDATE_TEST = '1'
        if ($UpdateMode -eq 'result-publish-fail') {
            $env:HERMES_TEST_RESULT_PUBLISH_FAIL = '1'
        } else {
            Remove-Item Env:HERMES_TEST_RESULT_PUBLISH_FAIL -ErrorAction SilentlyContinue
        }
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $handoffScript,
            '-InstallRoot', $Install.Root, '-DesktopPid', '0', '-NoUi'
        )
        if ($BridgeLeaseId) { $arguments += @('-BridgeLeaseId', $BridgeLeaseId) }
        if ($RelaunchExe) { $arguments += @('-RelaunchExe', $RelaunchExe) }
        & $powershellExe @arguments | Out-Null
        return $LASTEXITCODE
    } finally {
        $env:HERMES_TEST_PREFLIGHT_OUTPUT = $oldOutput
        $env:HERMES_TEST_PREFLIGHT_CODE = $oldCode
        $env:HERMES_TEST_PREFLIGHT_STDERR = $oldStderr
        $env:HERMES_TEST_MUTATION_SENTINEL = $oldSentinel
        $env:HERMES_TEST_LEASE_PATH = $oldLeasePath
        $env:HERMES_TEST_LEASE_CAPTURE = $oldLeaseCapture
        $env:HERMES_TEST_PREFLIGHT_LEASE_CAPTURE = $oldPreflightLeaseCapture
        $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE = $oldPreflightArgsCapture
        $env:HERMES_TEST_TOPOLOGY_CAPTURE = $oldTopologyCapture
        $env:HERMES_TEST_RESUME_CAPTURE = $oldResumeCapture
        $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE = $oldResumeRedirectorCapture
        $env:HERMES_TEST_BUILD_SHA_CAPTURE = $oldBuildShaCapture
        $env:HERMES_TEST_UPDATE_MODE = $oldUpdateMode
        $env:HERMES_DESKTOP_UPDATE_TEST = $oldTestMode
        $env:HERMES_TEST_RESULT_PUBLISH_FAIL = $oldPublishFail
    }
}

function Invoke-LeasedTestHandoff(
    [object]$Install,
    [string]$PreflightOutput,
    [int]$PreflightCode,
    [string]$PreflightStderr = ''
) {
    $leaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $Install $leaseId
    return Invoke-TestHandoff $Install $PreflightOutput $PreflightCode $PreflightStderr $leaseId
}

$suiteRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-desktop-update-suite-{0}" -f [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $suiteRoot -Force | Out-Null
$fakeHermes = Join-Path $suiteRoot 'fake-hermes.exe'
$invalidVenvHomes = @()

try {
    New-FakeHermes $fakeHermes
    $fakeDesktopTemplate = Join-Path $suiteRoot 'fake-desktop-template.exe'
    New-FakeDesktop $fakeDesktopTemplate

    $fixture = [System.IO.File]::ReadAllText($leaseFixture) | ConvertFrom-Json
    $fixtureFields = @($fixture.PSObject.Properties | ForEach-Object { $_.Name })
    Assert-Equal 7 $fixtureFields.Count 'shared lease fixture has exactly seven v1 fields'
    foreach ($field in @('schema_version', 'lease_id', 'owner_pid', 'created_at', 'expires_at', 'handoff_grace_until', 'install_root')) {
        Assert-True ($fixtureFields -contains $field) "shared lease fixture contains $field"
    }

    $profileGlobal = Join-Path $suiteRoot 'profile-global'
    $profileHome = Join-Path $profileGlobal 'profiles\research'
    $profileRoot = Join-Path $profileHome 'hermes-agent'
    $profileShim = Join-Path $profileRoot 'venv\Scripts'
    New-Item -ItemType Directory -Path $profileShim -Force | Out-Null
    Copy-Item -LiteralPath $fakeHermes -Destination (Join-Path $profileShim 'hermes.exe')
    Copy-Item -LiteralPath $fakeHermes -Destination (Join-Path $profileShim 'python.exe')
    $profileInstall = [pscustomobject]@{
        Home = $profileHome
        Root = $profileRoot
        Sentinel = Join-Path $profileHome 'mutation-sentinel.txt'
        Lease = Join-Path $profileHome '.hermes-venv-quiesce'
        LeaseCapture = Join-Path $profileHome 'lease-during-mutation.json'
        PreflightLeaseCapture = Join-Path $profileHome 'lease-before-mutation.json'
        UpdateMarker = Join-Path $profileHome '.hermes-update-in-progress'
        Result = Join-Path $profileHome '.hermes-update-result.json'
    }
    $code = Invoke-TestHandoff $profileInstall '' 0
    Assert-Equal 8 $code 'profile-scoped install root is rejected before coordination state is touched'
    Assert-True (-not (Test-Path -LiteralPath $profileInstall.Sentinel)) 'profile-scoped root never reaches mutation'
    Assert-True (-not (Test-Path -LiteralPath $profileInstall.UpdateMarker)) 'profile-local update marker is never created'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $profileGlobal '.hermes-update-in-progress'))) 'rejected profile input does not guess or mutate a global marker'

    $noCapability = New-TestInstall 'no-capability' $fakeHermes
    $code = Invoke-TestHandoff $noCapability '' 0
    Assert-Equal 8 $code 'Desktop update without a bridge lease capability fails closed'
    Assert-True (-not (Test-Path -LiteralPath $noCapability.Sentinel)) 'missing capability never reaches preflight or mutation'

    $invalid = New-TestInstall 'invalid-json' $fakeHermes
    $code = Invoke-LeasedTestHandoff $invalid 'not-json' 0
    Assert-Equal 7 $code 'invalid supported preflight aborts'
    Assert-True (-not (Test-Path -LiteralPath $invalid.Sentinel)) 'invalid preflight never reaches mutation'

    $blocked = New-TestInstall 'blocked' $fakeHermes
    $code = Invoke-LeasedTestHandoff $blocked (New-PreflightJson $blocked $true $false) 2
    Assert-Equal 7 $code 'ready=false preflight aborts'
    Assert-True (-not (Test-Path -LiteralPath $blocked.Sentinel)) 'blocked preflight never reaches mutation'

    $probeFailure = New-TestInstall 'probe-failure' $fakeHermes
    $code = Invoke-LeasedTestHandoff $probeFailure (New-PreflightJson $probeFailure $false $false) 1
    Assert-Equal 7 $code 'failed preflight probe aborts'
    Assert-True (-not (Test-Path -LiteralPath $probeFailure.Sentinel)) 'failed probe never reaches mutation'

    $legacy = New-TestInstall 'legacy' $fakeHermes
    $code = Invoke-LeasedTestHandoff $legacy '' 2 'hermes: error: unrecognized arguments: --preflight --json'
    Assert-Equal 7 $code 'explicitly unsupported preflight fails closed'
    Assert-True (-not (Test-Path -LiteralPath $legacy.Sentinel)) 'unsupported preflight never reaches legacy mutation guard'

    $partial = New-TestInstall 'partial-preflight' $fakeHermes
    $code = Invoke-LeasedTestHandoff $partial '' 2 'hermes: error: unrecognized arguments: --json'
    Assert-Equal 7 $code 'unknown --json alone is not mistaken for legacy preflight absence'
    Assert-True (-not (Test-Path -LiteralPath $partial.Sentinel)) 'partial preflight support fails before mutation'

    $missingLease = New-TestInstall 'missing-lease' $fakeHermes
    $missingLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    $code = Invoke-TestHandoff $missingLease (New-PreflightJson $missingLease $true $true) 0 '' $missingLeaseId
    Assert-Equal 8 $code 'expected but missing bridge lease aborts'
    Assert-True (-not (Test-Path -LiteralPath $missingLease.Sentinel)) 'missing expected lease never reaches mutation'

    $unreadableMarker = New-TestInstall 'unreadable-update-marker' $fakeHermes
    $markerNow = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    [System.IO.File]::WriteAllText($unreadableMarker.UpdateMarker, "$PID`n$markerNow`n")
    $unreadableMarkerLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $unreadableMarker $unreadableMarkerLeaseId
    $markerLock = [System.IO.File]::Open(
        $unreadableMarker.UpdateMarker,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::None
    )
    try {
        $code = Invoke-TestHandoff $unreadableMarker (New-PreflightJson $unreadableMarker $true $true) 0 '' $unreadableMarkerLeaseId
    } finally {
        $markerLock.Dispose()
    }
    Assert-Equal 8 $code 'unreadable update-marker owner fails closed'
    Assert-True (-not (Test-Path -LiteralPath $unreadableMarker.Sentinel)) 'unreadable marker never reaches mutation'
    Assert-True (Test-Path -LiteralPath $unreadableMarker.UpdateMarker) 'unreadable marker is preserved'

    $foreignMarker = New-TestInstall 'foreign-update-marker' $fakeHermes
    [System.IO.File]::WriteAllText($foreignMarker.UpdateMarker, "$PID`n$markerNow`n")
    $markerBefore = [System.IO.File]::ReadAllText($foreignMarker.UpdateMarker)
    $foreignMarkerLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $foreignMarker $foreignMarkerLeaseId
    $code = Invoke-TestHandoff $foreignMarker (New-PreflightJson $foreignMarker $true $true) 0 '' $foreignMarkerLeaseId
    Assert-Equal 8 $code 'live foreign update marker blocks atomic claim'
    Assert-True (-not (Test-Path -LiteralPath $foreignMarker.Sentinel)) 'foreign update marker blocks mutation'
    Assert-Equal $markerBefore ([System.IO.File]::ReadAllText($foreignMarker.UpdateMarker)) 'foreign update marker is neither replaced nor deleted'

    $oldLiveMarker = New-TestInstall 'old-live-update-marker' $fakeHermes
    $longAgo = $markerNow - 1260
    # PID 4 is the Windows System process, created at boot. On the native CI
    # hosts it predates this deliberately old claim and therefore models a
    # genuine >20-minute live updater rather than a reused numeric PID.
    [System.IO.File]::WriteAllText($oldLiveMarker.UpdateMarker, "4`n$longAgo`n")
    $oldMarkerBefore = [System.IO.File]::ReadAllText($oldLiveMarker.UpdateMarker)
    $oldMarkerLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $oldLiveMarker $oldMarkerLeaseId
    $code = Invoke-TestHandoff $oldLiveMarker (New-PreflightJson $oldLiveMarker $true $true) 0 '' $oldMarkerLeaseId
    Assert-Equal 8 $code 'live update marker remains authoritative past the age ceiling'
    Assert-True (-not (Test-Path -LiteralPath $oldLiveMarker.Sentinel)) 'long-running live owner still blocks mutation'
    Assert-Equal $oldMarkerBefore ([System.IO.File]::ReadAllText($oldLiveMarker.UpdateMarker)) 'old live marker is preserved rather than stolen'

    $leased = New-TestInstall 'lease-adoption' $fakeHermes
    $leasedDesktop = Join-Path $leased.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $leasedDesktop
    $leaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $leased $leaseId
    Assert-Equal 1 (@([System.IO.File]::ReadAllLines((Join-Path $leased.Root 'venv\pyvenv.cfg')) | Where-Object { $_ -match '^\s*executable\s*=' }).Count) 'explicit-executable recovery fixture has one executable key'
    $code = Invoke-TestHandoff $leased (New-PreflightJson $leased $true $true) 0 'preflight diagnostic' $leaseId 'normal' $leasedDesktop
    Assert-Equal 0 $code 'ready preflight with matching lease completes'
    Assert-Equal ('a' * 40) ([System.IO.File]::ReadAllText($leased.BuildShaCapture)) 'git rebuild stamp is pinned to the correlated resulting HEAD'
    Assert-True (Test-Path -LiteralPath $leased.Sentinel) 'ready preflight reaches update'
    Assert-True (-not (Test-Path -LiteralPath $leased.Lease)) 'exact update child clears its adopted lease before success'
    Assert-True (Test-Path -LiteralPath $leased.PreflightLeaseCapture) 'preflight observed the script-owned step-zero lease'
    Assert-True (Test-Path -LiteralPath $leased.LeaseCapture) 'fake updater observed lease during mutation'
    Assert-True (Test-Path -LiteralPath $leased.PreflightArgsCapture) 'preflight argv was captured for capability contract checks'
    if (Test-Path -LiteralPath $leased.PreflightArgsCapture) {
        $preflightArgLines = @([System.IO.File]::ReadAllLines($leased.PreflightArgsCapture))
        Assert-Equal 1 $preflightArgLines.Count 'successful handoff runs one capability-authorized initial preflight'
        $preflightRecord = @($preflightArgLines[0] -split "`t", 2)
        Assert-Equal 2 $preflightRecord.Count 'preflight capture records argv and working directory'
        Assert-True ($preflightRecord[0] -match '^-m hermes_cli\.main update ') 'preflight bypasses the console shim and runs managed Python directly'
        Assert-True ($preflightRecord[0] -match '--bridge-lease-id' -and $preflightRecord[0] -match [regex]::Escape($leaseId)) 'initial preflight receives the matching private lease capability'
        if ($preflightRecord.Count -eq 2) {
            Assert-Equal ([System.IO.Path]::GetFullPath($leased.Root).TrimEnd([char[]]@('\', '/'))) ([System.IO.Path]::GetFullPath($preflightRecord[1]).TrimEnd([char[]]@('\', '/'))) 'first contained managed preflight starts in the exact install root'
        }
    }
    $scriptLeaseOwnerPid = -1
    if (Test-Path -LiteralPath $leased.PreflightLeaseCapture) {
        $beforeMutation = [System.IO.File]::ReadAllText($leased.PreflightLeaseCapture) | ConvertFrom-Json
        $scriptLeaseOwnerPid = [int64]$beforeMutation.owner_pid
        Assert-True ([int64]$beforeMutation.owner_pid -gt 0 -and [int64]$beforeMutation.owner_pid -ne $PID) 'PowerShell adopts lease with its real pid before preflight'
    }
    if (Test-Path -LiteralPath $leased.LeaseCapture) {
        $captured = [System.IO.File]::ReadAllText($leased.LeaseCapture) | ConvertFrom-Json
        Assert-Equal 1 $captured.schema_version 'adopted lease keeps schema version'
        Assert-Equal $leaseId $captured.lease_id 'adopted lease keeps capability id'
        Assert-True ([int64]$captured.owner_pid -gt 0 -and [int64]$captured.owner_pid -ne $scriptLeaseOwnerPid) 'exact update child adopts lease before mutation'
        Assert-Equal 1200 ([int64]$captured.expires_at - [int64]$captured.created_at) 'renewed lease keeps bounded lifetime'
        Assert-Equal 90 ([int64]$captured.handoff_grace_until - [int64]$captured.created_at) 'renewed lease keeps bounded handoff grace'
        Assert-Equal ([System.IO.Path]::GetFullPath($leased.Root).TrimEnd([char[]]@('\', '/'))) $captured.install_root 'adopted lease preserves canonical install root'
    }
    Assert-True (Test-Path -LiteralPath $leased.TopologyCapture) 'contained updater records its wrapper-to-managed-process topology'
    if ((Test-Path -LiteralPath $leased.TopologyCapture) -and (Test-Path -LiteralPath $leased.LeaseCapture)) {
        $topology = @([System.IO.File]::ReadAllLines($leased.TopologyCapture) | Where-Object { $_ })
        $captured = [System.IO.File]::ReadAllText($leased.LeaseCapture) | ConvertFrom-Json
        Assert-Equal 3 $topology.Count 'contained updater topology records wrapper pid, managed pid, and inherited Job membership'
        Assert-True ([int]$topology[0] -gt 0 -and [int]$topology[0] -ne [int]$topology[1]) 'managed updater is a descendant distinct from the assigned wrapper'
        Assert-Equal 'in-job' $topology[2] 'managed updater starts only after the wrapper has inherited exact Job containment'
        Assert-Equal ([int64]$captured.owner_pid) ([int64]$topology[1]) 'lease transfer accepts the contained managed descendant as exact owner'
    }
    if (Test-Path -LiteralPath $leased.Sentinel) {
        Assert-True ([System.IO.File]::ReadAllText($leased.Sentinel) -match '^-m hermes_cli\.main update ') 'mutation path bypasses the console shim and retains one exact Python PID'
    }
    Assert-True (Test-Path -LiteralPath $leased.ResumeCapture) 'trusted deferred gateway resume argv is captured'
    if ((Test-Path -LiteralPath $leased.Sentinel) -and (Test-Path -LiteralPath $leased.ResumeCapture)) {
        $updateInvocation = [regex]::Match([System.IO.File]::ReadAllText($leased.Sentinel), '--invocation-id\s+(\S+)').Groups[1].Value
        $resumeInvocation = [regex]::Match([System.IO.File]::ReadAllText($leased.ResumeCapture), '--invocation-id\s+(\S+)').Groups[1].Value
        Assert-True ($updateInvocation -match '^invocation-[A-Za-z0-9._-]{16,128}$') 'update receives one valid parent-generated invocation id'
        Assert-Equal $updateInvocation $resumeInvocation 'update receipt plan and trusted resume share one invocation id'
    }
    Assert-True (Test-Path -LiteralPath $leased.Result) 'versioned handoff result is written after terminal cleanup'
    if (Test-Path -LiteralPath $leased.Result) {
        $result = [System.IO.File]::ReadAllText($leased.Result) | ConvertFrom-Json
        $resultFields = @($result.PSObject.Properties | ForEach-Object { $_.Name })
        Assert-Equal 16 $resultFields.Count 'handoff result has exact v2 top-level field count'
        Assert-Equal 2 $result.schema_version 'handoff result schema version'
        Assert-Equal 'complete' $result.state 'terminal result is complete only after Desktop ACK'
        Assert-True ($result.ok -is [bool] -and $result.ok) 'handoff result success is a literal boolean'
        Assert-Equal $result.receipt.invocation_id $result.invocation_id 'handoff result correlates invocation id'
        Assert-Equal $result.receipt.lease_id $result.lease_id 'handoff result correlates lease id'
        Assert-True ($result.cleanup.update_marker_released -and $result.cleanup.bridge_lease_released) 'handoff result is written after both markers are released'
        Assert-Equal 'acknowledged' $result.relaunch.state 'terminal result records exact Desktop acknowledgment'
        Assert-True ($result.desktop.backend_ready -is [bool] -and $result.desktop.backend_ready) 'terminal result includes literal backend readiness proof'
        Assert-Equal 0 ([int]$result.exit_code) 'terminal complete result carries exit code zero'
    }
    $handoffLog = Join-Path $leased.Home 'logs\desktop-update-handoff.log'
    if (Test-Path -LiteralPath $handoffLog) {
        Assert-True ([System.IO.File]::ReadAllText($handoffLog) -notmatch [regex]::Escape($leaseId)) 'lease capability is absent from user-visible handoff diagnostics'
    }

    $trampoline = New-TestInstall 'resume-trampoline' $fakeHermes
    $trampolineBaseDir = Join-Path $trampoline.Root 'base-python'
    [System.IO.File]::WriteAllText(
        (Join-Path $trampoline.Root 'venv\pyvenv.cfg'),
        "home = $trampolineBaseDir`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    Assert-Equal 0 (@([System.IO.File]::ReadAllLines((Join-Path $trampoline.Root 'venv\pyvenv.cfg')) | Where-Object { $_ -match '^\s*executable\s*=' }).Count) 'home-only recovery fixture omits the executable key'
    $trampolineDesktop = Join-Path $trampoline.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $trampolineDesktop
    $trampolineLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $trampoline $trampolineLeaseId
    $code = Invoke-TestHandoff $trampoline (New-PreflightJson $trampoline $true $true) 0 '' $trampolineLeaseId 'resume-trampoline' $trampolineDesktop
    if ($code -ne 0) {
        Get-Content -LiteralPath (Join-Path $trampoline.Home 'logs\desktop-update-handoff.log') -ErrorAction SilentlyContinue
    }
    Assert-Equal 0 $code 'deferred resume bypasses the exact Windows venv redirector'
    Assert-True (-not (Test-Path -LiteralPath $trampoline.ResumeRedirectorCapture)) 'deferred resume never starts the redirector process'
    Assert-True (Test-Path -LiteralPath $trampoline.ResumeCapture) 'home-only recovery starts the canonical base interpreter'
    Assert-True (-not (Test-Path -LiteralPath $trampoline.Lease)) 'base-interpreter resume clears the exact adopted lease'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $trampoline.Home -Filter '.hermes-venv-quiesce.cas-*' -File -ErrorAction SilentlyContinue).Count) 'base-interpreter resume leaves no lease CAS artifacts'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $trampoline.Home -Filter '.hermes-gateway-resume-*.json' -File -ErrorAction SilentlyContinue).Count) 'base-interpreter resume consumes the exact pending plan'

    $invalidVenvCases = @(
        [pscustomobject]@{
            Tag = 'missing-home'
            Label = 'missing pyvenv home'
            Configure = {
                param($Install)
                Write-TestPyvenvConfig $Install @('implementation = CPython')
            }
        },
        [pscustomobject]@{
            Tag = 'malformed-home'
            Label = 'malformed pyvenv home entry'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root 'base-python'
                Write-TestPyvenvConfig $Install @("home $baseDir", 'implementation = CPython')
            }
        },
        [pscustomobject]@{
            Tag = 'duplicate-home'
            Label = 'duplicate pyvenv home key'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root 'base-python'
                Write-TestPyvenvConfig $Install @("home = $baseDir", "HOME = $baseDir")
            }
        },
        [pscustomobject]@{
            Tag = 'missing-derived-python'
            Label = 'missing home-derived python.exe'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root 'base-python'
                Remove-Item -LiteralPath (Join-Path $baseDir 'python.exe') -Force
                Write-TestPyvenvConfig $Install @("home = $baseDir")
            }
        },
        [pscustomobject]@{
            Tag = 'non-file-derived-python'
            Label = 'non-file home-derived python.exe'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root 'base-python'
                $basePython = Join-Path $baseDir 'python.exe'
                Remove-Item -LiteralPath $basePython -Force
                New-Item -ItemType Directory -Path $basePython | Out-Null
                Write-TestPyvenvConfig $Install @("home = $baseDir")
            }
        },
        [pscustomobject]@{
            Tag = 'explicit-python-outside-home'
            Label = 'explicit python.exe outside canonical home'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root 'base-python'
                $outsideDir = Join-Path $Install.Root 'outside-base-python'
                New-Item -ItemType Directory -Path $outsideDir | Out-Null
                $outsidePython = Join-Path $outsideDir 'python.exe'
                Copy-Item -LiteralPath $fakeHermes -Destination $outsidePython
                Write-TestPyvenvConfig $Install @("home = $baseDir", "executable = $outsidePython")
            }
        }
    )
    foreach ($invalidVenvCase in $invalidVenvCases) {
        $invalidVenv = New-TestInstall $invalidVenvCase.Tag $fakeHermes
        $invalidVenvHomes += $invalidVenv.Home
        $configureInvalidVenv = $invalidVenvCase.Configure
        & $configureInvalidVenv $invalidVenv
        $invalidVenvLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $invalidVenv $invalidVenvLeaseId
        $code = Invoke-TestHandoff $invalidVenv (New-PreflightJson $invalidVenv $true $true) 0 '' $invalidVenvLeaseId
        Assert-InvalidPyvenvRecoveryRejected $invalidVenv $code $invalidVenvCase.Label
    }

    $archive = New-TestInstall 'archive-build-identity' $fakeHermes
    $archiveLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $archive $archiveLeaseId
    $archiveDesktop = Join-Path $archive.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $archiveDesktop
    $code = Invoke-TestHandoff $archive (New-PreflightJson $archive $true $true) 0 '' $archiveLeaseId 'archive' $archiveDesktop
    Assert-Equal 0 $code 'archive-mode handoff completes with receipt-correlated Desktop proof'
    Assert-Equal ('b' * 64) ([System.IO.File]::ReadAllText($archive.BuildShaCapture)) 'archive rebuild stamp is pinned to the correlated 64-hex archive digest'
    if (Test-Path -LiteralPath $archive.Result) {
        $archiveResult = [System.IO.File]::ReadAllText($archive.Result) | ConvertFrom-Json
        Assert-Equal ('b' * 64) ([string]$archiveResult.desktop.build_id) 'archive Desktop ACK proves the same receipt-derived build identity'
    }

    $immediate = New-TestInstall 'immediate-consumer' $fakeHermes
    $immediateLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $immediate $immediateLeaseId
    $fakeDesktop = Join-Path $immediate.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $fakeDesktop
    $code = Invoke-TestHandoff $immediate (New-PreflightJson $immediate $true $true) 0 '' $immediateLeaseId 'normal' $fakeDesktop
    Assert-Equal 0 $code 'successful handoff publishes a correlated result for the immediate relaunch consumer'
    $consumerResult = Join-Path $immediate.Home 'immediate-consumer-result.json'
    $consumerPidPath = Join-Path $immediate.Home 'immediate-consumer-pid.txt'
    $consumerDeadline = (Get-Date).AddSeconds(6)
    while ((Get-Date) -lt $consumerDeadline -and -not (Test-Path -LiteralPath $consumerResult)) {
        Start-Sleep -Milliseconds 25
    }
    Assert-True (Test-Path -LiteralPath $consumerResult) 'consumer starting at relaunch observes the atomically published result'
    if ((Test-Path -LiteralPath $consumerResult) -and (Test-Path -LiteralPath $consumerPidPath)) {
        $consumed = [System.IO.File]::ReadAllText($consumerResult) | ConvertFrom-Json
        $consumerPid = [int][System.IO.File]::ReadAllText($consumerPidPath)
        Assert-Equal 'pending' $consumed.state 'published result is pending, never success, before ACK'
        Assert-True ($consumed.ok -is [bool] -and -not $consumed.ok) 'pending result carries literal ok=false'
        Assert-Equal 'pending' $consumed.relaunch.state 'published result records relaunch as pending, not confirmed'
        Assert-Equal $consumerPid ([int]$consumed.relaunch.pid) 'published result correlates the exact relaunched Desktop pid'
    }

    $survivor = New-TestInstall 'single-instance-survivor' $fakeHermes
    $survivorLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $survivor $survivorLeaseId
    $survivorDesktop = Join-Path $survivor.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $survivorDesktop
    $oldDesktop = Start-Process -FilePath $survivorDesktop -WorkingDirectory $survivor.Home -WindowStyle Hidden -PassThru
    $oldDesktopPid = $oldDesktop.Id
    Start-Sleep -Milliseconds 150
    $code = Invoke-TestHandoff $survivor (New-PreflightJson $survivor $true $true) 0 '' $survivorLeaseId 'normal' $survivorDesktop
    if ($code -ne 0) {
        Get-Content -LiteralPath (Join-Path $survivor.Home 'logs\desktop-update-handoff.log') -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $survivor.Home -Filter '.hermes-update-relaunch-exit-ack-*' -File -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host ("LEFTOVER ACK: {0} :: {1}" -f $_.Name, [System.IO.File]::ReadAllText($_.FullName))
        }
    }
    Assert-Equal 0 $code 'parked single-instance Desktop ACKs quit before exact relaunch'
    $oldDesktop.WaitForExit(3000) | Out-Null
    Assert-True $oldDesktop.HasExited 'pre-existing exact Desktop process exits after its attempt-scoped request ACK'
    if (Test-Path -LiteralPath $survivor.Result) {
        $survivorResult = [System.IO.File]::ReadAllText($survivor.Result) | ConvertFrom-Json
        Assert-True ([int]$survivorResult.relaunch.pid -gt 0 -and [int]$survivorResult.relaunch.pid -ne $oldDesktopPid) 'terminal result identifies the newly launched Desktop, not the survivor'
    }
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $survivor.Home -Filter '.hermes-update-relaunch-request-*.json' -File -ErrorAction SilentlyContinue).Count) 'relaunch request is retired only after durable authorization'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $survivor.Home -Filter '.hermes-update-relaunch-exit-ack-*.json' -File -ErrorAction SilentlyContinue).Count) 'survivor quit ACK is exact-byte consumed'

    $unwritableResult = New-TestInstall 'unwritable-result' $fakeHermes
    $unwritableDesktop = Join-Path $unwritableResult.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $unwritableDesktop
    $unwritableLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $unwritableResult $unwritableLeaseId
    $code = Invoke-TestHandoff $unwritableResult (New-PreflightJson $unwritableResult $true $true) 0 '' $unwritableLeaseId 'result-publish-fail' $unwritableDesktop
    if ($code -ne 11) {
        Get-Content -LiteralPath (Join-Path $unwritableResult.Home 'logs\desktop-update-handoff.log') -ErrorAction SilentlyContinue
    }
    Assert-Equal 11 $code 'success is downgraded when the durable handoff result cannot be published'
    Assert-True (-not (Test-Path -LiteralPath $unwritableResult.Result)) 'simulated publication failure leaves no false durable success result'

    $silent = New-TestInstall 'silent-child' $fakeHermes
    $silentDesktop = Join-Path $silent.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $silentDesktop
    $silentLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $silent $silentLeaseId
    $code = Invoke-TestHandoff $silent (New-PreflightJson $silent $true $true) 0 '' $silentLeaseId 'silent' $silentDesktop
    Assert-Equal 0 $code 'silent update child does not block lease polling or receipt verification'
    Assert-True (-not (Test-Path -LiteralPath $silent.Lease)) 'silent child cleans its exact adopted lease'

    $stderrHeavy = New-TestInstall 'stderr-heavy-child' $fakeHermes
    $stderrDesktop = Join-Path $stderrHeavy.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $stderrDesktop
    $stderrLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $stderrHeavy $stderrLeaseId
    $code = Invoke-TestHandoff $stderrHeavy (New-PreflightJson $stderrHeavy $true $true) 0 '' $stderrLeaseId 'stderr-heavy' $stderrDesktop
    Assert-Equal 0 $code 'stderr-heavy update child is drained concurrently without deadlock'
    Assert-True (-not (Test-Path -LiteralPath $stderrHeavy.Lease)) 'stderr-heavy child cleans its exact adopted lease'

    $foreignRace = New-TestInstall 'foreign-race' $fakeHermes
    $raceLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $foreignRace $raceLeaseId
    $code = Invoke-TestHandoff $foreignRace (New-PreflightJson $foreignRace $true $true) 0 '' $raceLeaseId 'foreign-rewrite'
    Assert-Equal 13 $code 'foreign lease rewrite stops the exact spawned update tree and reports failed fleet recovery'
    Assert-True (Test-Path -LiteralPath $foreignRace.Lease) 'foreign rewrite survives failed child cleanup'
    Assert-True (Test-Path -LiteralPath $foreignRace.TopologyCapture) 'lease-loss fixture records the contained updater tree'
    if (Test-Path -LiteralPath $foreignRace.TopologyCapture) {
        $raceTopology = @([System.IO.File]::ReadAllLines($foreignRace.TopologyCapture) | Where-Object { $_ })
        Assert-Equal 3 $raceTopology.Count 'lease-loss topology retains exact wrapper, managed child, and Job-membership proof'
        $wrapperTerminated = $raceTopology.Count -ge 2 -and -not (Get-Process -Id ([int]$raceTopology[0]) -ErrorAction SilentlyContinue)
        $managedTerminated = $raceTopology.Count -ge 2 -and -not (Get-Process -Id ([int]$raceTopology[1]) -ErrorAction SilentlyContinue)
        Assert-True $wrapperTerminated 'lease loss terminates the exact assigned wrapper'
        Assert-True $managedTerminated 'lease loss terminates the contained managed descendant'
    }
    if (Test-Path -LiteralPath $foreignRace.Lease) {
        $racedLease = [System.IO.File]::ReadAllText($foreignRace.Lease) | ConvertFrom-Json
        Assert-Equal 'foreign-lease-0123456789abcdef' $racedLease.lease_id 'foreign raced capability is not deleted'
    }

    $foreign = New-TestInstall 'foreign-lease' $fakeHermes
    $foreignLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $foreign $foreignLeaseId
    $before = [System.IO.File]::ReadAllText($foreign.Lease)
    $code = Invoke-TestHandoff $foreign (New-PreflightJson $foreign $true $true) 0 '' ('different-' + [Guid]::NewGuid().ToString('N'))
    Assert-Equal 8 $code 'mismatched lease capability aborts before mutation'
    Assert-True (-not (Test-Path -LiteralPath $foreign.Sentinel)) 'foreign lease is never followed by mutation'
    Assert-Equal $before ([System.IO.File]::ReadAllText($foreign.Lease)) 'foreign live lease is neither rewritten nor deleted'
} finally {
    $cleanupPaths = @($noCapability.Home, $invalid.Home, $blocked.Home, $probeFailure.Home, $legacy.Home, $partial.Home, $missingLease.Home, $unreadableMarker.Home, $foreignMarker.Home, $oldLiveMarker.Home, $leased.Home, $trampoline.Home, $archive.Home, $immediate.Home, $survivor.Home, $unwritableResult.Home, $silent.Home, $stderrHeavy.Home, $foreignRace.Home, $foreign.Home, $suiteRoot) + $invalidVenvHomes
    foreach ($path in $cleanupPaths) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($failures -gt 0) {
    Write-Host "FAILED: $failures assertion(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host 'All desktop update handoff contract tests passed.' -ForegroundColor Green
exit 0
