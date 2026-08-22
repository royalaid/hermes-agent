# Contract tests for scripts/desktop-update.ps1.
#
# These tests run the handoff in a temporary install with a tiny fake
# hermes.exe. No real checkout, venv, Desktop process, or update is touched.

param(
    [switch]$DeferredGatewayContainmentOnly,
    [switch]$ActivationBuildProofOnly,
    [switch]$ActivationJournalRecoveryOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$handoffScript = Join-Path $repoRoot 'scripts\desktop-update.ps1'
$desktopUpdateJsonHelper = Join-Path $repoRoot 'scripts\desktop-update-json.ps1'
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

function Assert-NoDeferredGateArtifacts([string]$GatePath, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($GatePath)) {
        Assert-True $false "$Label records its exact gate path"
        return
    }
    $parent = Split-Path -Parent $GatePath
    $leaf = Split-Path -Leaf $GatePath
    $artifacts = @(
        Get-ChildItem -LiteralPath $parent -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq $leaf -or $_.Name -like "$leaf.next-*" -or $_.Name -like "$leaf.previous-*" }
    )
    Assert-Equal 0 $artifacts.Count "$Label leaves no startup gate or transition sibling"
}

function Get-TestWriterIdentity([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $lines = @([System.IO.File]::ReadAllLines($Path))
    if ($lines.Count -ne 3) { return $null }
    $pidValue = 0
    $startedAtTicks = 0L
    if (-not [int]::TryParse($lines[0], [ref]$pidValue) -or $pidValue -le 0 -or
        -not [int64]::TryParse($lines[2], [ref]$startedAtTicks) -or $startedAtTicks -le 0) {
        return $null
    }
    return [pscustomobject]@{
        Pid = $pidValue
        Membership = $lines[1]
        StartedAtTicks = $startedAtTicks
    }
}

function Test-TestWriterLive([AllowNull()][object]$Identity) {
    if (-not $Identity) { return $false }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$Identity.Pid)
        return -not $process.HasExited -and
            $process.StartTime.ToUniversalTime().Ticks -eq [int64]$Identity.StartedAtTicks
    } catch {
        return $false
    }
}

function Stop-TestWriterExact([AllowNull()][object]$Identity, [string]$ReleasePath) {
    if (-not $Identity) { return }
    try {
        [System.IO.File]::WriteAllText($ReleasePath, 'release')
    } catch {}
    $deadline = (Get-Date).AddSeconds(5)
    while ((Test-TestWriterLive $Identity) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 25
    }
    if (-not (Test-TestWriterLive $Identity)) { return }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$Identity.Pid)
        if (-not $process.HasExited -and
            $process.StartTime.ToUniversalTime().Ticks -eq [int64]$Identity.StartedAtTicks) {
            $process.Kill()
            $process.WaitForExit()
        }
    } catch {}
}

function Compile-TestExecutable(
    [string]$Destination,
    [string]$Source,
    [string]$Label,
    [switch]$Windowless
) {
    if ($PSVersionTable.PSEdition -eq 'Core') {
        # PowerShell 7's Add-Type cannot emit an executable. Use the inbox .NET
        # Framework compiler so the same behavior fixture runs under PS5 + pwsh.
        $sourcePath = [System.IO.Path]::ChangeExtension($Destination, '.cs')
        [System.IO.File]::WriteAllText($sourcePath, $Source)
        $windowsDirectory = [Environment]::GetFolderPath('Windows')
        $compiler = Join-Path $windowsDirectory 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        $target = if ($Windowless) { 'winexe' } else { 'exe' }
        & $compiler /nologo "/target:$target" "/out:$Destination" $sourcePath
        $compileCode = $LASTEXITCODE
        Remove-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue
        if ($compileCode -ne 0 -or -not (Test-Path -LiteralPath $Destination)) {
            throw "$Label compilation failed with exit $compileCode"
        }
    } else {
        $outputType = if ($Windowless) { 'WindowsApplication' } else { 'ConsoleApplication' }
        Add-Type -TypeDefinition $Source -Language CSharp -OutputAssembly $Destination -OutputType $outputType
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
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars;
        public int dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsProcessInJob(IntPtr processHandle, IntPtr jobHandle, out bool result);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessTimes(IntPtr processHandle, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint creationFlags, IntPtr environment, string currentDirectory,
        ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInformation);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);

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

    static string HmacHex(string payload, string key) {
        using (var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(key))) {
            return String.Concat(hmac.ComputeHash(Encoding.UTF8.GetBytes(payload)).Select(b => b.ToString("x2")));
        }
    }

    static string JsonString(string value) {
        return "\"" + Escape(value) + "\"";
    }

    static string CreationFileTime(Process process) {
        long creation, exit, kernel, user;
        if (!GetProcessTimes(process.Handle, out creation, out exit, out kernel, out user))
            throw new System.ComponentModel.Win32Exception();
        return ((ulong)creation).ToString(System.Globalization.CultureInfo.InvariantCulture);
    }

    static void AppendContainmentState(string value) {
        var capture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_CONTAINMENT_CAPTURE");
        if (!String.IsNullOrEmpty(capture))
            File.AppendAllText(capture, value + Environment.NewLine);
    }

    static int AwaitContainmentGate(string mode) {
        var gate = Environment.GetEnvironmentVariable("HERMES_DEFERRED_GATEWAY_STARTUP_GATE");
        if (String.IsNullOrEmpty(gate)) {
            AppendContainmentState("missing");
            return 20;
        }
        var gateCapture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_TARGET_GATE_CAPTURE");
        if (!String.IsNullOrEmpty(gateCapture)) File.WriteAllText(gateCapture, gate);
        AppendContainmentState("waiting:" + Process.GetCurrentProcess().Id + ":" +
            Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks);
        var deadline = DateTime.UtcNow.AddSeconds(10);
        DateTime? unreadableSince = null;
        while (DateTime.UtcNow < deadline) {
            string state;
            try {
                using (var stream = new FileStream(
                    gate, FileMode.Open, FileAccess.Read,
                    FileShare.ReadWrite | FileShare.Delete)) {
                    if (stream.Length <= 0 || stream.Length > 6) {
                        AppendContainmentState("malformed");
                        return 23;
                    }
                    var bytes = new byte[(int)stream.Length];
                    if (stream.Read(bytes, 0, bytes.Length) != bytes.Length) {
                        AppendContainmentState("malformed");
                        return 23;
                    }
                    state = Encoding.UTF8.GetString(bytes);
                }
                unreadableSince = null;
            } catch (Exception error) {
                if (!(error is IOException) && !(error is UnauthorizedAccessException))
                    throw;
                if (!unreadableSince.HasValue) unreadableSince = DateTime.UtcNow;
                if (DateTime.UtcNow >= unreadableSince.Value.AddMilliseconds(2250)) {
                    AppendContainmentState("unreadable");
                    return 21;
                }
                Thread.Sleep(25);
                continue;
            }
            if (state == "wait") {
                Thread.Sleep(25);
                continue;
            }
            if (state == "abort") {
                AppendContainmentState("aborted");
                return 22;
            }
            if (state != "armed") {
                AppendContainmentState("malformed");
                return 23;
            }
            bool inJob;
            var known = IsProcessInJob(Process.GetCurrentProcess().Handle, IntPtr.Zero, out inJob);
            AppendContainmentState(known && inJob ? "armed:in-job" : "armed:not-in-job");
            return known && inJob ? 0 : 24;
        }
        AppendContainmentState("timeout");
        return 25;
    }

    static Process StartHeldWriter() {
        var executable = Process.GetCurrentProcess().MainModule.FileName;
        var commandLine = new StringBuilder("\"" + executable + "\" --test-held-writer");
        var startup = new STARTUPINFO { cb = Marshal.SizeOf(typeof(STARTUPINFO)) };
        PROCESS_INFORMATION created;
        // bInheritHandles=false is the behavior under test: the descendant
        // inherits Job membership, but never the wrapper's outer pipe handles.
        if (!CreateProcess(executable, commandLine, IntPtr.Zero, IntPtr.Zero, false,
            // DETACHED_PROCESS guarantees this long-lived writer cannot keep
            // the short-lived console target's conhost alive.
            0x00000008, IntPtr.Zero, Directory.GetCurrentDirectory(), ref startup, out created))
            throw new System.ComponentModel.Win32Exception();
        Process process;
        try {
            process = Process.GetProcessById(created.dwProcessId);
        } finally {
            CloseHandle(created.hThread);
            CloseHandle(created.hProcess);
        }
        var capture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_WRITER_CAPTURE");
        var deadline = DateTime.UtcNow.AddSeconds(2);
        while (process != null && !String.IsNullOrEmpty(capture) && !File.Exists(capture) &&
            DateTime.UtcNow < deadline && !process.HasExited)
            Thread.Sleep(10);
        return process;
    }

    static int HoldWriter() {
        var capture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_WRITER_CAPTURE");
        var release = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_WRITER_RELEASE");
        bool inJob;
        var known = IsProcessInJob(Process.GetCurrentProcess().Handle, IntPtr.Zero, out inJob);
        if (!String.IsNullOrEmpty(capture))
            File.WriteAllText(capture, Process.GetCurrentProcess().Id + Environment.NewLine +
                (known && inJob ? "in-job" : "not-in-job") + Environment.NewLine +
                Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks + Environment.NewLine);
        var home = Environment.GetEnvironmentVariable("HERMES_HOME");
        if (!String.IsNullOrEmpty(home)) {
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var created = (Process.GetCurrentProcess().StartTime.ToUniversalTime() - epoch).TotalSeconds;
            var status = "{\"kind\":\"hermes-gateway\",\"gateway_state\":\"running\",\"pid\":" +
                Process.GetCurrentProcess().Id + ",\"start_time\":" + Math.Round(created * 100) +
                ",\"hermes_home\":" + JsonString(Path.GetFullPath(home).ToLowerInvariant()) + "}";
            File.WriteAllText(Path.Combine(home, "gateway_state.json"), status);
        }
        var delayedWrite = Environment.GetEnvironmentVariable("HERMES_TEST_DELAYED_WRITE_PATH");
        var delayedAt = DateTime.UtcNow.AddSeconds(2);
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < deadline && (String.IsNullOrEmpty(release) || !File.Exists(release))) {
            if (!String.IsNullOrEmpty(delayedWrite) && DateTime.UtcNow >= delayedAt) {
                File.WriteAllText(delayedWrite, "late-write");
                delayedWrite = null;
            }
            Thread.Sleep(25);
        }
        return 0;
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
        if (mode == "containment-noncooperative-success" || mode == "containment-fast-success") {
            var gate = Environment.GetEnvironmentVariable("HERMES_DEFERRED_GATEWAY_STARTUP_GATE") ?? "";
            var gateCapture = Environment.GetEnvironmentVariable("HERMES_TEST_RESUME_TARGET_GATE_CAPTURE");
            if (!String.IsNullOrEmpty(gateCapture)) File.WriteAllText(gateCapture, gate);
            bool inJob;
            var known = IsProcessInJob(Process.GetCurrentProcess().Handle, IntPtr.Zero, out inJob);
            AppendContainmentState(known && inJob ? "started:in-job" : "started:not-in-job");
        } else {
            var gateResult = AwaitContainmentGate(mode);
            if (gateResult != 0) return gateResult;
        }
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
        if (mode == "containment-success") {
            var gate = Environment.GetEnvironmentVariable("HERMES_DEFERRED_GATEWAY_STARTUP_GATE") ?? "";
            Console.WriteLine("Deferred gateway resume failed: home=" + home.ToUpperInvariant() +
                " temp=" + Path.GetTempPath().ToUpperInvariant() + " invocation=" + invocationId +
                " lease=" + leaseId + " gate=" + gate + " secret=CHILD_DIAGNOSTIC_SECRET_8f86b783");
            Console.Error.WriteLine("child stderr home=" + home.ToUpperInvariant() +
                " temp=" + Path.GetTempPath().ToUpperInvariant() +
                " token=CHILD_DIAGNOSTIC_SECRET_8f86b783 " + new string('z', 4096));
        }
        Process writer = null;
        if (mode == "containment-success" || mode == "containment-noncooperative-success" ||
            mode == "containment-failure-drain") {
            writer = StartHeldWriter();
            if (writer == null) return 26;
        }
        if (mode == "resume-duplicate-frame") Console.WriteLine(frame);
        if (mode == "resume-fail" || mode == "receipt-clock-rollback-resume-fail" ||
            mode == "containment-failure-drain") {
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
        var prepared = Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".prepared");
        var manifestPath = Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".prepared-runtime.json");
        if (!File.Exists(pending)) return 1;
        var planRaw = File.ReadAllText(pending);
        File.Move(pending, prepared);
        var root = Path.Combine(home, "hermes-agent").ToLowerInvariant();
        var runtimes = "[]";
        if (writer != null) {
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var created = Math.Round((writer.StartTime.ToUniversalTime() - epoch).TotalSeconds, 2);
            // Python json.dumps preserves the .0 for a rounded float. Mirror
            // that canonical representation exactly for the HMAC fixture.
            var createdText = created.ToString("0.0#", System.Globalization.CultureInfo.InvariantCulture);
            runtimes = "[{\"created_at\":" + createdText + ",\"creation_file_time\":" +
                JsonString(CreationFileTime(writer)) + ",\"executable_path\":" +
                JsonString(writer.MainModule.FileName.ToLowerInvariant()) + ",\"pid\":" + writer.Id +
                ",\"profile\":\"default\",\"profile_home\":" + JsonString(home.ToLowerInvariant()) + "}]";
        }
        var unsigned = "{\"install_root\":" + JsonString(root) + ",\"invocation_id\":" +
            JsonString(invocationId) + ",\"plan_sha256\":\"" + Fingerprint(planRaw) +
            "\",\"runtimes\":" + runtimes + ",\"schema_version\":1}";
        var unsignedCapture = Environment.GetEnvironmentVariable("HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE");
        if (!String.IsNullOrEmpty(unsignedCapture)) File.WriteAllText(unsignedCapture, unsigned);
        var manifest = "{\"auth\":\"" + HmacHex(unsigned, leaseId) + "\",\"install_root\":" +
            JsonString(root) + ",\"invocation_id\":" + JsonString(invocationId) +
            ",\"plan_sha256\":\"" + Fingerprint(planRaw) + "\",\"runtimes\":" + runtimes +
            ",\"schema_version\":1}";
        File.WriteAllText(manifestPath, manifest);
        if (File.Exists(leasePath)) File.Delete(leasePath);
        Console.WriteLine("Deferred gateway fleet prepared for native commit.");
        return 0;
    }

    static string JsonField(string raw, string name) {
        var match = Regex.Match(
            raw,
            "\\\"" + Regex.Escape(name) + "\\\"\\s*:\\s*\\\"([^\\\"]*)\\\""
        );
        return match.Success ? match.Groups[1].Value.Replace("\\\\", "\\") : null;
    }

    static string LeaseAuthorityJson(string lease, string root, long createdAt) {
        return "{\"schema_version\":1,\"lease_id\":\"" + Escape(lease) +
            "\",\"owner_pid\":" + Process.GetCurrentProcess().Id +
            ",\"created_at\":" + createdAt +
            ",\"expires_at\":" + (createdAt + 1200) +
            ",\"handoff_grace_until\":" + (createdAt + 90) +
            ",\"install_root\":\"" + Escape(root) + "\"}";
    }

    static bool HasJournalLeaseAuthority(string raw, int schema, string lease, string root) {
        if (!Regex.IsMatch(raw, "^\\{\\\"schema_version\\\":" + schema + ",") ||
            JsonField(raw, "lease_id") != lease) return false;
        var authority = Regex.Match(
            raw,
            "\\\"lease_authority\\\":\\{\\\"schema_version\\\":1," +
            "\\\"lease_id\\\":\\\"" + Regex.Escape(Escape(lease)) + "\\\"," +
            "\\\"owner_pid\\\":([1-9][0-9]*)," +
            "\\\"created_at\\\":([0-9]+)," +
            "\\\"expires_at\\\":([0-9]+)," +
            "\\\"handoff_grace_until\\\":([0-9]+)," +
            "\\\"install_root\\\":\\\"" + Regex.Escape(Escape(root)) + "\\\"\\}"
        );
        long created, expires, grace;
        return authority.Success &&
            Int64.TryParse(authority.Groups[2].Value, out created) &&
            Int64.TryParse(authority.Groups[3].Value, out expires) &&
            Int64.TryParse(authority.Groups[4].Value, out grace) &&
            created > 0 && expires - created == 1200 && grace - created == 90;
    }

    static int Activation(string[] args, string mode) {
        var moduleIndex = Array.IndexOf(args, "hermes_cli.desktop_update_activation");
        if (moduleIndex < 0 || moduleIndex + 1 >= args.Length) return 1;
        var action = args[moduleIndex + 1];
        var root = Environment.GetEnvironmentVariable("HERMES_INTERNAL_DESKTOP_UPDATE_ROOT");
        var home = Environment.GetEnvironmentVariable("HERMES_INTERNAL_DESKTOP_UPDATE_HOME");
        var invocation = Environment.GetEnvironmentVariable("HERMES_INTERNAL_DESKTOP_UPDATE_INVOCATION");
        var lease = Environment.GetEnvironmentVariable("HERMES_INTERNAL_DESKTOP_UPDATE_LEASE");
        if (String.IsNullOrEmpty(root) || String.IsNullOrEmpty(home) ||
            String.IsNullOrEmpty(invocation) || String.IsNullOrEmpty(lease)) return 1;
        var manifestPath = Path.Combine(home, ".hermes-update-activation.json");
        var statePath = Path.Combine(home, ".hermes-update-activation-state.json");
        var priorPath = statePath + ".prior";
        var receiptPath = Path.Combine(home, ".hermes-update-receipt.json");
        var healthPath = Path.Combine(home, ".hermes-update-desktop-health.json");
        var stagingPath = Path.Combine(home, ".hermes-update-staging.json");
        var acquisitionPath = Path.Combine(home, ".hermes-update-acquisition.json");

        if (action == "rollback-source") {
            if (mode == "rollback-fail") return 1;
            if (File.Exists(acquisitionPath)) {
                var acquisition = File.ReadAllText(acquisitionPath);
                if (!HasJournalLeaseAuthority(acquisition, 2, lease, root)) return 1;
                var workspaceRel = JsonField(acquisition, "workspace_rel");
                var workspace = String.IsNullOrEmpty(workspaceRel)
                    ? null
                    : Path.Combine(home, workspaceRel.Replace('/', Path.DirectorySeparatorChar));
                if (!String.IsNullOrEmpty(workspace) && Directory.Exists(workspace))
                    Directory.Delete(workspace, true);
                File.Delete(acquisitionPath);
            }
            if (File.Exists(stagingPath)) {
                if (!HasJournalLeaseAuthority(File.ReadAllText(stagingPath), 3, lease, root)) return 1;
                var candidate = Path.Combine(root, ".hermes-runtime", "venv-candidate-12345678");
                var generation = Path.Combine(root, ".hermes-runtime", "python", "generation-12345678");
                if (Directory.Exists(candidate)) Directory.Delete(candidate, true);
                if (Directory.Exists(generation)) Directory.Delete(generation, true);
                File.Delete(stagingPath);
            }
            return 0;
        }
        if (action == "activate") {
            if (!File.Exists(manifestPath) || File.Exists(statePath))
                return 1;
            if (mode == "activation-fail") {
                File.WriteAllText(statePath, "prepared");
                return 1;
            }
            if (File.Exists(receiptPath)) File.Copy(receiptPath, priorPath, true);
            else if (File.Exists(priorPath)) File.Delete(priorPath);
            File.WriteAllText(statePath, "active");
            return 0;
        }
        if (action == "publish-receipt") {
            if (mode == "receipt-fail" || !File.Exists(manifestPath) || !File.Exists(statePath))
                return 1;
            var manifest = File.ReadAllText(manifestPath);
            var targetHead = JsonField(manifest, "target_head");
            var branch = JsonField(manifest, "branch");
            var remote = JsonField(manifest, "remote");
            var targetRef = JsonField(manifest, "target_ref");
            if (String.IsNullOrEmpty(targetHead) || String.IsNullOrEmpty(branch) ||
                String.IsNullOrEmpty(remote) || String.IsNullOrEmpty(targetRef) ||
                !File.Exists(healthPath)) return 1;
            var health = File.ReadAllText(healthPath);
            if (JsonField(health, "invocation_id") != invocation ||
                JsonField(health, "lease_id") != lease ||
                JsonField(health, "target_head") != targetHead ||
                JsonField(health, "branch") != branch ||
                !health.Contains("\"build_exit_code\":0") ||
                !health.Contains("\"node_dependencies\":true") ||
                !health.Contains("\"desktop_rebuild\":true")) return 1;
            var timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            if (mode == "receipt-clock-rollback" || mode == "receipt-clock-rollback-resume-fail")
                timestamp -= 60;
            var receipt = "{\"schema_version\":1,\"invocation_id\":\"" + invocation +
                "\",\"lease_id\":\"" + lease +
                "\",\"mode\":\"git\",\"remote\":\"" + remote +
                "\",\"target_ref\":\"" + targetRef +
                "\",\"target_sha\":\"" + targetHead +
                "\",\"resulting_head\":\"" + targetHead +
                "\",\"archive_sha\":null,\"root\":\"" + Escape(root) +
                "\",\"branch\":\"" + branch + "\",\"timestamp\":" + timestamp +
                ",\"success\":true,\"gateway_resume_deferred\":true,\"health\":{" +
                "\"critical_syntax\":true,\"critical_imports\":true,\"dependencies\":true," +
                "\"node_dependencies\":true}}";
            if (mode == "oversized-receipt") receipt = new string('x', 70 * 1024);
            File.WriteAllText(receiptPath, receipt);
            File.WriteAllText(statePath, "receipt-published");
            return 0;
        }
        if (action == "rollback") {
            if (mode == "rollback-fail") return 1;
            if (File.Exists(stagingPath) &&
                !HasJournalLeaseAuthority(File.ReadAllText(stagingPath), 3, lease, root)) return 1;
            if (File.Exists(priorPath)) {
                File.Copy(priorPath, receiptPath, true);
                File.Delete(priorPath);
            } else if (File.Exists(receiptPath)) {
                File.Delete(receiptPath);
            }
            if (File.Exists(manifestPath)) {
                var manifest = File.ReadAllText(manifestPath);
                var candidateRel = JsonField(manifest, "candidate_rel");
                var candidate = String.IsNullOrEmpty(candidateRel)
                    ? null
                    : Path.Combine(root, candidateRel.Replace('/', Path.DirectorySeparatorChar));
                if (!String.IsNullOrEmpty(candidate) && Directory.Exists(candidate))
                    Directory.Delete(candidate, true);
                var provisionedRel = JsonField(manifest, "rel");
                var provisioned = String.IsNullOrEmpty(provisionedRel)
                    ? null
                    : Path.Combine(root, provisionedRel.Replace('/', Path.DirectorySeparatorChar));
                if (!String.IsNullOrEmpty(provisioned) && Directory.Exists(provisioned))
                    Directory.Delete(provisioned, true);
            }
            if (File.Exists(statePath)) File.Delete(statePath);
            if (File.Exists(manifestPath)) File.Delete(manifestPath);
            if (File.Exists(healthPath)) File.Delete(healthPath);
            if (File.Exists(stagingPath)) File.Delete(stagingPath);
            return 0;
        }
        if (action == "commit") {
            if (mode == "commit-fail" || !File.Exists(statePath) ||
                File.ReadAllText(statePath) != "receipt-published") return 1;
            if (File.Exists(priorPath)) File.Delete(priorPath);
            if (File.Exists(manifestPath)) {
                var candidateRel = JsonField(File.ReadAllText(manifestPath), "candidate_rel");
                var candidate = String.IsNullOrEmpty(candidateRel)
                    ? null
                    : Path.Combine(root, candidateRel.Replace('/', Path.DirectorySeparatorChar));
                if (!String.IsNullOrEmpty(candidate) && Directory.Exists(candidate))
                    Directory.Delete(candidate, true);
            }
            File.Delete(statePath);
            if (File.Exists(manifestPath)) File.Delete(manifestPath);
            if (File.Exists(healthPath)) File.Delete(healthPath);
            if (File.Exists(stagingPath)) File.Delete(stagingPath);
            return 0;
        }
        return 1;
    }

    public static int Main(string[] args) {
        if (args.Contains("--test-held-writer")) return HoldWriter();
        var pythonArgsCapture = Environment.GetEnvironmentVariable("HERMES_TEST_PYTHON_ARGS_CAPTURE");
        if (!String.IsNullOrEmpty(pythonArgsCapture))
            File.AppendAllText(pythonArgsCapture, String.Join(" ", args) + Environment.NewLine);
        var mode = Environment.GetEnvironmentVariable("HERMES_TEST_UPDATE_MODE") ?? "normal";
        if (args.Contains("hermes_cli.desktop_update_activation"))
            return Activation(args, mode);
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
            var markerCapture = Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_MARKER_CAPTURE");
            var marker = String.IsNullOrEmpty(home) ? null : Path.Combine(home, ".hermes-update-in-progress");
            if (!String.IsNullOrEmpty(marker) && !String.IsNullOrEmpty(markerCapture) && File.Exists(marker))
                File.Copy(marker, markerCapture, true);
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
        if (mode == "pre-plan-fail") {
            Console.Error.WriteLine("simulated pre-plan updater refusal");
            return 2;
        }
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
            if (mode == "build-fail") {
                Console.Error.WriteLine("simulated Desktop build failure");
                return 1;
            }
            var root = Directory.GetCurrentDirectory();
            var buildSha = Environment.GetEnvironmentVariable("GITHUB_SHA") ?? "";
            var buildBranch = Environment.GetEnvironmentVariable("GITHUB_REF_NAME") ?? "";
            var builtAt = mode == "build-stamp-offset"
                ? "2026-08-22T00:00:00.000+00:00"
                : "2026-08-22T00:00:00.000Z";
            var stamp = "{\"schemaVersion\":1,\"commit\":\"" + buildSha +
                "\",\"branch\":\"" + buildBranch +
                "\",\"builtAt\":\"" + builtAt + "\",\"dirty\":false,\"source\":\"ci\"}";
            var sourceStamp = Path.Combine(root, "apps", "desktop", "build", "install-stamp.json");
            var packagedStamp = Path.Combine(root, "apps", "desktop", "release", "win-unpacked", "resources", "install-stamp.json");
            Directory.CreateDirectory(Path.GetDirectoryName(sourceStamp));
            Directory.CreateDirectory(Path.GetDirectoryName(packagedStamp));
            File.WriteAllText(sourceStamp, stamp);
            File.WriteAllText(packagedStamp, stamp);
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
        if ((mode == "invalid-plan-fail" || mode == "ambiguous-plan-fail") &&
            !String.IsNullOrEmpty(leaseId) && !String.IsNullOrEmpty(invocationId) &&
            !String.IsNullOrEmpty(leasePath)) {
            var home = Path.GetDirectoryName(leasePath);
            var pending = Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".json");
            if (mode == "invalid-plan-fail") {
                File.WriteAllText(pending, "{invalid-plan");
            } else {
                WritePlan(home, Path.Combine(home, "hermes-agent"), invocationId, leaseId);
                File.Copy(
                    pending,
                    Path.Combine(home, ".hermes-gateway-resume-" + invocationId + ".completed"),
                    true
                );
            }
            if (File.Exists(leasePath) && parentLeaseOwnerPid > 0) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(
                    returned,
                    "\"owner_pid\"\\s*:\\s*\\d+",
                    "\"owner_pid\":" + parentLeaseOwnerPid
                );
                ReplaceLease(leasePath, returned, "update-return-invalid-plan");
            }
            Console.Error.WriteLine("simulated unproved recovery plan");
            return 2;
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

        if (mode == "archive") {
            if (!String.IsNullOrEmpty(leasePath) && parentLeaseOwnerPid > 0 && File.Exists(leasePath)) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(returned, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentLeaseOwnerPid);
                ReplaceLease(leasePath, returned, "update-return-archive");
            }
            Console.WriteLine("Archive staging completed without a transactional Git claim.");
            return 0;
        }

        if (mode == "acquisition-crash") {
            var home = Path.GetDirectoryName(leasePath);
            var root = Path.Combine(home, "hermes-agent");
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            var workspaceRel = "tmp/update-acquisition-1234567890abcdef12345678";
            var workspace = Path.Combine(home, workspaceRel.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(workspace);
            File.WriteAllText(Path.Combine(workspace, "partial.pack"), "partial");
            var journal = "{\"schema_version\":2,\"invocation_id\":\"" + invocationId +
                "\",\"lease_id\":\"" + leaseId + "\",\"root\":\"" + Escape(root) +
                "\",\"workspace_rel\":\"" + workspaceRel +
                "\",\"workspace_identity_sha256\":\"" + new string('e', 64) +
                "\",\"lease_authority\":" + LeaseAuthorityJson(leaseId, root, now) +
                ",\"created_at\":" + now + "}";
            File.WriteAllText(Path.Combine(home, ".hermes-update-acquisition.json"), journal);
            if (File.Exists(leasePath) && parentLeaseOwnerPid > 0) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(returned, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentLeaseOwnerPid);
                ReplaceLease(leasePath, returned, "update-return-acquisition-crash");
            }
            Console.Error.WriteLine("simulated crash during isolated package acquisition");
            return 1;
        }

        if (mode == "staging-crash") {
            var home = Path.GetDirectoryName(leasePath);
            var root = Path.Combine(home, "hermes-agent");
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            var candidateRel = ".hermes-runtime/venv-candidate-12345678";
            var generationRel = ".hermes-runtime/python/generation-12345678";
            Directory.CreateDirectory(Path.Combine(root, candidateRel.Replace('/', Path.DirectorySeparatorChar)));
            Directory.CreateDirectory(Path.Combine(root, generationRel.Replace('/', Path.DirectorySeparatorChar)));
            var staging = "{\"schema_version\":3,\"invocation_id\":\"" + invocationId +
                "\",\"lease_id\":\"" + leaseId + "\",\"root\":\"" + Escape(root) +
                "\",\"phase\":\"candidate-staging\",\"pre_update_head\":\"" + new string('a', 40) +
                "\",\"pre_update_branch\":\"main\",\"branch\":\"main\",\"selected_pre_head\":null" +
                ",\"target_head\":\"" + new string('b', 40) +
                "\",\"candidate\":{\"rel\":\"" + candidateRel +
                "\",\"identity_sha256\":\"" + new string('c', 64) + "\"}" +
                ",\"provisioned_generation\":{\"rel\":\"" + generationRel +
                "\",\"identity_sha256\":\"" + new string('d', 64) + "\"}" +
                ",\"lease_authority\":" + LeaseAuthorityJson(leaseId, root, now) +
                ",\"created_at\":" + now + ",\"updated_at\":" + now + "}";
            File.WriteAllText(Path.Combine(home, ".hermes-update-staging.json"), staging);
            if (File.Exists(leasePath) && parentLeaseOwnerPid > 0) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(returned, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentLeaseOwnerPid);
                ReplaceLease(leasePath, returned, "update-return-staging-crash");
            }
            Console.Error.WriteLine("simulated crash after staging journal publication");
            return 1;
        }

        if (!String.IsNullOrEmpty(leaseId) && !String.IsNullOrEmpty(leasePath)) {
            var home = Path.GetDirectoryName(leasePath);
            var root = Path.Combine(home, "hermes-agent");
            var branchIndex = Array.IndexOf(args, "--branch");
            var branch = branchIndex >= 0 && branchIndex + 1 < args.Length ? args[branchIndex + 1] : "main";
            var preUpdateBranch = JsonField(
                Environment.GetEnvironmentVariable("HERMES_TEST_PREFLIGHT_OUTPUT") ?? "",
                "branch"
            ) ?? "main";
            var candidateRel = ".hermes-runtime/venv-candidate-12345678";
            var provisionedRel = ".hermes-runtime/python/generation-12345678";
            var targetHead = mode == "target-mismatch"
                ? new string('f', 40)
                : new string('b', 40);
            var targetRemote = mode == "target-remote-mismatch" ? "mirror" : "origin";
            var targetRef = "refs/remotes/" + targetRemote + "/" + branch;
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            Directory.CreateDirectory(Path.Combine(root, candidateRel.Replace('/', Path.DirectorySeparatorChar)));
            Directory.CreateDirectory(Path.Combine(root, provisionedRel.Replace('/', Path.DirectorySeparatorChar)));
            var manifest = "{\"schema_version\":2,\"invocation_id\":\"" + invocationId +
                "\",\"lease_id\":\"" + leaseId + "\",\"root\":\"" + Escape(root) +
                "\",\"candidate_rel\":\"" + candidateRel +
                "\",\"provisioned_generation\":{\"rel\":\"" + provisionedRel +
                "\",\"identity_sha256\":\"" + new string('c', 64) + "\"}" +
                ",\"pre_update_head\":\"" + new string('a', 40) +
                "\",\"pre_update_branch\":\"" + preUpdateBranch +
                "\",\"selected_pre_head\":null" +
                ",\"target_head\":\"" + targetHead +
                "\",\"branch\":\"" + branch +
                "\",\"remote\":\"" + targetRemote + "\",\"target_ref\":\"" + targetRef +
                "\",\"prior_receipt_sha256\":null,\"python_health\":{" +
                "\"critical_imports\":true,\"critical_syntax\":true,\"dependencies\":true}," +
                "\"created_at\":" + now + "}";
            if (mode == "manifest-retire-crash") {
                var staging = "{\"schema_version\":3,\"invocation_id\":\"" + invocationId +
                    "\",\"lease_id\":\"" + leaseId + "\",\"root\":\"" + Escape(root) +
                    "\",\"phase\":\"candidate-staging\",\"pre_update_head\":\"" + new string('a', 40) +
                    "\",\"pre_update_branch\":\"" + preUpdateBranch +
                    "\",\"branch\":\"" + branch + "\",\"selected_pre_head\":null" +
                    ",\"target_head\":\"" + targetHead +
                    "\",\"candidate\":{\"rel\":\"" + candidateRel +
                    "\",\"identity_sha256\":\"" + new string('c', 64) + "\"}" +
                    ",\"provisioned_generation\":{\"rel\":\"" + provisionedRel +
                    "\",\"identity_sha256\":\"" + new string('d', 64) + "\"}" +
                    ",\"lease_authority\":" + LeaseAuthorityJson(leaseId, root, now) +
                    ",\"created_at\":" + now + ",\"updated_at\":" + now + "}";
                File.WriteAllText(Path.Combine(home, ".hermes-update-staging.json"), staging);
            }
            File.WriteAllText(Path.Combine(home, ".hermes-update-activation.json"), manifest);
            if (File.Exists(leasePath) && parentLeaseOwnerPid > 0) {
                var returned = File.ReadAllText(leasePath);
                returned = Regex.Replace(returned, "\"owner_pid\"\\s*:\\s*\\d+", "\"owner_pid\":" + parentLeaseOwnerPid);
                ReplaceLease(leasePath, returned, "update-return");
            }
            if (mode == "manifest-retire-crash") {
                Console.Error.WriteLine("simulated crash before exact staging journal retirement");
                return 1;
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
        if ((Environment.GetEnvironmentVariable("HERMES_TEST_UPDATE_MODE") ?? "") == "relaunch-fail")
            return 17;
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (DateTime.UtcNow < deadline) {
            foreach (var requestPath in Directory.GetFiles(home, ".hermes-update-relaunch-request-*.json")) {
                string requestRaw;
                try {
                    requestRaw = File.ReadAllText(requestPath);
                } catch (IOException) {
                    continue;
                }
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
                        string currentRequest = null;
                        try {
                            if (File.Exists(requestPath))
                                currentRequest = File.ReadAllText(requestPath);
                        } catch (IOException) {
                            File.Delete(exitTemp);
                            continue;
                        }
                        if (currentRequest == requestRaw)
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
    Compile-TestExecutable $Destination $source 'fake Desktop' -Windowless
}

function New-TestInstall([string]$Tag, [string]$FakeHermes) {
    $testHome = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-desktop-update-test-{0}-{1}" -f $Tag, [Guid]::NewGuid().ToString('N'))
    $root = Join-Path $testHome 'hermes-agent'
    $shimDir = Join-Path $root 'venv\Scripts'
    $baseDir = Join-Path $root '.hermes-runtime\python\test-generation'
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
        PreflightMarkerCapture = Join-Path $testHome 'marker-before-mutation.txt'
        PreflightArgsCapture = Join-Path $testHome 'preflight-args.txt'
        PythonArgsCapture = Join-Path $testHome 'python-args.txt'
        TopologyCapture = Join-Path $testHome 'contained-process-topology.txt'
        ResumeCapture = Join-Path $testHome 'deferred-resume-args.txt'
        ResumeRedirectorCapture = Join-Path $testHome 'deferred-resume-redirector.txt'
        ResumeContainmentCapture = Join-Path $testHome 'deferred-resume-containment.txt'
        ResumeGateCapture = Join-Path $testHome 'deferred-resume-gate.txt'
        ResumeTargetGateCapture = Join-Path $testHome 'deferred-resume-target-gate.txt'
        ResumeWriterCapture = Join-Path $testHome 'deferred-resume-writer.txt'
        ResumeWriterRelease = Join-Path $testHome 'deferred-resume-writer.release'
        ManifestUnsignedCapture = Join-Path $testHome 'deferred-resume-manifest-unsigned.txt'
        DelayedWrite = Join-Path $testHome 'deferred-resume-delayed-write.txt'
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
    Assert-Equal 3 $Code "$Label fails before mutation when the private base interpreter cannot be authenticated"
    Assert-True (-not (Test-Path -LiteralPath $Install.Sentinel)) "$Label never reaches update mutation"
    Assert-True (-not (Test-Path -LiteralPath $Install.ResumeCapture)) "$Label starts no recovery interpreter"
    Assert-True (-not (Test-Path -LiteralPath $Install.UpdateMarker)) "$Label releases the exact update marker"
    Assert-True (-not (Test-Path -LiteralPath $Install.Lease)) "$Label releases the exact bridge lease"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-update-in-progress.cas-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no update-marker CAS artifacts"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-venv-quiesce.cas-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no lease CAS artifacts"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.json' -File -ErrorAction SilentlyContinue).Count) "$Label creates no recovery plan before mutation"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.completed' -File -ErrorAction SilentlyContinue).Count) "$Label does not claim the recovery plan was completed"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $Install.Home -Filter '.hermes-gateway-resume-*.consume-*' -File -ErrorAction SilentlyContinue).Count) "$Label leaves no plan-consume artifacts"
}

function Assert-ManagedPhaseOrder([object]$Install, [string[]]$Patterns, [string]$Label) {
    $actual = @([System.IO.File]::ReadAllLines($Install.PythonArgsCapture) | Where-Object { $_ })
    Assert-Equal $Patterns.Count $actual.Count "$Label executes exactly the required managed phases"
    for ($phase = 0; $phase -lt $Patterns.Count; $phase++) {
        $matches = $phase -lt $actual.Count -and $actual[$phase] -match $Patterns[$phase]
        Assert-True $matches "$Label lifecycle phase $phase has the required exact order"
    }
}

function Assert-RolledBackActivationArtifacts([object]$Install, [string]$Label) {
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $Install.Home '.hermes-update-activation.json'))) "$Label retires its activation manifest"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $Install.Home '.hermes-update-activation-state.json'))) "$Label retires its activation state"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $Install.Home '.hermes-update-receipt.json'))) "$Label restores the prior receipt state"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $Install.Home '.hermes-update-desktop-health.json'))) "$Label retires its exact Desktop health proof"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $Install.Root '.hermes-runtime') -Filter 'venv-candidate-*' -Directory -ErrorAction SilentlyContinue).Count) "$Label removes its staged candidate generation"
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $Install.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) "$Label removes its provisioned Python generation"
    Assert-True (-not (Test-Path -LiteralPath $Install.UpdateMarker)) "$Label releases the update marker"
    Assert-True (-not (Test-Path -LiteralPath $Install.Lease)) "$Label releases the bridge lease"
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

function New-PreflightJson(
    [object]$Install,
    [bool]$Ok,
    [bool]$Ready,
    [string]$CurrentBranch = 'main',
    [string]$TargetBranch = 'main'
) {
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
        git = [ordered]@{
            head = ('a' * 40)
            branch = $CurrentBranch
            dirty = $false
            tracking_remote = 'origin'
            target_branch = $TargetBranch
            target_ref = "refs/remotes/origin/$TargetBranch"
            target_sha = ('b' * 40)
        }
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
    [string]$RelaunchExe = '',
    [AllowNull()][object]$SelfPreclaimAgeSeconds = $null,
    [string[]]$AdditionalArguments = @()
) {
    $oldOutput = $env:HERMES_TEST_PREFLIGHT_OUTPUT
    $oldCode = $env:HERMES_TEST_PREFLIGHT_CODE
    $oldStderr = $env:HERMES_TEST_PREFLIGHT_STDERR
    $oldSentinel = $env:HERMES_TEST_MUTATION_SENTINEL
    $oldLeasePath = $env:HERMES_TEST_LEASE_PATH
    $oldLeaseCapture = $env:HERMES_TEST_LEASE_CAPTURE
    $oldPreflightLeaseCapture = $env:HERMES_TEST_PREFLIGHT_LEASE_CAPTURE
    $oldPreflightMarkerCapture = $env:HERMES_TEST_PREFLIGHT_MARKER_CAPTURE
    $oldPreflightArgsCapture = $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE
    $oldPythonArgsCapture = $env:HERMES_TEST_PYTHON_ARGS_CAPTURE
    $oldTopologyCapture = $env:HERMES_TEST_TOPOLOGY_CAPTURE
    $oldResumeCapture = $env:HERMES_TEST_RESUME_CAPTURE
    $oldResumeRedirectorCapture = $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE
    $oldResumeContainmentCapture = $env:HERMES_TEST_RESUME_CONTAINMENT_CAPTURE
    $oldResumeGateCapture = $env:HERMES_TEST_RESUME_GATE_CAPTURE
    $oldResumeTargetGateCapture = $env:HERMES_TEST_RESUME_TARGET_GATE_CAPTURE
    $oldResumeWriterCapture = $env:HERMES_TEST_RESUME_WRITER_CAPTURE
    $oldResumeWriterRelease = $env:HERMES_TEST_RESUME_WRITER_RELEASE
    $oldDelayedWrite = $env:HERMES_TEST_DELAYED_WRITE_PATH
    $oldManifestUnsignedCapture = $env:HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE
    $oldBuildShaCapture = $env:HERMES_TEST_BUILD_SHA_CAPTURE
    $oldUpdateMode = $env:HERMES_TEST_UPDATE_MODE
    $oldTestMode = $env:HERMES_DESKTOP_UPDATE_TEST
    $oldPublishFail = $env:HERMES_TEST_RESULT_PUBLISH_FAIL
    $oldMarkerPath = $env:HERMES_TEST_UPDATE_MARKER_PATH
    $oldMarkerAge = $env:HERMES_TEST_UPDATE_MARKER_AGE_SECONDS
    $oldHandoffScript = $env:HERMES_TEST_HANDOFF_SCRIPT
    try {
        $env:HERMES_TEST_PREFLIGHT_OUTPUT = $PreflightOutput
        $env:HERMES_TEST_PREFLIGHT_CODE = "$PreflightCode"
        $env:HERMES_TEST_PREFLIGHT_STDERR = $PreflightStderr
        $env:HERMES_TEST_MUTATION_SENTINEL = $Install.Sentinel
        $env:HERMES_TEST_LEASE_PATH = $Install.Lease
        $env:HERMES_TEST_LEASE_CAPTURE = $Install.LeaseCapture
        $env:HERMES_TEST_PREFLIGHT_LEASE_CAPTURE = $Install.PreflightLeaseCapture
        $env:HERMES_TEST_PREFLIGHT_MARKER_CAPTURE = $Install.PreflightMarkerCapture
        $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE = $Install.PreflightArgsCapture
        $env:HERMES_TEST_PYTHON_ARGS_CAPTURE = $Install.PythonArgsCapture
        $env:HERMES_TEST_TOPOLOGY_CAPTURE = $Install.TopologyCapture
        $env:HERMES_TEST_RESUME_CAPTURE = $Install.ResumeCapture
        $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE = $Install.ResumeRedirectorCapture
        $env:HERMES_TEST_RESUME_CONTAINMENT_CAPTURE = $Install.ResumeContainmentCapture
        $env:HERMES_TEST_RESUME_GATE_CAPTURE = $Install.ResumeGateCapture
        $env:HERMES_TEST_RESUME_TARGET_GATE_CAPTURE = $Install.ResumeTargetGateCapture
        $env:HERMES_TEST_RESUME_WRITER_CAPTURE = $Install.ResumeWriterCapture
        $env:HERMES_TEST_RESUME_WRITER_RELEASE = $Install.ResumeWriterRelease
        $env:HERMES_TEST_DELAYED_WRITE_PATH = $Install.DelayedWrite
        $env:HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE = $Install.ManifestUnsignedCapture
        $env:HERMES_TEST_BUILD_SHA_CAPTURE = $Install.BuildShaCapture
        $env:HERMES_TEST_UPDATE_MODE = $UpdateMode
        $env:HERMES_DESKTOP_UPDATE_TEST = '1'
        if ($UpdateMode -eq 'result-publish-fail') {
            $env:HERMES_TEST_RESULT_PUBLISH_FAIL = '1'
        } else {
            Remove-Item Env:HERMES_TEST_RESULT_PUBLISH_FAIL -ErrorAction SilentlyContinue
        }
        $scriptToRun = $handoffScript
        if ($null -ne $SelfPreclaimAgeSeconds) {
            $scriptToRun = Join-Path $Install.Home 'self-preclaim-wrapper.ps1'
            $wrapper = @'
$startedAt = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + [int64]$env:HERMES_TEST_UPDATE_MARKER_AGE_SECONDS
$raw = "$PID`n$startedAt`n"
[System.IO.File]::WriteAllText(
    $env:HERMES_TEST_UPDATE_MARKER_PATH,
    $raw,
    (New-Object System.Text.UTF8Encoding($false))
)
[System.IO.File]::WriteAllText(
    ($env:HERMES_TEST_UPDATE_MARKER_PATH + '.original'),
    $raw,
    (New-Object System.Text.UTF8Encoding($false))
)
& $env:HERMES_TEST_HANDOFF_SCRIPT @args
exit $LASTEXITCODE
'@
            [System.IO.File]::WriteAllText($scriptToRun, $wrapper, (New-Object System.Text.UTF8Encoding($false)))
            $env:HERMES_TEST_UPDATE_MARKER_PATH = $Install.UpdateMarker
            $env:HERMES_TEST_UPDATE_MARKER_AGE_SECONDS = "$SelfPreclaimAgeSeconds"
            $env:HERMES_TEST_HANDOFF_SCRIPT = $handoffScript
        }
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptToRun,
            '-InstallRoot', $Install.Root, '-DesktopPid', '0', '-NoUi', '-TestMode'
        )
        if ($BridgeLeaseId) { $arguments += @('-BridgeLeaseId', $BridgeLeaseId) }
        if ($RelaunchExe) { $arguments += @('-RelaunchExe', $RelaunchExe) }
        if ($AdditionalArguments.Count -gt 0) { $arguments += $AdditionalArguments }
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
        $env:HERMES_TEST_PREFLIGHT_MARKER_CAPTURE = $oldPreflightMarkerCapture
        $env:HERMES_TEST_PREFLIGHT_ARGS_CAPTURE = $oldPreflightArgsCapture
        $env:HERMES_TEST_PYTHON_ARGS_CAPTURE = $oldPythonArgsCapture
        $env:HERMES_TEST_TOPOLOGY_CAPTURE = $oldTopologyCapture
        $env:HERMES_TEST_RESUME_CAPTURE = $oldResumeCapture
        $env:HERMES_TEST_RESUME_REDIRECTOR_CAPTURE = $oldResumeRedirectorCapture
        $env:HERMES_TEST_RESUME_CONTAINMENT_CAPTURE = $oldResumeContainmentCapture
        $env:HERMES_TEST_RESUME_GATE_CAPTURE = $oldResumeGateCapture
        $env:HERMES_TEST_RESUME_TARGET_GATE_CAPTURE = $oldResumeTargetGateCapture
        $env:HERMES_TEST_RESUME_WRITER_CAPTURE = $oldResumeWriterCapture
        $env:HERMES_TEST_RESUME_WRITER_RELEASE = $oldResumeWriterRelease
        $env:HERMES_TEST_DELAYED_WRITE_PATH = $oldDelayedWrite
        $env:HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE = $oldManifestUnsignedCapture
        $env:HERMES_TEST_BUILD_SHA_CAPTURE = $oldBuildShaCapture
        $env:HERMES_TEST_UPDATE_MODE = $oldUpdateMode
        $env:HERMES_DESKTOP_UPDATE_TEST = $oldTestMode
        $env:HERMES_TEST_RESULT_PUBLISH_FAIL = $oldPublishFail
        $env:HERMES_TEST_UPDATE_MARKER_PATH = $oldMarkerPath
        $env:HERMES_TEST_UPDATE_MARKER_AGE_SECONDS = $oldMarkerAge
        $env:HERMES_TEST_HANDOFF_SCRIPT = $oldHandoffScript
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

function Invoke-ActivationJournalRecoveryContracts([string]$FakeHermes) {
    $installs = @()
    try {
        $acquisitionCrash = New-TestInstall 'acquisition-journal-crash' $FakeHermes
        $installs += $acquisitionCrash
        $acquisitionCrashLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $acquisitionCrash $acquisitionCrashLeaseId
        $code = Invoke-TestHandoff $acquisitionCrash (New-PreflightJson $acquisitionCrash $true $true) 0 '' $acquisitionCrashLeaseId 'acquisition-crash'
        Assert-Equal 1 $code 'isolated package acquisition crash fails closed before live installation mutation'
        Assert-ManagedPhaseOrder $acquisitionCrash @(
            '^-B -m hermes_cli\.main update --preflight ',
            '^-B -m hermes_cli\.main update --yes ',
            '^-B -m hermes_cli\.desktop_update_activation rollback-source$',
            '^-B -m hermes_cli\.main update --resume-deferred-gateway '
        ) 'isolated package acquisition crash'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $acquisitionCrash.Home '.hermes-update-acquisition.json'))) 'acquisition crash consumes its exact authenticated journal'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $acquisitionCrash.Home 'tmp') -Filter 'update-acquisition-*' -Directory -ErrorAction SilentlyContinue).Count) 'acquisition crash removes only its journal-named workspace'
        Assert-True (-not (Test-Path -LiteralPath $acquisitionCrash.UpdateMarker)) 'acquisition crash releases the update marker'
        Assert-True (-not (Test-Path -LiteralPath $acquisitionCrash.Lease)) 'acquisition crash releases the bridge lease'
        Start-Sleep -Milliseconds 7500
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $acquisitionCrash.Home '.hermes-update-acquisition.json'))) 'acquisition journal is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $acquisitionCrash.Home 'tmp') -Filter 'update-acquisition-*' -Directory -ErrorAction SilentlyContinue).Count) 'acquisition workspace is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $acquisitionCrash.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'acquisition crash has no delayed temporary writer 7.5 seconds after return'

        $stagingCrash = New-TestInstall 'staging-journal-crash' $FakeHermes
        $installs += $stagingCrash
        $stagingCrashLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $stagingCrash $stagingCrashLeaseId
        $code = Invoke-TestHandoff $stagingCrash (New-PreflightJson $stagingCrash $true $true) 0 '' $stagingCrashLeaseId 'staging-crash'
        Assert-Equal 1 $code 'post-journal staging crash restores the exact prior source and fails closed'
        Assert-ManagedPhaseOrder $stagingCrash @(
            '^-B -m hermes_cli\.main update --preflight ',
            '^-B -m hermes_cli\.main update --yes ',
            '^-B -m hermes_cli\.desktop_update_activation rollback-source$',
            '^-B -m hermes_cli\.main update --resume-deferred-gateway '
        ) 'post-journal staging crash'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $stagingCrash.Home '.hermes-update-staging.json'))) 'staging crash consumes its exact authenticated journal'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $stagingCrash.Root '.hermes-runtime') -Filter 'venv-candidate-*' -Directory -ErrorAction SilentlyContinue).Count) 'staging crash removes only its journal-named candidate'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $stagingCrash.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'staging crash removes only its journal-named provisioned generation'
        Assert-True (-not (Test-Path -LiteralPath $stagingCrash.UpdateMarker)) 'staging crash releases the update marker'
        Assert-True (-not (Test-Path -LiteralPath $stagingCrash.Lease)) 'staging crash releases the bridge lease'
        Start-Sleep -Milliseconds 7500
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $stagingCrash.Home '.hermes-update-staging.json'))) 'staging journal is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $stagingCrash.Root '.hermes-runtime') -Filter 'venv-candidate-*' -Directory -ErrorAction SilentlyContinue).Count) 'staging candidate is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $stagingCrash.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'provisioned generation is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $stagingCrash.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'staging crash has no delayed temporary writer 7.5 seconds after return'

        $manifestRetireCrash = New-TestInstall 'manifest-staging-journal-coexistence' $FakeHermes
        $installs += $manifestRetireCrash
        $manifestRetireCrashLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $manifestRetireCrash $manifestRetireCrashLeaseId
        $code = Invoke-TestHandoff $manifestRetireCrash (New-PreflightJson $manifestRetireCrash $true $true) 0 '' $manifestRetireCrashLeaseId 'manifest-retire-crash'
        Assert-Equal 1 $code 'crash after manifest publication but before journal retirement fails closed'
        Assert-ManagedPhaseOrder $manifestRetireCrash @(
            '^-B -m hermes_cli\.main update --preflight ',
            '^-B -m hermes_cli\.main update --yes ',
            '^-B -m hermes_cli\.desktop_update_activation rollback$',
            '^-B -m hermes_cli\.main update --resume-deferred-gateway '
        ) 'coexisting manifest and staging journal crash'
        Assert-RolledBackActivationArtifacts $manifestRetireCrash 'coexisting manifest and staging journal crash'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $manifestRetireCrash.Home '.hermes-update-staging.json'))) 'manifest rollback consumes the exact coexisting staging journal'
        Start-Sleep -Milliseconds 7500
        Assert-RolledBackActivationArtifacts $manifestRetireCrash 'coexisting manifest and staging journal crash after 7.5 seconds'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $manifestRetireCrash.Home '.hermes-update-staging.json'))) 'coexisting staging journal is not recreated 7.5 seconds after return'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $manifestRetireCrash.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'coexisting manifest and staging crash has no delayed temporary writer 7.5 seconds after return'
    } finally {
        foreach ($install in $installs) {
            if ($install -and (Test-Path -LiteralPath $install.Home)) {
                Remove-Item -LiteralPath $install.Home -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$suiteRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-desktop-update-suite-{0}" -f [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $suiteRoot -Force | Out-Null
$fakeHermes = Join-Path $suiteRoot 'fake-hermes.exe'
$invalidVenvHomes = @()
$containmentWriterIdentities = @()

try {
    New-FakeHermes $fakeHermes
    $fakeDesktopTemplate = Join-Path $suiteRoot 'fake-desktop-template.exe'
    New-FakeDesktop $fakeDesktopTemplate

    . $desktopUpdateJsonHelper
    $fallbackStampRaw = '{"schemaVersion":1,"commit":"' + ('b' * 40) +
        '","branch":"main","builtAt":"2026-08-22T00:00:00.000Z","dirty":false,"source":"ci"}'
    $nativeStamp = ConvertFrom-DesktopStampJson $fallbackStampRaw
    Assert-True ($nativeStamp.builtAt -is [string] -and
        $nativeStamp.builtAt -eq '2026-08-22T00:00:00.000Z') 'shared production parser preserves canonical timestamp bytes'
    $fallbackStamp = ConvertFrom-DesktopStampJson $fallbackStampRaw -ForceCoreFallback
    Assert-True ($fallbackStamp.builtAt -is [string] -and
        $fallbackStamp.builtAt -eq '2026-08-22T00:00:00.000Z') 'shared forced parser seam preserves canonical timestamp bytes as a string'
    $fallbackOffset = ConvertFrom-DesktopStampJson ($fallbackStampRaw.Replace('.000Z', '.000+00:00')) -ForceCoreFallback
    Assert-True ($fallbackOffset.builtAt -is [string] -and
        $fallbackOffset.builtAt -eq '2026-08-22T00:00:00.000+00:00') 'shared forced parser seam does not canonicalize an equivalent offset timestamp'

    if ($ActivationJournalRecoveryOnly) {
        Invoke-ActivationJournalRecoveryContracts $fakeHermes
        if ($failures -gt 0) {
            throw "focused activation journal recovery failed with $failures assertion(s)"
        }
        Write-Host 'Focused activation journal recovery passed.' -ForegroundColor Green
        return
    }

    if ($ActivationBuildProofOnly) {
        $missingHelperProbe = Join-Path $suiteRoot 'missing-json-helper'
        $missingHelperRoot = Join-Path $missingHelperProbe 'install'
        $missingHelperHome = Join-Path $missingHelperProbe 'home'
        $missingHelperScript = Join-Path $missingHelperProbe 'desktop-update.ps1'
        New-Item -ItemType Directory -Path $missingHelperRoot, $missingHelperHome -Force | Out-Null
        Copy-Item -LiteralPath $handoffScript -Destination $missingHelperScript
        $oldMissingHelperHome = $env:HERMES_HOME
        $oldMissingHelperErrorAction = $ErrorActionPreference
        try {
            $env:HERMES_HOME = $missingHelperHome
            $ErrorActionPreference = 'Continue'
            & $powershellExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File $missingHelperScript -InstallRoot $missingHelperRoot -NoUi -TestMode 2>$null
            $missingHelperCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $oldMissingHelperErrorAction
            $env:HERMES_HOME = $oldMissingHelperHome
        }
        Assert-Equal 3 $missingHelperCode 'missing shared parser fails before updater initialization'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $missingHelperHome -Force -Recurse -ErrorAction SilentlyContinue).Count) 'missing shared parser creates no marker, lease, result, or log'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $missingHelperRoot -Force -Recurse -ErrorAction SilentlyContinue).Count) 'missing shared parser performs no install mutation'

        $focusedSuccess = New-TestInstall 'focused-activation-success' $fakeHermes
        $focusedSuccessDesktop = Join-Path $focusedSuccess.Home 'fake-desktop.exe'
        Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $focusedSuccessDesktop
        $focusedSuccessLease = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $focusedSuccess $focusedSuccessLease
        $code = Invoke-TestHandoff $focusedSuccess (New-PreflightJson $focusedSuccess $true $true) 0 '' $focusedSuccessLease 'normal' $focusedSuccessDesktop
        Assert-Equal 0 $code 'focused canonical stamp transaction completes through ACK and commit'
        Assert-ManagedPhaseOrder $focusedSuccess @(
            '^-B -m hermes_cli\.main update --preflight ',
            '^-B -m hermes_cli\.main update --yes ',
            '^-B -m hermes_cli\.desktop_update_activation activate$',
            '^-B -m hermes_cli\.main desktop ',
            '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
            '^-B -m hermes_cli\.desktop_update_activation commit$',
            '^-B -m hermes_cli\.main update --resume-deferred-gateway '
        ) 'focused canonical stamp transaction'

        $focusedOffset = New-TestInstall 'focused-noncanonical-stamp' $fakeHermes
        $focusedOffsetLease = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $focusedOffset $focusedOffsetLease
        $code = Invoke-TestHandoff $focusedOffset (New-PreflightJson $focusedOffset $true $true) 0 '' $focusedOffsetLease 'build-stamp-offset'
        Assert-Equal 6 $code 'focused build exit zero with +00:00 stamp remains failed'
        Assert-True (Test-Path -LiteralPath $focusedOffset.BuildShaCapture) 'focused +00:00 fixture proves build exit zero'
        Assert-RolledBackActivationArtifacts $focusedOffset 'focused +00:00 stamp rejection'
        if ($failures -gt 0) {
            throw "focused activation build proof failed with $failures assertion(s)"
        }
        Write-Host 'Focused activation build proof passed.' -ForegroundColor Green
        return
    }

    $realPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    Assert-True (Test-Path -LiteralPath $realPython -PathType Leaf) 'real managed Python is available for the containment gate integration test'
    if (Test-Path -LiteralPath $realPython -PathType Leaf) {
        $realGate = Join-Path $suiteRoot 'real-python-containment.gate'
        $realGateCapture = Join-Path $suiteRoot 'real-python-containment-armed.txt'
        $realGateProbe = Join-Path $suiteRoot 'real-python-containment-probe.py'
        [System.IO.File]::WriteAllText($realGate, 'wait', (New-Object System.Text.UTF8Encoding($false)))
        [System.IO.File]::WriteAllText(
            $realGateProbe,
            @'
import os
from pathlib import Path
from hermes_cli.update_cmd import _await_parent_gateway_containment
_await_parent_gateway_containment()
Path(os.environ["HERMES_TEST_REAL_GATE_CAPTURE"]).write_text("armed", encoding="utf-8")
'@,
            (New-Object System.Text.UTF8Encoding($false))
        )
        $realPsi = New-Object System.Diagnostics.ProcessStartInfo
        $realPsi.FileName = $realPython
        $realPsi.Arguments = '"' + $realGateProbe + '"'
        $realPsi.WorkingDirectory = $repoRoot
        $realPsi.UseShellExecute = $false
        $realPsi.CreateNoWindow = $true
        $realPsi.RedirectStandardOutput = $true
        $realPsi.RedirectStandardError = $true
        $realPsi.EnvironmentVariables['PYTHONPATH'] = $repoRoot
        $realPsi.EnvironmentVariables['HERMES_DEFERRED_GATEWAY_STARTUP_GATE'] = $realGate
        $realPsi.EnvironmentVariables['HERMES_TEST_REAL_GATE_CAPTURE'] = $realGateCapture
        $realGateProcess = [System.Diagnostics.Process]::Start($realPsi)
        Start-Sleep -Milliseconds 400
        Assert-True (-not $realGateProcess.HasExited -and -not (Test-Path -LiteralPath $realGateCapture)) 'real Python waits while the exact startup gate remains wait'
        $realGateNext = $realGate + '.next'
        $realGatePrevious = $realGate + '.previous'
        [System.IO.File]::WriteAllText($realGateNext, 'armed', (New-Object System.Text.UTF8Encoding($false)))
        [System.IO.File]::Replace($realGateNext, $realGate, $realGatePrevious, $true)
        Remove-Item -LiteralPath $realGatePrevious -Force
        $realGateProcess.WaitForExit(5000) | Out-Null
        Assert-True ($realGateProcess.HasExited -and $realGateProcess.ExitCode -eq 0 -and
            (Test-Path -LiteralPath $realGateCapture) -and
            [System.IO.File]::ReadAllText($realGateCapture) -eq 'armed') 'real Python proceeds only after the exact wait-to-armed transition'
        $realGateProcess.Dispose()
    }

    $containmentNoncooperative = New-TestInstall 'deferred-containment-noncooperative' $fakeHermes
    $containmentNoncooperativeDesktop = Join-Path $containmentNoncooperative.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $containmentNoncooperativeDesktop
    $containmentNoncooperativeLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentNoncooperative $containmentNoncooperativeLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentNoncooperative `
        -PreflightOutput (New-PreflightJson $containmentNoncooperative $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentNoncooperativeLeaseId `
        -UpdateMode 'containment-noncooperative-success' `
        -RelaunchExe $containmentNoncooperativeDesktop `
        -AdditionalArguments @('-TestDeferredGatewayPreAssignHoldMilliseconds', '500')
    Assert-Equal 0 $code 'a gate-ignorant recovery target starts only after its trusted wrapper is contained'
    $containmentNoncooperativeStates = if (Test-Path -LiteralPath $containmentNoncooperative.ResumeContainmentCapture) {
        @([System.IO.File]::ReadAllLines($containmentNoncooperative.ResumeContainmentCapture) | Where-Object { $_ })
    } else { @() }
    Assert-True ($containmentNoncooperativeStates -contains 'started:in-job' -and
        $containmentNoncooperativeStates -notcontains 'started:not-in-job') 'non-cooperative target first executes as a member of the private recovery Job'
    $containmentNoncooperativeWriter = Get-TestWriterIdentity $containmentNoncooperative.ResumeWriterCapture
    if ($containmentNoncooperativeWriter) { $containmentWriterIdentities += $containmentNoncooperativeWriter }
    Assert-Equal 'in-job' $(if ($containmentNoncooperativeWriter) { $containmentNoncooperativeWriter.Membership } else { '' }) 'a gate-ignorant target can spawn only contained descendants'
    Stop-TestWriterExact $containmentNoncooperativeWriter $containmentNoncooperative.ResumeWriterRelease
    $containmentNoncooperativeGatePath = [System.IO.File]::ReadAllText($containmentNoncooperative.ResumeGateCapture)
    Assert-NoDeferredGateArtifacts $containmentNoncooperativeGatePath 'gate-ignorant success'
    $containmentNoncooperativeTargetGatePath = if (Test-Path -LiteralPath $containmentNoncooperative.ResumeTargetGateCapture) {
        [System.IO.File]::ReadAllText($containmentNoncooperative.ResumeTargetGateCapture)
    } else { '' }
    Assert-True ($containmentNoncooperativeTargetGatePath -and
        -not [string]::Equals($containmentNoncooperativeGatePath, $containmentNoncooperativeTargetGatePath, [StringComparison]::OrdinalIgnoreCase)) 'gate-ignorant target receives only the separate target-retention gate'
    Assert-NoDeferredGateArtifacts $containmentNoncooperativeTargetGatePath 'gate-ignorant target success'

    $bufferedAdoption = New-TestInstall 'deferred-buffered-adoption' $fakeHermes
    $bufferedAdoptionDesktop = Join-Path $bufferedAdoption.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $bufferedAdoptionDesktop
    $bufferedAdoptionLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $bufferedAdoption $bufferedAdoptionLeaseId
    $code = Invoke-TestHandoff `
        -Install $bufferedAdoption `
        -PreflightOutput (New-PreflightJson $bufferedAdoption $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $bufferedAdoptionLeaseId `
        -UpdateMode 'normal' `
        -RelaunchExe $bufferedAdoptionDesktop `
        -AdditionalArguments @('-TestDeferredGatewayPostTargetGateHoldMilliseconds', '500')
    Assert-Equal 0 $code 'a buffered adoption frame proves the exact child after it clears the lease'
    $bufferedAdoptionLog = Get-Content -LiteralPath (Join-Path $bufferedAdoption.Home 'logs\desktop-update-handoff.log') -Raw
    Assert-True ($bufferedAdoptionLog -notmatch 'lost its bridge-quiesce lease') 'buffered adoption is not misclassified as lease loss'

    $containmentFastSuccess = New-TestInstall 'deferred-containment-fast-success' $fakeHermes
    $containmentFastSuccessLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentFastSuccess $containmentFastSuccessLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentFastSuccess `
        -PreflightOutput (New-PreflightJson $containmentFastSuccess $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentFastSuccessLeaseId `
        -UpdateMode 'containment-fast-success' `
        -AdditionalArguments @('-TestDeferredGatewayTargetRetentionHoldMilliseconds', '500')
    Assert-Equal 13 $code 'fast target exit during parent retention fails closed without reopening its PID'
    $fastWrapperGatePath = if (Test-Path -LiteralPath $containmentFastSuccess.ResumeGateCapture) {
        [System.IO.File]::ReadAllText($containmentFastSuccess.ResumeGateCapture)
    } else { '' }
    $fastTargetGatePath = if (Test-Path -LiteralPath $containmentFastSuccess.ResumeTargetGateCapture) {
        [System.IO.File]::ReadAllText($containmentFastSuccess.ResumeTargetGateCapture)
    } else { '' }
    Assert-True ($fastWrapperGatePath -and $fastTargetGatePath -and
        -not [string]::Equals($fastWrapperGatePath, $fastTargetGatePath, [StringComparison]::OrdinalIgnoreCase)) 'fast target remains behind a distinct parent-retention gate'
    Assert-NoDeferredGateArtifacts $fastWrapperGatePath 'fast target wrapper failure'
    Assert-NoDeferredGateArtifacts $fastTargetGatePath 'fast target retention failure'
    $fastArgs = if (Test-Path -LiteralPath $containmentFastSuccess.ResumeCapture) {
        [System.IO.File]::ReadAllText($containmentFastSuccess.ResumeCapture)
    } else { '' }
    $fastInvocationMatch = [regex]::Match($fastArgs, '--invocation-id\s+([^\s]+)')
    $fastStateRestored = $false
    if ($fastInvocationMatch.Success) {
        $fastPrefix = Join-Path $containmentFastSuccess.Home ('.hermes-gateway-resume-' + $fastInvocationMatch.Groups[1].Value)
        $fastStateRestored = (Test-Path -LiteralPath ($fastPrefix + '.json') -PathType Leaf) -and
            -not (Test-Path -LiteralPath ($fastPrefix + '.prepared')) -and
            -not (Test-Path -LiteralPath ($fastPrefix + '.prepared-runtime.json')) -and
            -not (Test-Path -LiteralPath ($fastPrefix + '.completed'))
    }
    Assert-True $fastStateRestored 'fast target failure restores pending authority and removes prepared state'

    $containmentSuccess = New-TestInstall 'deferred-containment-success' $fakeHermes
    $containmentSuccessDesktop = Join-Path $containmentSuccess.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $containmentSuccessDesktop
    $containmentSuccessLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentSuccess $containmentSuccessLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentSuccess `
        -PreflightOutput (New-PreflightJson $containmentSuccess $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentSuccessLeaseId `
        -UpdateMode 'containment-success' `
        -RelaunchExe $containmentSuccessDesktop `
        -AdditionalArguments @(
            '-TestDeferredGatewayPreAssignHoldMilliseconds', '250',
            '-TestDeferredGatewayWrapperExitHoldMilliseconds', '1250'
        )
    if ($code -ne 0) {
        Get-Content -LiteralPath (Join-Path $containmentSuccess.Home 'logs\desktop-update-handoff.log') -ErrorAction SilentlyContinue
    }
    Assert-Equal 0 $code 'deferred gateway writer starts only after its parent is assigned and terminal proof disarms containment'
    $containmentSuccessPythonArgs = @(
        [System.IO.File]::ReadAllLines($containmentSuccess.PythonArgsCapture) |
            Where-Object { $_ }
    )
    Assert-Equal 1 (@($containmentSuccessPythonArgs | Where-Object {
        $_ -match '^-B -m hermes_cli\.desktop_update_activation activate$'
    }).Count) 'the real handoff activates exactly one staged candidate before recovery'
    $commitIndex = [Array]::IndexOf(
        $containmentSuccessPythonArgs,
        '-B -m hermes_cli.desktop_update_activation commit'
    )
    $resumeIndex = -1
    for ($index = 0; $index -lt $containmentSuccessPythonArgs.Count; $index++) {
        if ($containmentSuccessPythonArgs[$index] -match '^-B -m hermes_cli\.main update --resume-deferred-gateway ') {
            $resumeIndex = $index
            break
        }
    }
    Assert-True ($commitIndex -ge 0 -and $resumeIndex -gt $commitIndex) 'the verified installation commits before any gateway process restart'
    $nativeUnsignedCapture = $containmentSuccess.ManifestUnsignedCapture + '.native'
    Assert-True ((Test-Path -LiteralPath $containmentSuccess.ManifestUnsignedCapture -PathType Leaf) -and
        (Test-Path -LiteralPath $nativeUnsignedCapture -PathType Leaf) -and
        [string]::Equals(
            [System.IO.File]::ReadAllText($containmentSuccess.ManifestUnsignedCapture),
            [System.IO.File]::ReadAllText($nativeUnsignedCapture),
            [StringComparison]::Ordinal
        )) 'native survivor proof authenticates the publisher canonical bytes exactly'
    $containmentSuccessStates = if (Test-Path -LiteralPath $containmentSuccess.ResumeContainmentCapture) {
        @([System.IO.File]::ReadAllLines($containmentSuccess.ResumeContainmentCapture) | Where-Object { $_ })
    } else { @() }
    Assert-True ($containmentSuccessStates.Count -ge 3 -and
        @($containmentSuccessStates | Where-Object { $_ -match '^waiting:' }).Count -ge 1 -and
        $containmentSuccessStates -contains 'armed:in-job') 'resume observes wait then exact inherited Job membership before any writer spawn'
    $containmentSuccessWriter = Get-TestWriterIdentity $containmentSuccess.ResumeWriterCapture
    if ($containmentSuccessWriter) { $containmentWriterIdentities += $containmentSuccessWriter }
    Assert-True ($null -ne $containmentSuccessWriter) 'contained success records the exact writer generation'
    Assert-Equal 'in-job' $(if ($containmentSuccessWriter) { $containmentSuccessWriter.Membership } else { '' }) 'successful writer inherits the private recovery Job'
    Assert-True (Test-TestWriterLive $containmentSuccessWriter) 'terminal proof disarms kill-on-close before the recovery Job handle is released'
    $containmentSuccessLogPath = Join-Path $containmentSuccess.Home 'logs\desktop-update-handoff.log'
    $containmentSuccessLog = [System.IO.File]::ReadAllText($containmentSuccessLogPath)
    $containmentSuccessResumeArgs = [System.IO.File]::ReadAllText($containmentSuccess.ResumeCapture)
    $containmentInvocationMatch = [regex]::Match($containmentSuccessResumeArgs, '--invocation-id\s+([^\s]+)')
    $containmentSuccessGatePath = [System.IO.File]::ReadAllText($containmentSuccess.ResumeGateCapture)
    $containmentSuccessTargetGatePath = if (Test-Path -LiteralPath $containmentSuccess.ResumeTargetGateCapture) {
        [System.IO.File]::ReadAllText($containmentSuccess.ResumeTargetGateCapture)
    } else { '' }
    Assert-True ($containmentSuccessTargetGatePath -and
        -not [string]::Equals($containmentSuccessGatePath, $containmentSuccessTargetGatePath, [StringComparison]::OrdinalIgnoreCase)) 'target starts behind a second gate distinct from wrapper assignment authorization'
    $gatewayDiagnosticLines = @([System.IO.File]::ReadAllLines($containmentSuccessLogPath) |
        Where-Object { $_ -match 'gateway-resume(?:!?)\|' })
    Assert-True ($containmentInvocationMatch.Success -and
        $containmentSuccessLog.IndexOf($containmentInvocationMatch.Groups[1].Value, [StringComparison]::OrdinalIgnoreCase) -lt 0) 'handoff diagnostics never expose the exact deferred invocation capability'
    Assert-True ($containmentSuccessLog.IndexOf($containmentSuccessLeaseId, [StringComparison]::OrdinalIgnoreCase) -lt 0) 'handoff diagnostics never expose the exact bridge lease capability'
    Assert-True ($containmentSuccessLog.IndexOf($containmentSuccessGatePath, [StringComparison]::OrdinalIgnoreCase) -lt 0) 'handoff diagnostics never expose the exact deferred startup gate path'
    Assert-True ($containmentSuccessLog.IndexOf('CHILD_DIAGNOSTIC_SECRET_8f86b783', [StringComparison]::OrdinalIgnoreCase) -lt 0) 'handoff diagnostics redact secret-like child output'
    Assert-True ($containmentSuccessLog -match '\[HERMES_HOME\]' -and
        $containmentSuccessLog -match '\[TEMP\]') 'handoff diagnostics redact mixed-case Hermes and TEMP paths to fixed labels'
    Assert-True ($containmentSuccessLog -notmatch '"event"\s*:\s*"deferred-gateway-lease-adopted"') 'handoff diagnostics replace the raw adoption protocol frame with a symbolic status'
    Assert-True ($gatewayDiagnosticLines.Count -le 8 -and
        @($gatewayDiagnosticLines | Where-Object { $_.Length -gt 640 }).Count -eq 0) 'gateway recovery diagnostics have a fixed line-count and line-length bound'
    Stop-TestWriterExact $containmentSuccessWriter $containmentSuccess.ResumeWriterRelease
    Assert-NoDeferredGateArtifacts $containmentSuccessGatePath 'contained success'
    Assert-NoDeferredGateArtifacts $containmentSuccessTargetGatePath 'contained target success'

    $containmentBoundaryFailure = New-TestInstall 'deferred-containment-native-boundary-failure' $fakeHermes
    $containmentBoundaryLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentBoundaryFailure $containmentBoundaryLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentBoundaryFailure `
        -PreflightOutput (New-PreflightJson $containmentBoundaryFailure $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentBoundaryLeaseId `
        -UpdateMode 'containment-success' `
        -AdditionalArguments @('-TestDeferredGatewayNativeBoundaryFailure')
    Assert-Equal 13 $code 'a runtime identity change at the final native boundary fails closed'
    $containmentBoundaryWriter = Get-TestWriterIdentity $containmentBoundaryFailure.ResumeWriterCapture
    if ($containmentBoundaryWriter) { $containmentWriterIdentities += $containmentBoundaryWriter }
    Assert-True ($null -ne $containmentBoundaryWriter -and
        -not (Test-TestWriterLive $containmentBoundaryWriter)) 'native-boundary failure drains every retained runtime generation before return'
    $containmentBoundaryArgs = [System.IO.File]::ReadAllText($containmentBoundaryFailure.ResumeCapture)
    $containmentBoundaryInvocation = [regex]::Match($containmentBoundaryArgs, '--invocation-id\s+([^\s]+)').Groups[1].Value
    $containmentBoundaryPrefix = Join-Path $containmentBoundaryFailure.Home ('.hermes-gateway-resume-' + $containmentBoundaryInvocation)
    Assert-True ((Test-Path -LiteralPath ($containmentBoundaryPrefix + '.json') -PathType Leaf) -and
        -not (Test-Path -LiteralPath ($containmentBoundaryPrefix + '.prepared')) -and
        -not (Test-Path -LiteralPath ($containmentBoundaryPrefix + '.prepared-runtime.json')) -and
        -not (Test-Path -LiteralPath ($containmentBoundaryPrefix + '.completed')) -and
        @(Get-ChildItem -LiteralPath $containmentBoundaryFailure.Home -Filter ('.hermes-gateway-resume-' + $containmentBoundaryInvocation + '.consume-*') -File -ErrorAction SilentlyContinue).Count -eq 0) 'native-boundary failure restores exact pending authority and retires prepared artifacts'
    $containmentBoundaryGatePath = [System.IO.File]::ReadAllText($containmentBoundaryFailure.ResumeGateCapture)
    Assert-NoDeferredGateArtifacts $containmentBoundaryGatePath 'native-boundary failure'

    $containmentDrain = New-TestInstall 'deferred-containment-drain' $fakeHermes
    $containmentDrainLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentDrain $containmentDrainLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentDrain `
        -PreflightOutput (New-PreflightJson $containmentDrain $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentDrainLeaseId `
        -UpdateMode 'containment-failure-drain'
    Assert-Equal 13 $code 'failed deferred gateway recovery remains a terminal recovery failure'
    $containmentDrainWriter = Get-TestWriterIdentity $containmentDrain.ResumeWriterCapture
    if ($containmentDrainWriter) { $containmentWriterIdentities += $containmentDrainWriter }
    Assert-True ($null -ne $containmentDrainWriter) 'failed recovery starts one contained writer fixture'
    Assert-Equal 'in-job' $(if ($containmentDrainWriter) { $containmentDrainWriter.Membership } else { '' }) 'failed writer inherits the private recovery Job'
    Assert-True (-not (Test-TestWriterLive $containmentDrainWriter)) 'failure drains every recovery Job process before the handoff returns'
    $containmentDrainGatePath = [System.IO.File]::ReadAllText($containmentDrain.ResumeGateCapture)
    Assert-NoDeferredGateArtifacts $containmentDrainGatePath 'contained failure'
    Assert-True (-not (Test-Path -LiteralPath $containmentDrain.DelayedWrite)) 'failed recovery returns before no delayed descendant write is possible'

    $containmentAssignFailure = New-TestInstall 'deferred-containment-assign-failure' $fakeHermes
    $containmentAssignFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $containmentAssignFailure $containmentAssignFailureLeaseId
    $code = Invoke-TestHandoff `
        -Install $containmentAssignFailure `
        -PreflightOutput (New-PreflightJson $containmentAssignFailure $true $true) `
        -PreflightCode 0 `
        -BridgeLeaseId $containmentAssignFailureLeaseId `
        -UpdateMode 'containment-success' `
        -AdditionalArguments @('-TestDeferredGatewayAssignFailure', '-TestDeferredGatewayInitialKillFailure')
    Assert-Equal 13 $code 'assignment failure cannot escape into an uncontained gateway recovery'
    $containmentAssignStates = if (Test-Path -LiteralPath $containmentAssignFailure.ResumeContainmentCapture) {
        @([System.IO.File]::ReadAllLines($containmentAssignFailure.ResumeContainmentCapture) | Where-Object { $_ })
    } else { @() }
    Assert-True ($containmentAssignStates.Count -ge 1 -and
        @($containmentAssignStates | Where-Object { $_ -match '^waiting:' }).Count -ge 1 -and
        @($containmentAssignStates | Where-Object { $_ -match 'armed' }).Count -eq 0) 'assignment failure leaves its trusted wrapper waiting and never arms target launch'
    Assert-True (-not (Test-Path -LiteralPath $containmentAssignFailure.ResumeWriterCapture)) 'assignment and initial-kill failure never permit a writer to spawn'
    $waitingState = @($containmentAssignStates | Where-Object { $_ -match '^waiting:' } | Select-Object -First 1)
    $waitingMatch = if ($waitingState.Count -eq 1) {
        [regex]::Match($waitingState[0], '^waiting:(\d+):(\d+)$')
    } else { $null }
    $waitingIdentity = if ($waitingMatch -and $waitingMatch.Success) {
        [pscustomobject]@{ Pid = [int]$waitingMatch.Groups[1].Value; StartedAtTicks = [int64]$waitingMatch.Groups[2].Value }
    } else { $null }
    Assert-True ($null -ne $waitingIdentity -and -not (Test-TestWriterLive $waitingIdentity)) 'unassigned resume child is proven dead before the handoff returns'
    $assignmentGatePath = if (Test-Path -LiteralPath $containmentAssignFailure.ResumeGateCapture) {
        [System.IO.File]::ReadAllText($containmentAssignFailure.ResumeGateCapture)
    } else { '' }
    Assert-True ($assignmentGatePath -and -not (Test-Path -LiteralPath $assignmentGatePath)) 'assignment failure removes its exact startup gate only after the child is dead'
    Assert-NoDeferredGateArtifacts $assignmentGatePath 'assignment failure'
    Start-Sleep -Seconds 7
    Assert-True (-not (Test-TestWriterLive $containmentSuccessWriter)) 'released success fixture has no delayed writer seven seconds later'
    Assert-True (-not (Test-TestWriterLive $containmentNoncooperativeWriter)) 'gate-ignorant writer has no delayed process seven seconds later'
    Assert-True (-not (Test-TestWriterLive $containmentDrainWriter)) 'failed recovery writer remains absent seven seconds after return'
    Assert-True (-not (Test-TestWriterLive $containmentBoundaryWriter)) 'native-boundary runtime remains absent seven seconds after return'
    Assert-True (-not (Test-Path -LiteralPath $containmentAssignFailure.ResumeWriterCapture)) 'assignment failure creates no delayed writer seven seconds after return'
    Assert-True (-not (Test-TestWriterLive $waitingIdentity)) 'unassigned resume generation remains absent seven seconds after return'
    Assert-True (-not $assignmentGatePath -or -not (Test-Path -LiteralPath $assignmentGatePath)) 'startup gate is not recreated seven seconds after return'
    Assert-True (-not (Test-Path -LiteralPath $containmentDrain.DelayedWrite)) 'failed recovery descendant cannot perform a delayed post-return write'
    Assert-True (-not (Test-Path -LiteralPath $containmentBoundaryFailure.DelayedWrite)) 'native-boundary failure cannot perform a delayed post-return write'
    Assert-NoDeferredGateArtifacts $containmentNoncooperativeGatePath 'gate-ignorant success after seven seconds'
    Assert-NoDeferredGateArtifacts $containmentNoncooperativeTargetGatePath 'gate-ignorant target success after seven seconds'
    Assert-NoDeferredGateArtifacts $fastWrapperGatePath 'fast target wrapper failure after seven seconds'
    Assert-NoDeferredGateArtifacts $fastTargetGatePath 'fast target retention failure after seven seconds'
    Assert-NoDeferredGateArtifacts $containmentSuccessGatePath 'contained success after seven seconds'
    Assert-NoDeferredGateArtifacts $containmentSuccessTargetGatePath 'contained target success after seven seconds'
    Assert-NoDeferredGateArtifacts $containmentDrainGatePath 'contained failure after seven seconds'
    Assert-NoDeferredGateArtifacts $containmentBoundaryGatePath 'native-boundary failure after seven seconds'
    Assert-NoDeferredGateArtifacts $assignmentGatePath 'assignment failure after seven seconds'

    $logContention = New-TestInstall 'handoff-log-contention' $fakeHermes
    $logContentionLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $logContention $logContentionLeaseId
    $logContentionDir = Join-Path $logContention.Home 'logs'
    New-Item -ItemType Directory -Path $logContentionDir -Force | Out-Null
    $logContentionPrimary = Join-Path $logContentionDir 'desktop-update-handoff.log'
    [System.IO.File]::WriteAllText($logContentionPrimary, 'locked-primary')
    $logLock = [System.IO.File]::Open(
        $logContentionPrimary,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $code = Invoke-TestHandoff `
            -Install $logContention `
            -PreflightOutput (New-PreflightJson $logContention $true $true) `
            -PreflightCode 0 `
            -BridgeLeaseId $logContentionLeaseId `
            -UpdateMode 'pre-plan-fail'
    } finally {
        $logLock.Dispose()
    }
    Assert-Equal 13 $code 'primary handoff-log contention does not alter the updater terminal result'
    Assert-Equal 'locked-primary' ([System.IO.File]::ReadAllText($logContentionPrimary)) 'contended primary handoff log is never overwritten or truncated'
    $fallbackLogs = @(Get-ChildItem -LiteralPath $logContentionDir -Filter 'desktop-update-handoff-fallback-*.log' -File -ErrorAction SilentlyContinue)
    Assert-Equal 1 $fallbackLogs.Count 'one durable per-attempt fallback log records primary-log contention'
    if ($fallbackLogs.Count -eq 1) {
        $fallbackRaw = [System.IO.File]::ReadAllText($fallbackLogs[0].FullName)
        Assert-True ($fallbackLogs[0].Length -le 131072) 'fallback handoff diagnostics remain byte-bounded'
        Assert-True ($fallbackRaw -match 'primary handoff log unavailable' -and
            $fallbackRaw -notmatch 'Add-Content|being used by another process|IOException') 'fallback honestly signals omission without raw PowerShell or system errors'
    }
    Assert-True (Test-Path -LiteralPath $logContention.Result) 'log contention still publishes the ordinary terminal handoff result'

    if (-not $DeferredGatewayContainmentOnly) {

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

    $markerNow = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $selfPreclaim = New-TestInstall 'self-update-marker' $fakeHermes
    $selfPreclaimLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $selfPreclaim $selfPreclaimLeaseId
    $code = Invoke-TestHandoff $selfPreclaim (New-PreflightJson $selfPreclaim $true $true) 0 '' $selfPreclaimLeaseId 'pre-plan-fail' '' 2
    Assert-Equal 13 $code 'exact live self preclaim is adopted before the bounded failure'
    $selfPreclaimOriginal = [System.IO.File]::ReadAllText($selfPreclaim.UpdateMarker + '.original')
    Assert-Equal $selfPreclaimOriginal ([System.IO.File]::ReadAllText($selfPreclaim.PreflightMarkerCapture)) 'self preclaim is adopted without rewriting its exact bytes'
    Assert-True (-not (Test-Path -LiteralPath $selfPreclaim.UpdateMarker)) 'adopted self preclaim is released by its original timestamp'

    $impossibleSelfPreclaim = New-TestInstall 'impossible-self-update-marker' $fakeHermes
    $impossibleSelfLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $impossibleSelfPreclaim $impossibleSelfLeaseId
    $code = Invoke-TestHandoff $impossibleSelfPreclaim (New-PreflightJson $impossibleSelfPreclaim $true $true) 0 '' $impossibleSelfLeaseId 'normal' '' -120
    Assert-Equal 8 $code 'self PID with an impossible process generation is refused'
    $impossibleOriginal = [System.IO.File]::ReadAllText($impossibleSelfPreclaim.UpdateMarker + '.original')
    Assert-Equal $impossibleOriginal ([System.IO.File]::ReadAllText($impossibleSelfPreclaim.UpdateMarker)) 'impossible self claim remains byte-for-byte unchanged'

    foreach ($malformedMarkerRaw in @("$PID`n", "not-a-pid`n$markerNow`n")) {
        $malformedMarker = New-TestInstall 'malformed-update-marker' $fakeHermes
        [System.IO.File]::WriteAllText($malformedMarker.UpdateMarker, $malformedMarkerRaw)
        $malformedBefore = [System.IO.File]::ReadAllText($malformedMarker.UpdateMarker)
        $malformedLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $malformedMarker $malformedLeaseId
        $code = Invoke-TestHandoff $malformedMarker (New-PreflightJson $malformedMarker $true $true) 0 '' $malformedLeaseId
        Assert-Equal 8 $code 'readable malformed update marker blocks atomic claim'
        Assert-True (-not (Test-Path -LiteralPath $malformedMarker.Sentinel)) 'readable malformed marker never reaches mutation'
        Assert-Equal $malformedBefore ([System.IO.File]::ReadAllText($malformedMarker.UpdateMarker)) 'readable malformed marker bytes remain unchanged'
        $invalidVenvHomes += $malformedMarker.Home
    }

    $missingLease = New-TestInstall 'missing-lease' $fakeHermes
    $missingLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    $code = Invoke-TestHandoff $missingLease (New-PreflightJson $missingLease $true $true) 0 '' $missingLeaseId
    Assert-Equal 8 $code 'expected but missing bridge lease aborts'
    Assert-True (-not (Test-Path -LiteralPath $missingLease.Sentinel)) 'missing expected lease never reaches mutation'

    $unreadableMarker = New-TestInstall 'unreadable-update-marker' $fakeHermes
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

    $deadMarker = New-TestInstall 'dead-update-marker' $fakeHermes
    $deadMarkerRaw = "2147483647`n$markerNow`n"
    [System.IO.File]::WriteAllText($deadMarker.UpdateMarker, $deadMarkerRaw)
    $deadMarkerLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $deadMarker $deadMarkerLeaseId
    $code = Invoke-TestHandoff $deadMarker (New-PreflightJson $deadMarker $true $true) 0 '' $deadMarkerLeaseId 'pre-plan-fail'
    Assert-Equal 13 $code 'valid proven-dead update marker may be reclaimed before the bounded failure'
    Assert-True (-not (Test-Path -LiteralPath $deadMarker.UpdateMarker)) 'reclaimed dead marker is removed only after exact ownership proof'

    $leased = New-TestInstall 'lease-adoption' $fakeHermes
    $leasedDesktop = Join-Path $leased.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $leasedDesktop
    $leaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $leased $leaseId
    Assert-Equal 1 (@([System.IO.File]::ReadAllLines((Join-Path $leased.Root 'venv\pyvenv.cfg')) | Where-Object { $_ -match '^\s*executable\s*=' }).Count) 'explicit-executable recovery fixture has one executable key'
    $code = Invoke-TestHandoff $leased (New-PreflightJson $leased $true $true) 0 'preflight diagnostic' $leaseId 'normal' $leasedDesktop
    Assert-Equal 0 $code 'ready preflight with matching lease completes'
    Assert-Equal ('b' * 40) ([System.IO.File]::ReadAllText($leased.BuildShaCapture)) 'git rebuild stamp is pinned to the staged target HEAD'
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
        Assert-True ($preflightRecord[0] -match '^-B -m hermes_cli\.main update ') 'preflight bypasses the console shim and disables bytecode writes'
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
        Assert-True ([System.IO.File]::ReadAllText($leased.Sentinel) -match '^-B -m hermes_cli\.main update ') 'mutation path bypasses the console shim and disables bytecode writes'
    }
    Assert-True (Test-Path -LiteralPath $leased.PythonArgsCapture) 'every managed phase records its public argv seam'
    if (Test-Path -LiteralPath $leased.PythonArgsCapture) {
        $pythonArgLines = @([System.IO.File]::ReadAllLines($leased.PythonArgsCapture) | Where-Object { $_ })
        $requiredOrder = @(
            '^-B -m hermes_cli\.main update --preflight ',
            '^-B -m hermes_cli\.main update --yes ',
            '^-B -m hermes_cli\.desktop_update_activation activate$',
            '^-B -m hermes_cli\.main desktop ',
            '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
            '^-B -m hermes_cli\.main update --resume-deferred-gateway ',
            '^-B -m hermes_cli\.desktop_update_activation commit$'
        )
        Assert-Equal $requiredOrder.Count $pythonArgLines.Count 'successful handoff executes exactly the seven required managed phases'
        for ($phase = 0; $phase -lt $requiredOrder.Count; $phase++) {
            $matches = $phase -lt $pythonArgLines.Count -and $pythonArgLines[$phase] -match $requiredOrder[$phase]
            Assert-True $matches "transactional lifecycle phase $phase has the required exact order"
        }
    }
    Assert-True (Test-Path -LiteralPath $leased.ResumeCapture) 'trusted deferred gateway resume argv is captured'
    if ((Test-Path -LiteralPath $leased.Sentinel) -and (Test-Path -LiteralPath $leased.ResumeCapture)) {
        $updateInvocation = [regex]::Match([System.IO.File]::ReadAllText($leased.Sentinel), '--invocation-id\s+(\S+)').Groups[1].Value
        $resumeInvocation = [regex]::Match([System.IO.File]::ReadAllText($leased.ResumeCapture), '--invocation-id\s+(\S+)').Groups[1].Value
        Assert-True ($updateInvocation -match '^invocation-[A-Za-z0-9._-]{16,128}$') 'update receives one valid parent-generated invocation id'
        Assert-Equal $updateInvocation $resumeInvocation 'update receipt plan and trusted resume share one invocation id'
    }
    Assert-True (Test-Path -LiteralPath $leased.Result) 'versioned handoff result is written after terminal cleanup'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $leased.Home '.hermes-update-activation.json'))) 'acknowledged success commits the activation manifest'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $leased.Home '.hermes-update-activation-state.json'))) 'acknowledged success removes retained rollback state'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $leased.Home '.hermes-update-desktop-health.json'))) 'acknowledged success consumes and retires the exact Desktop health proof'
    Assert-Equal 1 (@(Get-ChildItem -LiteralPath (Join-Path $leased.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'acknowledged success retains the provisioned Python generation used by the active venv'
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

    $crossBranch = New-TestInstall 'cross-branch-transaction' $fakeHermes
    $crossBranchDesktop = Join-Path $crossBranch.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $crossBranchDesktop
    $crossBranchLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $crossBranch $crossBranchLeaseId
    $crossBranchTarget = 'disposable/update-target'
    $code = Invoke-TestHandoff `
        $crossBranch `
        (New-PreflightJson $crossBranch $true $true 'fork-integration' $crossBranchTarget) `
        0 '' $crossBranchLeaseId 'normal' $crossBranchDesktop $null `
        @('-Branch', $crossBranchTarget)
    Assert-Equal 0 $code 'clean cross-branch retarget commits only after exact Desktop readiness'
    if (Test-Path -LiteralPath $crossBranch.PreflightArgsCapture) {
        $crossBranchPreflight = [System.IO.File]::ReadAllText($crossBranch.PreflightArgsCapture)
        Assert-True (
            $crossBranchPreflight -match '--branch\s+disposable/update-target(?:\s|\t)'
        ) 'cross-branch handoff binds the read-only preflight to the requested target branch'
    }
    Assert-ManagedPhaseOrder $crossBranch @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
        '^-B -m hermes_cli\.desktop_update_activation commit$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'cross-branch transaction'
    if (Test-Path -LiteralPath $crossBranch.Result) {
        $crossBranchResult = [System.IO.File]::ReadAllText($crossBranch.Result) | ConvertFrom-Json
        Assert-Equal $crossBranchTarget ([string]$crossBranchResult.branch) 'cross-branch result reports the explicit target branch'
        Assert-Equal $crossBranchTarget ([string]$crossBranchResult.receipt.branch) 'cross-branch receipt binds the explicit target branch'
    }

    Invoke-ActivationJournalRecoveryContracts $fakeHermes

    $targetMismatch = New-TestInstall 'preflight-target-mismatch' $fakeHermes
    $targetMismatchLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $targetMismatch $targetMismatchLeaseId
    $code = Invoke-TestHandoff $targetMismatch (New-PreflightJson $targetMismatch $true $true) 0 '' $targetMismatchLeaseId 'target-mismatch'
    Assert-Equal 9 $code 'manifest target differing from the stable preflight target fails closed'
    Assert-ManagedPhaseOrder $targetMismatch @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'preflight target mismatch'
    Assert-RolledBackActivationArtifacts $targetMismatch 'preflight target mismatch'

    $targetRemoteMismatch = New-TestInstall 'preflight-target-remote-mismatch' $fakeHermes
    $targetRemoteMismatchLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $targetRemoteMismatch $targetRemoteMismatchLeaseId
    $code = Invoke-TestHandoff $targetRemoteMismatch (New-PreflightJson $targetRemoteMismatch $true $true) 0 '' $targetRemoteMismatchLeaseId 'target-remote-mismatch'
    Assert-Equal 9 $code 'manifest remote differing from the stable preflight target fails closed'
    Assert-ManagedPhaseOrder $targetRemoteMismatch @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'preflight target remote mismatch'
    Assert-RolledBackActivationArtifacts $targetRemoteMismatch 'preflight target remote mismatch'

    $activationFailure = New-TestInstall 'activation-failure-rollback' $fakeHermes
    $activationFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $activationFailure $activationFailureLeaseId
    $code = Invoke-TestHandoff $activationFailure (New-PreflightJson $activationFailure $true $true) 0 '' $activationFailureLeaseId 'activation-fail'
    Assert-Equal 9 $code 'candidate activation failure is terminal after restoring the previous installation'
    $activationFailurePhases = @([System.IO.File]::ReadAllLines($activationFailure.PythonArgsCapture) | Where-Object { $_ })
    $activationFailureOrder = @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    )
    Assert-Equal $activationFailureOrder.Count $activationFailurePhases.Count 'activation failure executes only preflight, stage, activate, rollback, and recovery'
    for ($phase = 0; $phase -lt $activationFailureOrder.Count; $phase++) {
        Assert-True ($activationFailurePhases[$phase] -match $activationFailureOrder[$phase]) "activation failure lifecycle phase $phase has the required exact order"
    }
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $activationFailure.Home '.hermes-update-activation.json'))) 'activation failure retires its manifest'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $activationFailure.Home '.hermes-update-activation-state.json'))) 'activation failure retires partial activation state'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $activationFailure.Root '.hermes-runtime') -Filter 'venv-candidate-*' -Directory -ErrorAction SilentlyContinue).Count) 'activation failure removes the staged candidate generation'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $activationFailure.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'activation failure removes the provisioned Python generation'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $activationFailure.Home '.hermes-update-receipt.json'))) 'activation failure never publishes a new receipt'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $activationFailure.Home '.hermes-update-desktop-health.json'))) 'activation failure never publishes a Desktop health proof'
    Start-Sleep -Milliseconds 7500
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $activationFailure.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'activation failure has no delayed temporary writer 7.5 seconds after return'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $activationFailure.Root '.hermes-runtime') -Filter 'venv-candidate-*' -Directory -ErrorAction SilentlyContinue).Count) 'activation failure does not recreate its staged candidate 7.5 seconds after return'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath (Join-Path $activationFailure.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'activation failure does not recreate its provisioned generation 7.5 seconds after return'

    $buildFailure = New-TestInstall 'build-failure-rollback' $fakeHermes
    $buildFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $buildFailure $buildFailureLeaseId
    $code = Invoke-TestHandoff $buildFailure (New-PreflightJson $buildFailure $true $true) 0 '' $buildFailureLeaseId 'build-fail'
    Assert-Equal 6 $code 'Desktop build failure is terminal after restoring the previous installation'
    Assert-ManagedPhaseOrder $buildFailure @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'build failure'
    Assert-RolledBackActivationArtifacts $buildFailure 'build failure'
    if (Test-Path -LiteralPath $buildFailure.Result) {
        $buildFailureResult = [System.IO.File]::ReadAllText($buildFailure.Result) | ConvertFrom-Json
        Assert-Equal 'failed' ([string]$buildFailureResult.state) 'build failure publishes no false success'
        Assert-True ([string]$buildFailureResult.message -match 'previous installation was restored') 'build failure result truthfully reports rollback'
    }

    $noncanonicalStamp = New-TestInstall 'noncanonical-build-stamp' $fakeHermes
    $noncanonicalStampLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $noncanonicalStamp $noncanonicalStampLeaseId
    $code = Invoke-TestHandoff $noncanonicalStamp (New-PreflightJson $noncanonicalStamp $true $true) 0 '' $noncanonicalStampLeaseId 'build-stamp-offset'
    Assert-Equal 6 $code 'build exit zero with a noncanonical offset stamp remains a failed build proof'
    Assert-ManagedPhaseOrder $noncanonicalStamp @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'noncanonical Desktop build stamp'
    Assert-True (Test-Path -LiteralPath $noncanonicalStamp.BuildShaCapture) 'noncanonical stamp fixture proves the Desktop build itself exited successfully'
    Assert-RolledBackActivationArtifacts $noncanonicalStamp 'noncanonical Desktop build stamp'

    $receiptFailure = New-TestInstall 'receipt-failure-rollback' $fakeHermes
    $receiptFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $receiptFailure $receiptFailureLeaseId
    $code = Invoke-TestHandoff $receiptFailure (New-PreflightJson $receiptFailure $true $true) 0 '' $receiptFailureLeaseId 'receipt-fail'
    Assert-Equal 9 $code 'receipt publication failure is terminal after restoring the previous installation'
    Assert-ManagedPhaseOrder $receiptFailure @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
        '^-B -m hermes_cli\.desktop_update_activation rollback$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway '
    ) 'receipt failure'
    Assert-RolledBackActivationArtifacts $receiptFailure 'receipt failure'

    $recoveryFailure = New-TestInstall 'recovery-failure-rollback' $fakeHermes
    $recoveryFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $recoveryFailure $recoveryFailureLeaseId
    $code = Invoke-TestHandoff $recoveryFailure (New-PreflightJson $recoveryFailure $true $true) 0 '' $recoveryFailureLeaseId 'resume-fail'
    Assert-Equal 13 $code 'gateway recovery failure is terminal after restoring the previous installation'
    Assert-ManagedPhaseOrder $recoveryFailure @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$'
    ) 'recovery failure'
    Assert-RolledBackActivationArtifacts $recoveryFailure 'recovery failure'

    $relaunchFailure = New-TestInstall 'relaunch-failure-rollback' $fakeHermes
    $relaunchFailureDesktop = Join-Path $relaunchFailure.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $relaunchFailureDesktop
    $relaunchFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $relaunchFailure $relaunchFailureLeaseId
    $code = Invoke-TestHandoff $relaunchFailure (New-PreflightJson $relaunchFailure $true $true) 0 '' $relaunchFailureLeaseId 'relaunch-fail' $relaunchFailureDesktop
    Assert-Equal 12 $code 'exact relaunched Desktop exit before readiness restores the previous installation'
    Assert-ManagedPhaseOrder $relaunchFailure @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway ',
        '^-B -m hermes_cli\.desktop_update_activation rollback$'
    ) 'relaunch failure'
    Assert-RolledBackActivationArtifacts $relaunchFailure 'relaunch failure'

    $commitFailure = New-TestInstall 'commit-failure-retained' $fakeHermes
    $commitFailureDesktop = Join-Path $commitFailure.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $commitFailureDesktop
    $commitFailureLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $commitFailure $commitFailureLeaseId
    $code = Invoke-TestHandoff $commitFailure (New-PreflightJson $commitFailure $true $true) 0 '' $commitFailureLeaseId 'commit-fail' $commitFailureDesktop
    Assert-Equal 16 $code 'commit failure with a ready Desktop retains rollback state and withholds success'
    Assert-ManagedPhaseOrder $commitFailure @(
        '^-B -m hermes_cli\.main update --preflight ',
        '^-B -m hermes_cli\.main update --yes ',
        '^-B -m hermes_cli\.desktop_update_activation activate$',
        '^-B -m hermes_cli\.main desktop ',
        '^-B -m hermes_cli\.desktop_update_activation publish-receipt$',
        '^-B -m hermes_cli\.main update --resume-deferred-gateway ',
        '^-B -m hermes_cli\.desktop_update_activation commit$'
    ) 'commit failure'
    Assert-True (Test-Path -LiteralPath (Join-Path $commitFailure.Home '.hermes-update-activation.json')) 'commit failure retains its exact activation manifest for controlled recovery'
    Assert-True (Test-Path -LiteralPath (Join-Path $commitFailure.Home '.hermes-update-activation-state.json')) 'commit failure retains its exact activation state for controlled recovery'
    Assert-True (Test-Path -LiteralPath (Join-Path $commitFailure.Home '.hermes-update-receipt.json')) 'commit failure retains the verified active receipt'
    Assert-True (Test-Path -LiteralPath (Join-Path $commitFailure.Home '.hermes-update-desktop-health.json')) 'commit failure retains the exact Desktop health proof with rollback state'
    Assert-Equal 1 (@(Get-ChildItem -LiteralPath (Join-Path $commitFailure.Root '.hermes-runtime\python') -Filter 'generation-*' -Directory -ErrorAction SilentlyContinue).Count) 'commit failure retains the provisioned Python generation until controlled recovery'
    Assert-True (-not (Test-Path -LiteralPath $commitFailure.UpdateMarker)) 'commit failure releases the update marker'
    Assert-True (-not (Test-Path -LiteralPath $commitFailure.Lease)) 'commit failure releases the bridge lease'
    $commitManifestRaw = [System.IO.File]::ReadAllText((Join-Path $commitFailure.Home '.hermes-update-activation.json'))
    $commitStateRaw = [System.IO.File]::ReadAllText((Join-Path $commitFailure.Home '.hermes-update-activation-state.json'))

    Start-Sleep -Milliseconds 7500
    foreach ($rolledBackFailure in @($targetMismatch, $targetRemoteMismatch, $buildFailure, $noncanonicalStamp, $receiptFailure, $recoveryFailure, $relaunchFailure)) {
        Assert-RolledBackActivationArtifacts $rolledBackFailure 'rolled-back fault after 7.5 seconds'
        Assert-Equal 0 (@(Get-ChildItem -LiteralPath $rolledBackFailure.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'rolled-back fault has no delayed temporary writer 7.5 seconds after return'
    }
    Assert-Equal $commitManifestRaw ([System.IO.File]::ReadAllText((Join-Path $commitFailure.Home '.hermes-update-activation.json'))) 'commit failure manifest remains byte-stable 7.5 seconds after return'
    Assert-Equal $commitStateRaw ([System.IO.File]::ReadAllText((Join-Path $commitFailure.Home '.hermes-update-activation-state.json'))) 'commit failure state remains byte-stable 7.5 seconds after return'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $commitFailure.Home -Filter '*.tmp*' -File -Recurse -ErrorAction SilentlyContinue).Count) 'commit failure has no delayed temporary writer 7.5 seconds after return'

    $trampoline = New-TestInstall 'resume-trampoline' $fakeHermes
    $trampolineBaseDir = Join-Path $trampoline.Root '.hermes-runtime\python\test-generation'
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
                $baseDir = Join-Path $Install.Root '.hermes-runtime\python\test-generation'
                Write-TestPyvenvConfig $Install @("home $baseDir", 'implementation = CPython')
            }
        },
        [pscustomobject]@{
            Tag = 'duplicate-home'
            Label = 'duplicate pyvenv home key'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root '.hermes-runtime\python\test-generation'
                Write-TestPyvenvConfig $Install @("home = $baseDir", "HOME = $baseDir")
            }
        },
        [pscustomobject]@{
            Tag = 'missing-derived-python'
            Label = 'missing home-derived python.exe'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root '.hermes-runtime\python\test-generation'
                Remove-Item -LiteralPath (Join-Path $baseDir 'python.exe') -Force
                Write-TestPyvenvConfig $Install @("home = $baseDir")
            }
        },
        [pscustomobject]@{
            Tag = 'non-file-derived-python'
            Label = 'non-file home-derived python.exe'
            Configure = {
                param($Install)
                $baseDir = Join-Path $Install.Root '.hermes-runtime\python\test-generation'
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
                $baseDir = Join-Path $Install.Root '.hermes-runtime\python\test-generation'
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

    $archive = New-TestInstall 'archive-transaction-refusal' $fakeHermes
    $archiveLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $archive $archiveLeaseId
    $archiveDesktop = Join-Path $archive.Home 'fake-desktop.exe'
    Copy-Item -LiteralPath $fakeDesktopTemplate -Destination $archiveDesktop
    $code = Invoke-TestHandoff $archive (New-PreflightJson $archive $true $true) 0 '' $archiveLeaseId 'archive' $archiveDesktop
    Assert-Equal 9 $code 'archive-mode staging without a transactional Git manifest fails closed'
    Assert-True (-not (Test-Path -LiteralPath $archive.BuildShaCapture)) 'archive-mode staging never rebuilds an unauthenticated target'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $archive.Home '.hermes-update-activation.json'))) 'archive-mode refusal rolls back its staged activation claim'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $archive.Home '.hermes-update-activation-state.json'))) 'archive-mode refusal leaves no activation state'
    if (Test-Path -LiteralPath $archive.Result) {
        $archiveResult = [System.IO.File]::ReadAllText($archive.Result) | ConvertFrom-Json
        Assert-Equal 'failed' ([string]$archiveResult.state) 'archive-mode refusal publishes a terminal failure'
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

    $rollbackReceipt = New-TestInstall 'receipt-clock-rollback' $fakeHermes
    $rollbackLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $rollbackReceipt $rollbackLeaseId
    $code = Invoke-TestHandoff $rollbackReceipt (New-PreflightJson $rollbackReceipt $true $true) 0 '' $rollbackLeaseId 'receipt-clock-rollback-resume-fail'
    Assert-Equal 13 $code 'a capability-correlated receipt tolerates a bounded backward wall-clock step before recovery fails'
    if (Test-Path -LiteralPath $rollbackReceipt.Result) {
        $rollbackResult = [System.IO.File]::ReadAllText($rollbackReceipt.Result) | ConvertFrom-Json
        Assert-True ($null -eq $rollbackResult.receipt) 'recovery failure restores the prior receipt before publishing failure'
        Assert-True ([int64]$rollbackResult.finished_at -ge [int64]$rollbackResult.relaunch.requested_at) 'recovery failure retains monotonic result ordering'
        Assert-Equal 'failed' ([string]$rollbackResult.state) 'recovery rollback publishes a strict terminal failure'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $rollbackReceipt.Home '.hermes-update-activation.json'))) 'recovery rollback retires the activation manifest'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $rollbackReceipt.Home '.hermes-update-activation-state.json'))) 'recovery rollback retires activation state'
    } else {
        Assert-True $false 'recovery rollback publishes a strict terminal result'
    }

    $prePlanFailure = New-TestInstall 'pre-plan-failure' $fakeHermes
    $prePlanLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
    Write-TestLease $prePlanFailure $prePlanLeaseId
    $code = Invoke-TestHandoff $prePlanFailure (New-PreflightJson $prePlanFailure $true $true) 0 '' $prePlanLeaseId 'pre-plan-fail'
    Assert-Equal 13 $code 'pre-plan updater refusal remains a fail-closed recovery failure'
    Assert-True (-not (Test-Path -LiteralPath $prePlanFailure.Sentinel)) 'pre-plan updater refusal performs no mutation'
    Assert-True (-not (Test-Path -LiteralPath $prePlanFailure.ResumeCapture)) 'pre-plan updater refusal starts no recovery child'
    Assert-True (-not (Test-Path -LiteralPath $prePlanFailure.UpdateMarker)) 'pre-plan updater refusal releases the exact update marker'
    Assert-True (-not (Test-Path -LiteralPath $prePlanFailure.Lease)) 'pre-plan updater refusal releases the returned bridge lease'
    Assert-Equal 0 (@(Get-ChildItem -LiteralPath $prePlanFailure.Home -Filter '.hermes-gateway-resume-*' -File -ErrorAction SilentlyContinue).Count) 'pre-plan updater refusal leaves no gateway plan artifact'
    Assert-True (Test-Path -LiteralPath $prePlanFailure.Result) 'pre-plan updater refusal publishes a terminal result'
    if (Test-Path -LiteralPath $prePlanFailure.Result) {
        $prePlanResult = [System.IO.File]::ReadAllText($prePlanFailure.Result) | ConvertFrom-Json
        Assert-Equal 13 ([int]$prePlanResult.exit_code) 'pre-plan terminal result remains fail-closed'
        Assert-True ([string]$prePlanResult.message -match 'Hermes update failed \(exit 2\)') 'pre-plan terminal result retains the original update failure detail'
        Assert-True ([string]$prePlanResult.message -match 'could not verify whether gateway recovery was required or completed') 'pre-plan terminal result accurately reports unverified recovery state'
        Assert-True ([string]$prePlanResult.message -notmatch 'without restoring') 'pre-plan terminal result does not assert that an unstopped fleet was not restored'
    }

    foreach ($ambiguousMode in @('invalid-plan-fail', 'ambiguous-plan-fail')) {
        $ambiguousPlan = New-TestInstall $ambiguousMode $fakeHermes
        $ambiguousPlanLeaseId = 'lease-' + [Guid]::NewGuid().ToString('N')
        Write-TestLease $ambiguousPlan $ambiguousPlanLeaseId
        $code = Invoke-TestHandoff $ambiguousPlan (New-PreflightJson $ambiguousPlan $true $true) 0 '' $ambiguousPlanLeaseId $ambiguousMode
        Assert-Equal 13 $code "$ambiguousMode remains a failed fleet recovery"
        Assert-True (-not (Test-Path -LiteralPath $ambiguousPlan.Sentinel)) "$ambiguousMode performs no mutation"
        Assert-True (-not (Test-Path -LiteralPath $ambiguousPlan.ResumeCapture)) "$ambiguousMode starts no unproved recovery child"
        Assert-True (-not (Test-Path -LiteralPath $ambiguousPlan.UpdateMarker)) "$ambiguousMode releases the exact update marker"
        Assert-True (-not (Test-Path -LiteralPath $ambiguousPlan.Lease)) "$ambiguousMode releases the returned bridge lease"
        if (Test-Path -LiteralPath $ambiguousPlan.Result) {
            $ambiguousResult = [System.IO.File]::ReadAllText($ambiguousPlan.Result) | ConvertFrom-Json
            Assert-True ([string]$ambiguousResult.message -match 'could not verify whether gateway recovery was required or completed') "$ambiguousMode reports the unproved recovery state"
        }
        $invalidVenvHomes += $ambiguousPlan.Home
    }

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
    }
} finally {
    foreach ($identity in $containmentWriterIdentities) {
        Stop-TestWriterExact $identity (Join-Path $suiteRoot 'cleanup-writer.release')
    }
    $cleanupPaths = @($focusedSuccess.Home, $focusedOffset.Home, $containmentNoncooperative.Home, $bufferedAdoption.Home, $containmentFastSuccess.Home, $containmentSuccess.Home, $containmentBoundaryFailure.Home, $containmentDrain.Home, $containmentAssignFailure.Home, $logContention.Home, $noCapability.Home, $invalid.Home, $blocked.Home, $probeFailure.Home, $legacy.Home, $partial.Home, $selfPreclaim.Home, $impossibleSelfPreclaim.Home, $missingLease.Home, $unreadableMarker.Home, $foreignMarker.Home, $oldLiveMarker.Home, $deadMarker.Home, $leased.Home, $crossBranch.Home, $targetMismatch.Home, $targetRemoteMismatch.Home, $activationFailure.Home, $buildFailure.Home, $noncanonicalStamp.Home, $receiptFailure.Home, $recoveryFailure.Home, $relaunchFailure.Home, $commitFailure.Home, $trampoline.Home, $archive.Home, $immediate.Home, $survivor.Home, $unwritableResult.Home, $silent.Home, $stderrHeavy.Home, $rollbackReceipt.Home, $prePlanFailure.Home, $foreignRace.Home, $foreign.Home, $suiteRoot) + $invalidVenvHomes
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
