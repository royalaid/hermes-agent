# desktop-update.ps1 -- repo-owned Windows Desktop update hand-off.
#
# WHY THIS EXISTS (the frozen-binary problem): the Desktop's Update button
# used to hand off exclusively to the staged Tauri binary
# (%HERMES_HOME%\hermes-setup.exe). That binary has no self-update path --
# copy_self_to_hermes_home deliberately no-ops during --update -- so every
# updater-side fix (cache refresh #67369, marker self-adopt #74782, straggler
# handling) only reaches users when a new installer is built, signed, and
# published. In practice binaries go months stale and users hit long-fixed
# bugs on every update (the 2026-08-09 incident chain).
#
# This script lives in the repo checkout, so EVERY `hermes update` refreshes
# the very code that drives the next update. The Desktop spawns it through a
# `cmd start` wrapper (see wrapHandoffForDetachedConsole in
# apps/desktop/electron/updater-process.ts -- a bare detached+hidden
# powershell dies before -File runs) and exits; only PowerShell itself -- an
# OS component -- is "frozen".
#
# CONTRACT (keep in sync with apps/desktop/electron/main.ts):
#   cmd /d /s /c start "" /min powershell -NoProfile -ExecutionPolicy Bypass
#     -File scripts\desktop-update.ps1
#     -InstallRoot <path>   repo checkout (HERMES_HOME\hermes-agent)
#     -Branch <ref>         branch to update against
#     -DesktopPid <pid>     the Electron main process to wait out
#     [-RelaunchExe <path>] Hermes.exe/Electron executable to start when done
#     [-RelaunchAppPath <path>] optional Electron development app entry
#     [-BridgeLeaseId <id>] unguessable bridge-quiesce handoff capability
#     [-NoUi]               headless (tests); default shows a progress window
#     [-NoMarkerCleanup]    leave .hermes-update-in-progress in place (tests)
#
# SAFETY POSTURE: both preflight gates FAIL CLOSED. A Desktop that never
# exits, or a venv shim that never unlocks, aborts the hand-off without
# mutating the install -- a skipped update is recoverable, a half-updated
# venv is not. Every exit path (success, abort, crash) writes
# .hermes-update-result.json for the relaunched Desktop to surface, and
# relaunches the Desktop so the user is never left stranded.
#
# Marker: we claim HERMES_HOME\.hermes-update-in-progress with OUR pid as
# step 0 (the wrapper cmd.exe pid the Desktop saw is useless -- it exits
# immediately). hermes_cli/update_lock.py's ancestry rule lets our
# `hermes update` child adopt the claim; electron/update-marker.ts parks a
# relaunched Desktop on it. Cleanup only removes the marker while WE still
# own it (a handoff partner that rewrote it keeps its claim).

param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [string]$Branch = "main",
    [int]$DesktopPid = 0,
    [string]$RelaunchExe = "",
    [string]$RelaunchAppPath = "",
    [string]$BridgeLeaseId = "",
    [switch]$NoUi,
    [switch]$NoMarkerCleanup,
    [switch]$TestMode,
    [ValidateRange(0, 5000)][int]$TestDeferredGatewayPreAssignHoldMilliseconds = 0,
    [switch]$TestDeferredGatewayAssignFailure,
    [switch]$TestDeferredGatewayInitialKillFailure,
    [switch]$TestDeferredGatewayNativeBoundaryFailure,
    [ValidateRange(0, 5000)][int]$TestDeferredGatewayTargetRetentionHoldMilliseconds = 0,
    [ValidateRange(0, 5000)][int]$TestDeferredGatewayPostTargetGateHoldMilliseconds = 0,
    [ValidateRange(0, 5000)][int]$TestDeferredGatewayWrapperExitHoldMilliseconds = 0,
    [ValidateSet('none', 'malformed', 'duplicate', 'wrong-path', 'wrong-filetime', 'stall')]
    [string]$TestDeferredGatewayTargetStartFault = 'none'
)

$ErrorActionPreference = "Continue"
$desktopUpdateJsonHelper = Join-Path $PSScriptRoot 'desktop-update-json.ps1'
try {
    if (-not (Test-Path -LiteralPath $desktopUpdateJsonHelper -PathType Leaf)) {
        throw 'shared Desktop update JSON helper is missing'
    }
    . $desktopUpdateJsonHelper
    $desktopStampParser = Get-Command ConvertFrom-DesktopStampJson -CommandType Function -ErrorAction Stop
    if (-not $desktopStampParser) {
        throw 'shared Desktop update JSON parser is unavailable'
    }
} catch {
    [Console]::Error.WriteLine('Desktop update JSON validation could not initialize; update aborted before mutation.')
    exit 3
}
if (($TestDeferredGatewayPreAssignHoldMilliseconds -gt 0 -or
    $TestDeferredGatewayAssignFailure -or
    $TestDeferredGatewayInitialKillFailure -or
    $TestDeferredGatewayNativeBoundaryFailure -or
    $TestDeferredGatewayTargetRetentionHoldMilliseconds -gt 0 -or
    $TestDeferredGatewayPostTargetGateHoldMilliseconds -gt 0 -or
    $TestDeferredGatewayWrapperExitHoldMilliseconds -gt 0 -or
    $TestDeferredGatewayTargetStartFault -ne 'none') -and (-not $TestMode -or -not $NoUi)) {
    throw 'deferred gateway containment fault controls require -TestMode and -NoUi'
}
# `-TestMode -NoUi` is the explicit repo test-harness boundary. Production
# headless launches also use `-NoUi`, so that switch alone must never enable
# inherited fault controls from an older Desktop, test runner, or parent.
if (-not $TestMode) {
    Get-ChildItem Env: | Where-Object {
        $_.Name -match '^(?i:HERMES_(?:DESKTOP_UPDATE_TEST$|TEST_|SELFTEST_|FORCE_RELEASE_(?:FORCE|TEST)_|TERMINATE_|DEFERRED_GATEWAY_STARTUP_GATE$|INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS$|INTERNAL_TEST_|UPDATE_HANDOFF_))'
    } | ForEach-Object {
        Remove-Item -LiteralPath ("Env:{0}" -f $_.Name) -ErrorAction SilentlyContinue
    }
}
# Foreground helpers: the script is spawned via `cmd start /min`, so its
# WinForms window comes up backgrounded unless we explicitly claim focus --
# and after the update we must hand focus TO the relaunched Desktop (a
# WMI-spawned process starts unfocused). AllowSetForegroundWindow lets us
# pass our foreground right on to the new Hermes.exe pid.
try {
    Add-Type -Namespace HermesHandoff -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool AllowSetForegroundWindow(int dwProcessId);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@ -ErrorAction Stop
    $script:Win32 = $true
} catch { $script:Win32 = $false }
try {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace HermesHandoff {
    public static class UpdaterJob {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits {
            public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits {
            public BasicLimits BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicAccounting {
            public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool QueryInformationJobObject(IntPtr job, int infoClass, ref BasicAccounting info, uint length, IntPtr returnLength);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetProcessId(IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(IntPtr file, StringBuilder path, uint size, uint flags);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateHardLink(string newName, string existingName, IntPtr securityAttributes);

        public static IntPtr CreateKillOnClose() {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw new Win32Exception();
            var limits = new ExtendedLimits();
            limits.BasicLimitInformation.LimitFlags = 0x00002000;
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits)))) {
                int error = Marshal.GetLastWin32Error();
                CloseHandle(job);
                throw new Win32Exception(error);
            }
            return job;
        }
        public static void DisarmKillOnClose(IntPtr job) {
            if (job == IntPtr.Zero) return;
            var limits = new ExtendedLimits();
            limits.BasicLimitInformation.LimitFlags = 0;
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
                throw new Win32Exception();
        }
        public static void Assign(IntPtr job, IntPtr process) {
            if (!AssignProcessToJobObject(job, process)) throw new Win32Exception();
        }
        public static bool ContainsPid(IntPtr job, int pid) {
            IntPtr process = OpenProcess(0x1000, false, (uint)pid);
            if (process == IntPtr.Zero) throw new Win32Exception();
            try {
                bool contained;
                if (!IsProcessInJob(process, job, out contained)) throw new Win32Exception();
                return contained;
            } finally { CloseHandle(process); }
        }
        public static IntPtr OpenRuntimeProcess(int pid) {
            IntPtr process = OpenProcess(0x00101000, false, (uint)pid);
            if (process == IntPtr.Zero) throw new Win32Exception();
            return process;
        }
        public static bool RuntimeProcessIsLive(IntPtr process) {
            uint result = WaitForSingleObject(process, 0);
            if (result == 0x00000102) return true;
            if (result == 0x00000000) return false;
            throw new Win32Exception();
        }
        public static int RuntimeProcessId(IntPtr process) {
            uint pid = GetProcessId(process);
            if (pid == 0 || pid > Int32.MaxValue) throw new Win32Exception();
            return (int)pid;
        }
        public static bool ContainsProcessHandle(IntPtr job, IntPtr process) {
            bool contained;
            if (!IsProcessInJob(process, job, out contained)) throw new Win32Exception();
            return contained;
        }
        public static string RuntimeCreationFileTime(IntPtr process) {
            long creation, exit, kernel, user;
            if (!GetProcessTimes(process, out creation, out exit, out kernel, out user))
                throw new Win32Exception();
            return ((ulong)creation).ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
        public static string RuntimeExecutablePath(IntPtr process) {
            uint size = 32768;
            var path = new StringBuilder((int)size);
            if (!QueryFullProcessImageName(process, 0, path, ref size))
                throw new Win32Exception();
            return path.ToString();
        }
        // uv publishes the managed CPython home as a version-alias directory
        // symlink (cpython-3.11-... -> cpython-3.11.15-...), so a path taken
        // from pyvenv.cfg and the path Windows reports for the live process
        // image name the same file through different links. Resolve every
        // reparse point on both sides before they are compared so executable
        // identity is decided by the file itself, not by which link named it.
        public static string CanonicalFilePath(string path) {
            using (var stream = new System.IO.FileStream(
                path,
                System.IO.FileMode.Open,
                System.IO.FileAccess.Read,
                System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete)) {
                var resolved = new StringBuilder(32768);
                uint written = GetFinalPathNameByHandle(
                    stream.SafeFileHandle.DangerousGetHandle(), resolved, (uint)resolved.Capacity, 0);
                if (written == 0 || written >= (uint)resolved.Capacity) throw new Win32Exception();
                string full = resolved.ToString();
                if (full.StartsWith("\\\\?\\UNC\\", StringComparison.Ordinal))
                    full = "\\\\" + full.Substring(8);
                else if (full.StartsWith("\\\\?\\", StringComparison.Ordinal))
                    full = full.Substring(4);
                return System.IO.Path.GetFullPath(full);
            }
        }
        public static void CloseRuntimeProcess(IntPtr process) {
            if (process != IntPtr.Zero) CloseHandle(process);
        }
        public static void Terminate(IntPtr job) {
            if (job != IntPtr.Zero && !TerminateJobObject(job, 8)) throw new Win32Exception();
        }
        public static uint ActiveProcesses(IntPtr job) {
            var info = new BasicAccounting();
            if (!QueryInformationJobObject(job, 1, ref info, (uint)Marshal.SizeOf(typeof(BasicAccounting)), IntPtr.Zero))
                throw new Win32Exception();
            return info.ActiveProcesses;
        }
        public static void Close(IntPtr job) {
            if (job != IntPtr.Zero) CloseHandle(job);
        }
        public static void HardLink(string newName, string existingName) {
            if (!CreateHardLink(newName, existingName, IntPtr.Zero)) throw new Win32Exception();
        }
        public static string QuoteArgument(string argument) {
            if (argument == null) return "\"\"";
            if (argument.Length > 0 && argument.IndexOfAny(new [] { ' ', '\t', '\n', '\v', '"' }) < 0)
                return argument;
            var quoted = new StringBuilder("\"");
            int slashes = 0;
            foreach (char value in argument) {
                if (value == '\\') { slashes++; continue; }
                if (value == '"') {
                    quoted.Append('\\', slashes * 2 + 1).Append('"');
                    slashes = 0;
                    continue;
                }
                quoted.Append('\\', slashes).Append(value);
                slashes = 0;
            }
            quoted.Append('\\', slashes * 2).Append('"');
            return quoted.ToString();
        }
    }
}
'@ -Language CSharp -ErrorAction Stop
    $script:JobContainmentAvailable = $true
} catch {
    $script:JobContainmentAvailable = $false
}
# Render UTF-8 glyphs (checkmarks, arrows) correctly in our own console echo
# too; the legacy conhost default OEM codepage shows them as mojibake.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}
try {
    $InstallRoot = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).ProviderPath
    ).TrimEnd([char[]]@('\', '/'))
    $HermesHome = Split-Path -Parent $InstallRoot
    $hermesHomeParent = Split-Path -Parent $HermesHome
    if ([string]::Equals(
        (Split-Path -Leaf $hermesHomeParent),
        'profiles',
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'the install root resolves beneath a profile-scoped Hermes home'
    }
} catch {
    [Console]::Error.WriteLine("Update aborted: the canonical install-global Hermes root could not be verified. Nothing was changed.")
    exit 8
}
$MarkerPath = Join-Path $HermesHome ".hermes-update-in-progress"
$BridgeLeasePath = Join-Path $HermesHome ".hermes-venv-quiesce"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "desktop-update-handoff.log"
$ResultPath = Join-Path $HermesHome ".hermes-update-result.json"
$UpdateReceiptPath = Join-Path $HermesHome ".hermes-update-receipt.json"
$ActivationManifestPath = Join-Path $HermesHome ".hermes-update-activation.json"
$ActivationStatePath = Join-Path $HermesHome ".hermes-update-activation-state.json"
$DesktopHealthPath = Join-Path $HermesHome ".hermes-update-desktop-health.json"
$script:Ui = $null
$script:BridgeLeaseOwned = $false
$script:BridgeLeaseRequired = $false
$script:BridgeLeaseRefreshFailed = $false
$script:BridgeLeaseNextRefreshAt = [DateTime]::MinValue
$script:BridgeLeaseTransferredPid = 0
$script:HandoffStartedAt = [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$script:VerifiedUpdateReceipt = $null
$script:ReceiptVerifiedAt = 0L
$script:PreUpdateGitHead = $null
$script:PreUpdateGitBranch = $null
$script:PreflightTargetGitHead = $null
$script:PreflightTargetGitRemote = $null
$script:PreflightTargetGitRef = $null
$script:ActivationBasePython = $null
$script:ActivationStarted = $false
$script:ActivationReceiptPublished = $false
$script:ActivationCommitted = $false
$script:UpdateMarkerOwned = $false
$script:UpdateMarkerStartedAt = 0
$script:RelaunchRequired = -not [string]::IsNullOrWhiteSpace($RelaunchExe)
$script:RelaunchStarted = $false
$script:RelaunchPid = 0
$script:RelaunchProcessStartedAt = 0L
$script:RelaunchRequestedAt = 0L
$script:RelaunchRequestStartedAt = 0L
$script:CanonicalRelaunchExe = $null
$script:CanonicalRelaunchAppPath = $null
$script:AttemptId = "attempt-$([Guid]::NewGuid().ToString('N'))"
$script:InvocationId = "invocation-$([Guid]::NewGuid().ToString('N'))"
$handoffFallbackHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $handoffFallbackHash = [System.BitConverter]::ToString(
        $handoffFallbackHasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($script:AttemptId))
    ).Replace('-', '').ToLowerInvariant().Substring(0, 16)
} finally {
    $handoffFallbackHasher.Dispose()
}
$script:HandoffFallbackPath = Join-Path $LogDir ("desktop-update-handoff-fallback-{0}.log" -f $handoffFallbackHash)
$script:HandoffPrimaryLogUnavailable = $false
$script:HandoffFallbackHeaderWritten = $false
$script:HandoffFallbackSuppressionWritten = $false
$script:HandoffFallbackPersistenceWarningWritten = $false
$script:HandoffFallbackMaxBytes = 131072
$script:AckPath = Join-Path $HermesHome (".hermes-update-ack-{0}.json" -f $script:AttemptId)
$script:RelaunchRequestPath = Join-Path $HermesHome (".hermes-update-relaunch-request-{0}.json" -f $script:AttemptId)
$script:RelaunchRequestRaw = $null
$script:RelaunchRequestPublished = $false
$script:RelaunchSuppressed = $false
$script:GatewayPlanPath = Join-Path $HermesHome (".hermes-gateway-resume-{0}.json" -f $script:InvocationId)
$script:GatewayPlanCompletedPath = Join-Path $HermesHome (".hermes-gateway-resume-{0}.completed" -f $script:InvocationId)
$script:GatewayPlanPreparedPath = Join-Path $HermesHome (".hermes-gateway-resume-{0}.prepared" -f $script:InvocationId)
$script:GatewayRuntimeManifestPath = Join-Path $HermesHome (".hermes-gateway-resume-{0}.prepared-runtime.json" -f $script:InvocationId)
$script:BridgeLeaseMaxLifetimeSeconds = 1200
$script:BridgeLeaseHandoffGraceSeconds = 90
$script:GatewayPlanMaxLifetimeSeconds = 3960
$script:OuterJobDrainSeconds = 5

function Get-ManagedStageTimeoutSeconds([string]$Tag) {
    if ($env:HERMES_DESKTOP_UPDATE_TEST -eq '1') {
        $testValue = 0
        if ([int]::TryParse($env:HERMES_INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS, [ref]$testValue) -and
            $testValue -gt 0 -and $testValue -le 30) {
            return $testValue
        }
    }
    switch ($Tag) {
        'update' { return 3600 }
        'rebuild' { return 1800 }
        default { return 300 }
    }
}

function Add-HandoffLogLineWithRetry([string]$Path, [string]$Line) {
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($Line + [Environment]::NewLine)
    foreach ($delay in @(0, 25, 75, 150)) {
        if ($delay -gt 0) { Start-Sleep -Milliseconds $delay }
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            [void]$stream.Seek(0, [System.IO.SeekOrigin]::End)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
            return $true
        } catch {
            # Retry only this bounded append. Raw native/PowerShell errors are
            # not diagnostics and must never escape into the handoff surface.
        } finally {
            if ($stream) { $stream.Dispose() }
        }
    }
    return $false
}

function Add-HandoffFallbackLine([string]$Line) {
    try {
        $currentLength = if (Test-Path -LiteralPath $script:HandoffFallbackPath -PathType Leaf) {
            (Get-Item -LiteralPath $script:HandoffFallbackPath -ErrorAction Stop).Length
        } else { 0L }
        $lineBytes = (New-Object System.Text.UTF8Encoding($false)).GetByteCount($Line + [Environment]::NewLine)
        if (($currentLength + $lineBytes) -le $script:HandoffFallbackMaxBytes) {
            return Add-HandoffLogLineWithRetry $script:HandoffFallbackPath $Line
        }
        if (-not $script:HandoffFallbackSuppressionWritten) {
            $script:HandoffFallbackSuppressionWritten = $true
            [void](Add-HandoffLogLineWithRetry $script:HandoffFallbackPath (
                "{0:yyyy-MM-ddTHH:mm:ssK} further handoff diagnostics omitted after the bounded fallback limit" -f (Get-Date)
            ))
        }
        return $true
    } catch {
        return $false
    }
}

function Write-HandoffLog([string]$Message) {
    $line = "{0:yyyy-MM-ddTHH:mm:ssK} {1}" -f (Get-Date), $Message
    $persisted = $false
    if (-not $script:HandoffPrimaryLogUnavailable) {
        $persisted = Add-HandoffLogLineWithRetry $LogPath $line
        if (-not $persisted) { $script:HandoffPrimaryLogUnavailable = $true }
    }
    if ($script:HandoffPrimaryLogUnavailable) {
        if (-not $script:HandoffFallbackHeaderWritten) {
            $script:HandoffFallbackHeaderWritten = $true
            $persisted = Add-HandoffFallbackLine (
                "{0:yyyy-MM-ddTHH:mm:ssK} primary handoff log unavailable; using bounded per-attempt fallback" -f (Get-Date)
            )
        }
        if (-not (Add-HandoffFallbackLine $line)) { $persisted = $false }
    }
    if (-not $persisted -and -not $script:HandoffFallbackPersistenceWarningWritten) {
        $script:HandoffFallbackPersistenceWarningWritten = $true
        Write-Host 'Hermes update diagnostics could not be persisted; the updater result is unchanged.'
    }
    Write-Host $line
    if ($script:Ui) {
        try {
            $script:Ui.Box.AppendText($Message + "`r`n")
            [System.Windows.Forms.Application]::DoEvents()
        } catch {}
    }
}

function Get-UnixTimeSeconds {
    return [int64][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Get-CanonicalInstallRoot([string]$Path) {
    try {
        $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
        return [System.IO.Path]::GetFullPath($resolved).TrimEnd([char[]]@('\', '/'))
    } catch {
        return $null
    }
}

function Read-BoundedUtf8Text([string]$Path, [int]$MaxBytes) {
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
    )
    try {
        if ($stream.Length -le 0 -or $stream.Length -gt $MaxBytes) {
            throw 'protocol artifact size is outside its bounded contract'
        }
        $bytes = New-Object byte[] ([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($count -le 0) { throw 'protocol artifact ended before its claimed length' }
            $offset += $count
        }
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        return $strictUtf8.GetString($bytes)
    } finally {
        $stream.Dispose()
    }
}

function Resolve-ManagedVenvPythonLaunch([string]$VenvPython) {
    try {
        $launcher = (Resolve-Path -LiteralPath $VenvPython -ErrorAction Stop).ProviderPath
        $launcher = [System.IO.Path]::GetFullPath($launcher)
        $expectedLauncher = (Resolve-Path -LiteralPath (Join-Path $InstallRoot 'venv\Scripts\python.exe') -ErrorAction Stop).ProviderPath
        $expectedLauncher = [System.IO.Path]::GetFullPath($expectedLauncher)
        if (-not [string]::Equals($launcher, $expectedLauncher, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'the managed Python launcher is outside the exact install venv'
        }

        $venvRoot = Split-Path -Parent (Split-Path -Parent $launcher)
        $configPath = Join-Path $venvRoot 'pyvenv.cfg'
        $configInfo = Get-Item -LiteralPath $configPath -ErrorAction Stop
        if ($configInfo.Length -le 0 -or $configInfo.Length -gt 16384) {
            throw 'pyvenv.cfg has an invalid size'
        }
        $values = @{}
        foreach ($line in [System.IO.File]::ReadAllLines($configPath, [System.Text.Encoding]::UTF8)) {
            $trimmed = $line.Trim()
            if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
            $separator = $trimmed.IndexOf('=')
            if ($separator -le 0) { throw 'pyvenv.cfg contains a malformed entry' }
            $key = $trimmed.Substring(0, $separator).Trim().ToLowerInvariant()
            if ($key -ne 'home' -and $key -ne 'executable') { continue }
            if ($values.ContainsKey($key)) { throw "pyvenv.cfg repeats $key" }
            $value = $trimmed.Substring($separator + 1).Trim()
            if (-not $value -or -not [System.IO.Path]::IsPathRooted($value)) {
                throw "pyvenv.cfg has an invalid $key"
            }
            $values[$key] = $value
        }
        if (-not $values.ContainsKey('home')) {
            throw 'pyvenv.cfg does not identify the base interpreter'
        }
        $baseHome = (Resolve-Path -LiteralPath $values['home'] -ErrorAction Stop).ProviderPath
        $baseHome = [System.IO.Path]::GetFullPath($baseHome).TrimEnd([char[]]@('\', '/'))
        $managedPythonRoot = (Resolve-Path -LiteralPath (Join-Path $InstallRoot '.hermes-runtime\python') -ErrorAction Stop).ProviderPath
        $managedPythonRoot = [System.IO.Path]::GetFullPath($managedPythonRoot).TrimEnd([char[]]@('\', '/'))
        $managedPythonPrefix = $managedPythonRoot + [System.IO.Path]::DirectorySeparatorChar
        if (-not $baseHome.StartsWith($managedPythonPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'pyvenv.cfg base interpreter is outside the private managed runtime'
        }
        $basePythonPath = if ($values.ContainsKey('executable')) {
            $values['executable']
        } else {
            Join-Path $values['home'] 'python.exe'
        }
        $basePython = (Resolve-Path -LiteralPath $basePythonPath -ErrorAction Stop).ProviderPath
        $basePython = [System.IO.Path]::GetFullPath($basePython)
        if (-not (Test-Path -LiteralPath $basePython -PathType Leaf) -or
            -not [string]::Equals((Split-Path -Leaf $basePython), 'python.exe', [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals((Split-Path -Parent $basePython).TrimEnd([char[]]@('\', '/')), $baseHome, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'pyvenv.cfg base interpreter is not the exact python.exe under home'
        }
        return [pscustomobject]@{ Executable = $basePython; Launcher = $launcher }
    } catch {
        throw "the managed venv base interpreter could not be proven: $($_.Exception.Message)"
    }
}

function Get-ProcessClaimState([int64]$ProcessId, [int64]$ClaimedAt) {
    if ($ProcessId -le 0 -or $ProcessId -gt [int]::MaxValue -or $ClaimedAt -le 0) {
        return 'dead-or-reused'
    }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$ProcessId)
        if ($process.HasExited) { return 'dead-or-reused' }
        $started = [int64][DateTimeOffset]::new($process.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
        # A marker is written after its owner has already started. A process
        # whose creation time reaches the next whole second therefore began
        # after the claim and proves numeric PID reuse.
        if ($started -ge ($ClaimedAt + 1)) { return 'dead-or-reused' }
        return 'live'
    } catch [System.ArgumentException] {
        return 'dead-or-reused'
    } catch {
        # Access denied or an unreadable creation time is not proof of death.
        return 'unreadable'
    }
}

function Get-HandoffCasArtifacts([string]$Path) {
    $parent = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { return @() }
    return @(
        [System.IO.Directory]::EnumerateFiles($parent, "$leaf.cas-*") |
            ForEach-Object { [System.IO.Path]::GetFullPath($_) }
    )
}

function Assert-NoHandoffCasArtifacts([string]$Path) {
    if (@(Get-HandoffCasArtifacts $Path).Count -gt 0) {
        throw 'a handoff CAS recovery artifact is present; refusing to infer clear ownership'
    }
}

function Publish-HandoffFileNoGap(
    [string]$Path,
    [AllowNull()][object]$ExpectedRaw,
    [string]$NewRaw,
    [string]$Kind
) {
    Assert-NoHandoffCasArtifacts $Path
    $suffix = "$PID-$([Guid]::NewGuid().ToString('N'))"
    $shadow = "$Path.cas-shadow-$suffix"
    $previous = "$Path.cas-previous-$suffix"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($shadow, $NewRaw, $encoding)
    try {
        if ($null -eq $ExpectedRaw) {
            [HermesHandoff.UpdaterJob]::HardLink($Path, $shadow)
        } else {
            [System.IO.File]::Move($Path, $previous)
            $actualPrevious = [System.IO.File]::ReadAllText($previous)
            if (-not [string]::Equals($actualPrevious, [string]$ExpectedRaw, [StringComparison]::Ordinal)) {
                if (-not (Test-Path -LiteralPath $Path)) {
                    try { [HermesHandoff.UpdaterJob]::HardLink($Path, $previous) } catch {}
                }
                throw "$Kind ownership changed during exact-byte CAS"
            }
            try {
                [HermesHandoff.UpdaterJob]::HardLink($Path, $shadow)
            } catch {
                if (-not (Test-Path -LiteralPath $Path)) {
                    try { [HermesHandoff.UpdaterJob]::HardLink($Path, $previous) } catch {}
                }
                throw
            }
            if (-not [string]::Equals([System.IO.File]::ReadAllText($Path), $NewRaw, [StringComparison]::Ordinal)) {
                throw "$Kind changed immediately after publication"
            }
            Remove-Item -LiteralPath $previous -Force -ErrorAction Stop
        }
        Remove-Item -LiteralPath $shadow -Force -ErrorAction Stop
    } catch {
        # Recovery artifacts deliberately remain. Cross-runtime readers treat
        # them as an active/unknown claim rather than observing a clear gap.
        throw
    }
}

function Read-BridgeLeaseSnapshotFromStream([System.IO.FileStream]$Stream) {
    try {
        $Stream.Position = 0
        $length = [int]$Stream.Length
        if ($length -le 0 -or $length -gt 65536) { return $null }
        $bytes = New-Object byte[] $length
        $offset = 0
        while ($offset -lt $length) {
            $count = $Stream.Read($bytes, $offset, $length - $offset)
            if ($count -le 0) { return $null }
            $offset += $count
        }
        $raw = [System.Text.Encoding]::UTF8.GetString($bytes)
        $lease = $raw | ConvertFrom-Json -ErrorAction Stop
        $expected = @(
            'schema_version', 'lease_id', 'owner_pid', 'created_at',
            'expires_at', 'handoff_grace_until', 'install_root'
        )
        $names = @($lease.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $null }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $null }
        }
        return [pscustomobject]@{ Lease = $lease; Raw = $raw }
    } catch {
        return $null
    }
}

function Open-BridgeLeaseSnapshot {
    return [System.IO.File]::Open(
        $BridgeLeasePath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
    )
}

function New-RenewedBridgeLeaseJson(
    [object]$Lease,
    [int64]$Now
) {
    $renewed = [ordered]@{
        schema_version      = 1
        lease_id           = [string]$Lease.lease_id
        owner_pid          = [int]$PID
        # Each authenticated ownership transfer/renewal starts a fresh bounded
        # lease window. Keeping the prior value while extending expires_at
        # would violate the cross-language max-lifetime invariant.
        created_at         = $Now
        expires_at         = $Now + $script:BridgeLeaseMaxLifetimeSeconds
        handoff_grace_until = $Now + $script:BridgeLeaseHandoffGraceSeconds
        install_root       = [string]$Lease.install_root
    }
    return $renewed | ConvertTo-Json -Compress
}

function Replace-BridgeLeaseAtomically(
    [string]$ExpectedRaw,
    [object]$Lease,
    [int64]$Now
) {
    $newRaw = New-RenewedBridgeLeaseJson $Lease $Now
    Publish-HandoffFileNoGap $BridgeLeasePath $ExpectedRaw $newRaw 'bridge-quiesce lease'
}

function Test-BridgeLeaseFieldTypes([object]$Lease) {
    if (-not $Lease) { return $false }
    if (-not (Test-JsonInteger $Lease.schema_version)) { return $false }
    if ($Lease.lease_id -isnot [string] -or
        $Lease.lease_id -notmatch '^[A-Za-z0-9._-]{16,128}$') {
        return $false
    }
    foreach ($field in @('owner_pid', 'created_at', 'expires_at', 'handoff_grace_until')) {
        if (-not (Test-JsonInteger $Lease.$field)) { return $false }
    }
    return $Lease.install_root -is [string] -and -not [string]::IsNullOrWhiteSpace($Lease.install_root)
}

function Test-BridgeLeaseForAdoption([object]$Lease, [int64]$Now) {
    try {
        if (-not (Test-BridgeLeaseFieldTypes $Lease)) { return $false }
        if ([int]$Lease.schema_version -ne 1) { return $false }
        if (-not [string]::Equals([string]$Lease.lease_id, $BridgeLeaseId, [StringComparison]::Ordinal)) {
            return $false
        }
        $expectedRoot = Get-CanonicalInstallRoot $InstallRoot
        $leaseRoot = Get-CanonicalInstallRoot ([string]$Lease.install_root)
        if (-not $expectedRoot -or -not $leaseRoot -or
            -not [string]::Equals($expectedRoot, $leaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        $createdAt = [int64]$Lease.created_at
        $expiresAt = [int64]$Lease.expires_at
        $graceUntil = [int64]$Lease.handoff_grace_until
        $ownerPid = [int64]$Lease.owner_pid
        if ($ownerPid -le 0 -or $ownerPid -gt [int]::MaxValue) { return $false }
        if ($createdAt -le 0 -or $createdAt -gt ($Now + 30)) { return $false }
        if ($expiresAt -le $Now -or $expiresAt -gt ($Now + $script:BridgeLeaseMaxLifetimeSeconds)) {
            return $false
        }
        if (($expiresAt - $createdAt) -gt $script:BridgeLeaseMaxLifetimeSeconds) { return $false }
        if ($graceUntil -lt $createdAt -or $graceUntil -gt $expiresAt -or
            ($graceUntil - $createdAt) -gt $script:BridgeLeaseHandoffGraceSeconds) {
            return $false
        }
        $ownerState = Get-ProcessClaimState $ownerPid $createdAt
        if ($ownerState -eq 'dead-or-reused' -and $Now -gt $graceUntil) { return $false }
        return $true
    } catch {
        return $false
    }
}

function Adopt-BridgeQuiesceLease {
    $script:BridgeLeaseRequired = $true
    if ($BridgeLeaseId -notmatch '^[A-Za-z0-9._-]{16,128}$') {
        Write-HandoffLog 'Update aborted: this handoff has no valid bridge-quiesce capability.'
        return $false
    }
    if (-not (Test-Path -LiteralPath $BridgeLeasePath)) {
        Write-HandoffLog 'Update aborted: the expected bridge-quiesce lease is missing.'
        return $false
    }

    $stream = $null
    try {
        Assert-NoHandoffCasArtifacts $BridgeLeasePath
        # Take an exclusive snapshot, validate it, then replace the marker with
        # a complete same-directory file. Replace-BridgeLeaseAtomically checks
        # the displaced snapshot so a competing handoff cannot be overwritten.
        # The unguessable lease id authorizes this exact ownership transfer.
        $stream = Open-BridgeLeaseSnapshot
        $snapshot = Read-BridgeLeaseSnapshotFromStream $stream
        $now = Get-UnixTimeSeconds
        if (-not $snapshot -or -not (Test-BridgeLeaseForAdoption $snapshot.Lease $now)) {
            Write-HandoffLog 'Update aborted: the bridge-quiesce lease is invalid, expired, or belongs to another handoff.'
            return $false
        }
        $stream.Dispose()
        $stream = $null
        Replace-BridgeLeaseAtomically $snapshot.Raw $snapshot.Lease $now
        $script:BridgeLeaseOwned = $true
        $script:BridgeLeaseNextRefreshAt = (Get-Date).AddSeconds(30)
        Write-HandoffLog "adopted bridge-quiesce lease (owner pid $PID)"
        return $true
    } catch {
        Write-HandoffLog "Update aborted: could not adopt the bridge-quiesce lease safely: $($_.Exception.Message)"
        return $false
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Refresh-BridgeQuiesceLeaseIfOwned([switch]$Force) {
    if (-not $script:BridgeLeaseOwned) { return (-not $script:BridgeLeaseRequired) }
    if (-not $Force -and (Get-Date) -lt $script:BridgeLeaseNextRefreshAt) { return $true }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $stream = $null
        try {
            Assert-NoHandoffCasArtifacts $BridgeLeasePath
            $stream = Open-BridgeLeaseSnapshot
            $snapshot = Read-BridgeLeaseSnapshotFromStream $stream
            $lease = if ($snapshot) { $snapshot.Lease } else { $null }
            $expectedRoot = Get-CanonicalInstallRoot $InstallRoot
            $leaseRoot = if ($lease) { Get-CanonicalInstallRoot ([string]$Lease.install_root) } else { $null }
            $now = Get-UnixTimeSeconds
            if (-not (Test-BridgeLeaseForAdoption $lease $now) -or
                [int64]$lease.owner_pid -ne $PID -or -not $expectedRoot -or -not $leaseRoot -or
                -not [string]::Equals($expectedRoot, $leaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $script:BridgeLeaseOwned = $false
                $script:BridgeLeaseRefreshFailed = $true
                return $false
            }
            $stream.Dispose()
            $stream = $null
            Replace-BridgeLeaseAtomically $snapshot.Raw $lease $now
            $script:BridgeLeaseRefreshFailed = $false
            $script:BridgeLeaseNextRefreshAt = (Get-Date).AddSeconds(30)
            return $true
        } catch {
            if ($attempt -lt 3) { Start-Sleep -Milliseconds 100 }
        } finally {
            if ($stream) { $stream.Dispose() }
        }
    }
    $script:BridgeLeaseRefreshFailed = $true
    return $false
}

function Get-BridgeLeaseOwnerState(
    [int]$LauncherPid,
    [System.IntPtr]$JobHandle = [System.IntPtr]::Zero,
    [int64]$LauncherStartedAtTicks = 0
) {
    if (-not $script:BridgeLeaseRequired) { return 'not-required' }
    if (@(Get-HandoffCasArtifacts $BridgeLeasePath).Count -gt 0) { return 'unreadable' }
    if (-not (Test-Path -LiteralPath $BridgeLeasePath)) { return 'missing' }
    $stream = $null
    try {
        $stream = Open-BridgeLeaseSnapshot
        $snapshot = Read-BridgeLeaseSnapshotFromStream $stream
        $now = Get-UnixTimeSeconds
        if (-not $snapshot -or -not (Test-BridgeLeaseForAdoption $snapshot.Lease $now)) {
            return 'foreign-or-invalid'
        }
        $ownerPid = [int64]$snapshot.Lease.owner_pid
        if ($ownerPid -eq $PID) { return 'script' }
        if ($LauncherPid -le 0) {
            return 'foreign-or-invalid'
        }
        $exactLauncher = $false
        if ($ownerPid -eq $LauncherPid -and $LauncherStartedAtTicks -gt 0) {
            try {
                $launcher = [System.Diagnostics.Process]::GetProcessById($LauncherPid)
                $exactLauncher = -not $launcher.HasExited -and
                    $launcher.StartTime.ToUniversalTime().Ticks -eq $LauncherStartedAtTicks
            } catch {
                return 'unreadable'
            }
        }
        if ($exactLauncher -and
            (Get-ProcessClaimState $ownerPid ([int64]$snapshot.Lease.created_at)) -eq 'live') {
            # The deferred resume command is intentionally launched outside
            # the mutation Job so its verified gateway fleet can survive. It
            # runs the managed interpreter directly, making this exact PID the
            # only owner accepted without Job membership proof.
            $script:BridgeLeaseTransferredPid = [int]$ownerPid
            return 'child'
        }
        if ($JobHandle -eq [System.IntPtr]::Zero) { return 'foreign-or-invalid' }
        try {
            # The managed interpreter can be a descendant of the exact
            # launcher (for example, behind a console shim). The private Job
            # Object proves that the observed owner belongs to the contained
            # updater tree without accepting an arbitrary Python process.
            if ([HermesHandoff.UpdaterJob]::ContainsPid($JobHandle, [int]$ownerPid)) {
                $script:BridgeLeaseTransferredPid = [int]$ownerPid
                return 'child'
            }
        } catch {
            # OpenProcess/access-denied and IsProcessInJob failures are not
            # proof of ownership. The caller retries briefly, then cancels.
            return 'unreadable'
        }
        return 'foreign-or-invalid'
    } catch {
        return 'unreadable'
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Stop-ExactSpawnedProcessTree(
    [System.Diagnostics.Process]$Process,
    [int64]$StartedAtTicks,
    [System.IntPtr]$JobHandle = [System.IntPtr]::Zero
) {
    if ($JobHandle -ne [System.IntPtr]::Zero) {
        try {
            # This handle refers only to the Job Object created for this
            # invocation. Termination therefore reaches a shim's exact Python
            # descendants without any image-wide process matching.
            [HermesHandoff.UpdaterJob]::Terminate($JobHandle)
            # TerminateJobObject initiates termination, but return is not the
            # postcondition. Keep the exact private Job handle open until the
            # kernel reports that every associated process has drained. A
            # caller must never observe failure while a contained descendant
            # can still mutate updater state.
            while ($true) {
                try {
                    if ([HermesHandoff.UpdaterJob]::ActiveProcesses($JobHandle) -eq 0) {
                        return
                    }
                } catch {
                    # Unknown is not absence. Retain containment and retry.
                }
                Start-Sleep -Milliseconds 25
            }
        } catch {
            # Fall through to the exact launcher PID only when Job Object
            # termination itself failed.
        }
    }
    try {
        if ($Process.HasExited -or $Process.StartTime.ToUniversalTime().Ticks -ne $StartedAtTicks) {
            return
        }
        # The PID comes from the Process object we just spawned and its open
        # handle/start time were revalidated above. /T is scoped to that exact
        # updater tree; never use image-name matching or target arbitrary Python.
        & taskkill.exe /PID "$($Process.Id)" /T /F 2>$null | Out-Null
        if (-not $Process.HasExited) { $Process.Kill() }
    } catch {
        try { if (-not $Process.HasExited) { $Process.Kill() } } catch {}
    }
    while ($true) {
        try {
            if ($Process.HasExited -or
                $Process.StartTime.ToUniversalTime().Ticks -ne $StartedAtTicks) {
                return
            }
            $Process.Kill()
        } catch {
            # Unknown is not absence. Retain the exact Process handle and retry.
        }
        Start-Sleep -Milliseconds 25
    }
}

function Get-RetainedTargetBridgeLeaseOwnerState(
    [int]$TargetPid,
    [System.IntPtr]$TargetHandle,
    [string]$TargetCreationFileTime,
    [System.IntPtr]$JobHandle
) {
    if (-not $script:BridgeLeaseRequired) { return 'not-required' }
    if (@(Get-HandoffCasArtifacts $BridgeLeasePath).Count -gt 0) { return 'unreadable' }
    if (-not (Test-Path -LiteralPath $BridgeLeasePath)) { return 'missing' }
    $stream = $null
    $targetExitedAfterSnapshot = $false
    try {
        $stream = Open-BridgeLeaseSnapshot
        $snapshot = Read-BridgeLeaseSnapshotFromStream $stream
        $now = Get-UnixTimeSeconds
        if (-not $snapshot -or -not (Test-BridgeLeaseForAdoption $snapshot.Lease $now)) {
            return 'foreign-or-invalid'
        }
        $ownerPid = [int64]$snapshot.Lease.owner_pid
        if ($ownerPid -eq $PID) { return 'script' }
        if ($TargetPid -le 0 -or $ownerPid -ne $TargetPid -or
            $TargetHandle -eq [System.IntPtr]::Zero -or
            $JobHandle -eq [System.IntPtr]::Zero -or
            [string]::IsNullOrWhiteSpace($TargetCreationFileTime)) {
            return 'foreign-or-invalid'
        }
        try {
            if ([HermesHandoff.UpdaterJob]::RuntimeProcessId($TargetHandle) -ne $TargetPid -or
                -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($JobHandle, $TargetHandle) -or
                -not [string]::Equals(
                    [HermesHandoff.UpdaterJob]::RuntimeCreationFileTime($TargetHandle),
                    $TargetCreationFileTime,
                    [StringComparison]::Ordinal
                )) {
                return 'foreign-or-invalid'
            }
            if (-not [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive($TargetHandle)) {
                $targetExitedAfterSnapshot = $true
            }
        } catch {
            return 'unreadable'
        }
        if (-not $targetExitedAfterSnapshot) {
            $script:BridgeLeaseTransferredPid = $TargetPid
            return 'child'
        }
    } catch {
        return 'unreadable'
    } finally {
        if ($stream) { $stream.Dispose() }
    }
    # The exact retained target may atomically delete its lease and exit after
    # this function opens the delete-sharing snapshot. Reconcile only a fresh,
    # exact namespace absence after closing that stale snapshot. Any surviving
    # path, CAS sibling, or unreadable namespace remains fail-closed.
    try {
        if (@(Get-HandoffCasArtifacts $BridgeLeasePath).Count -gt 0) { return 'unreadable' }
        if (Test-Path -LiteralPath $BridgeLeasePath -ErrorAction Stop) {
            return 'foreign-or-invalid'
        }
        return 'missing'
    } catch {
        return 'unreadable'
    }
}

$script:DeferredGatewayDiagnosticMaxLines = 8
$script:DeferredGatewayDiagnosticMaxMessageChars = 480
$script:DeferredGatewayDiagnosticMaxOutputChars = 2048

function Protect-DeferredGatewayDiagnosticDetail(
    [string]$Value,
    [string]$StartupGate
) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return 'no detail' }
    $sanitized = $Value.Trim()
    $substitutions = @(
        [pscustomobject]@{ Value = $StartupGate; Replacement = '[STARTUP_GATE]' },
        [pscustomobject]@{ Value = $HermesHome; Replacement = '[HERMES_HOME]' },
        [pscustomobject]@{ Value = $InstallRoot; Replacement = '[INSTALL_ROOT]' },
        [pscustomobject]@{ Value = $env:TEMP; Replacement = '[TEMP]' },
        [pscustomobject]@{ Value = $script:InvocationId; Replacement = '[INVOCATION]' },
        [pscustomobject]@{ Value = $BridgeLeaseId; Replacement = '[LEASE]' },
        [pscustomobject]@{ Value = $script:AttemptId; Replacement = '[ATTEMPT]' }
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_.Value) } |
        Sort-Object { ([string]$_.Value).Length } -Descending
    foreach ($substitution in $substitutions) {
        $sanitized = [regex]::Replace(
            $sanitized,
            [regex]::Escape([string]$substitution.Value),
            [string]$substitution.Replacement,
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    $sanitized = [regex]::Replace(
        $sanitized,
        '(?i)\b(api[_-]?key|authorization|cookie|invocation(?:[_-]?id)?|lease(?:[_-]?id)?|nonce|password|secret|token)\s*([:=])\s*("[^"]*"|''[^'']*''|[^\s,;]+)',
        '$1$2[redacted]'
    )
    # Any absolute path not already recognized above is machine-specific and
    # not useful enough to justify exposing it in the user-visible handoff log.
    $sanitized = [regex]::Replace(
        $sanitized,
        '(?i)(?:[a-z]:\\|\\\\)[^,;|\r\n]*',
        '[LOCAL_PATH]'
    )
    $sanitized = [regex]::Replace($sanitized, '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '')
    if ($sanitized.Length -gt $script:DeferredGatewayDiagnosticMaxMessageChars) {
        $sanitized = $sanitized.Substring(0, $script:DeferredGatewayDiagnosticMaxMessageChars - 3) + '...'
    }
    return $sanitized
}

function ConvertTo-DeferredGatewayDiagnostic(
    [string]$Line,
    [string]$StartupGate,
    [ValidateSet('stdout', 'stderr')][string]$Source
) {
    $trimmed = if ($null -eq $Line) { '' } else { $Line.Trim() }
    $allowlist = @(
        [pscustomobject]@{ Raw = '✓ Deferred gateway fleet was already resumed.'; Safe = 'fleet already resumed' },
        [pscustomobject]@{ Raw = '✓ Deferred gateway fleet resumed.'; Safe = 'fleet resumed' },
        [pscustomobject]@{ Raw = '✗ Invalid deferred gateway resume request.'; Safe = 'invalid recovery request' },
        [pscustomobject]@{ Raw = '✗ Deferred gateway lease cleanup could not be proven.'; Safe = 'lease cleanup not proved' },
        [pscustomobject]@{ Raw = '✗ Deferred gateway fleet cleanup could not be proven.'; Safe = 'fleet cleanup not proved' },
        [pscustomobject]@{ Raw = 'deferred wrapper rejected its startup gate'; Safe = 'wrapper rejected startup gate' },
        [pscustomobject]@{ Raw = 'deferred wrapper could not read its startup gate'; Safe = 'wrapper could not read startup gate' },
        [pscustomobject]@{ Raw = 'deferred wrapper timed out before authorization'; Safe = 'wrapper authorization timed out' },
        [pscustomobject]@{ Raw = 'deferred wrapper rejected its target request'; Safe = 'wrapper rejected target request' },
        [pscustomobject]@{ Raw = 'deferred wrapper could not execute its target'; Safe = 'wrapper target execution failed' }
    )
    foreach ($entry in $allowlist) {
        if ([string]::Equals($trimmed, $entry.Raw, [StringComparison]::Ordinal)) {
            return [string]$entry.Safe
        }
    }
    foreach ($failurePrefix in @('✗ Deferred gateway resume failed:', 'Deferred gateway resume failed:')) {
        if ($trimmed.StartsWith($failurePrefix, [StringComparison]::Ordinal)) {
            $detail = $trimmed.Substring($failurePrefix.Length)
            return 'recovery failed: ' + (Protect-DeferredGatewayDiagnosticDetail $detail $StartupGate)
        }
    }
    return "$Source diagnostic omitted"
}

function Add-BoundedDeferredGatewayOutput(
    [System.Text.StringBuilder]$Builder,
    [string]$Message
) {
    $remaining = $script:DeferredGatewayDiagnosticMaxOutputChars - $Builder.Length
    if ($remaining -le 0) { return }
    $value = $Message + [Environment]::NewLine
    if ($value.Length -gt $remaining) { $value = $value.Substring(0, $remaining) }
    [void]$Builder.Append($value)
}

function Write-BoundedDeferredGatewayDiagnostic(
    [string]$Prefix,
    [string]$Message,
    [ref]$Count,
    [ref]$SuppressionLogged
) {
    if ($Count.Value -lt ($script:DeferredGatewayDiagnosticMaxLines - 1)) {
        Write-HandoffLog ("{0} {1}" -f $Prefix, $Message)
        $Count.Value++
        return
    }
    if (-not $SuppressionLogged.Value) {
        Write-HandoffLog 'gateway-resume| further child diagnostics suppressed'
        $Count.Value++
        $SuppressionLogged.Value = $true
    }
}

function Wait-ForTransferredBridgeLeaseCleanup(
    [int]$LauncherPid,
    [System.IntPtr]$JobHandle
) {
    $deadline = (Get-Date).AddSeconds(3)
    while ((Get-Date) -lt $deadline) {
        $state = Get-BridgeLeaseOwnerState $LauncherPid $JobHandle
        if ($state -eq 'missing') { return $true }
        if ($state -ne 'child' -and $state -ne 'unreadable') { return $false }
        Start-Sleep -Milliseconds 100
    }
    return (-not (Test-Path -LiteralPath $BridgeLeasePath) -and
        @(Get-HandoffCasArtifacts $BridgeLeasePath).Count -eq 0)
}

function Close-UpdaterContainment(
    [System.IntPtr]$JobHandle,
    [string]$StartupGate
) {
    if ($JobHandle -ne [System.IntPtr]::Zero) {
        try {
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE also removes any descendant
            # that outlived the launcher unexpectedly.
            [HermesHandoff.UpdaterJob]::Close($JobHandle)
        } catch {}
    }
    if ($StartupGate) {
        Remove-Item -LiteralPath $StartupGate -Force -ErrorAction SilentlyContinue
    }
}

function Remove-BridgeQuiesceLeaseIfOwned {
    if (-not $script:BridgeLeaseOwned) {
        return (-not $script:BridgeLeaseRequired -and
            -not (Test-Path -LiteralPath $BridgeLeasePath) -and
            @(Get-HandoffCasArtifacts $BridgeLeasePath).Count -eq 0)
    }
    $released = $false
    $tombstone = "$BridgeLeasePath.cas-release-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        # Move first, then inspect the exact inode we moved. A read-then-delete
        # sequence could delete a foreign lease rewritten between those calls.
        [System.IO.File]::Move($BridgeLeasePath, $tombstone)
        $raw = [System.IO.File]::ReadAllText($tombstone)
        $lease = $raw | ConvertFrom-Json -ErrorAction Stop
        $expectedRoot = Get-CanonicalInstallRoot $InstallRoot
        $leaseRoot = if ($lease) { Get-CanonicalInstallRoot ([string]$lease.install_root) } else { $null }
        if ((Test-BridgeLeaseFieldTypes $lease) -and [int]$lease.schema_version -eq 1 -and
            [string]::Equals([string]$lease.lease_id, $BridgeLeaseId, [StringComparison]::Ordinal) -and
            [int64]$lease.owner_pid -eq $PID -and $expectedRoot -and $leaseRoot -and
            [string]::Equals($expectedRoot, $leaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $tombstone -Force -ErrorAction Stop
            Write-HandoffLog 'removed bridge-quiesce lease (owned)'
            $released = $true
        } else {
            if (-not (Test-Path -LiteralPath $BridgeLeasePath)) {
                [System.IO.File]::Move($tombstone, $BridgeLeasePath)
            }
            Write-HandoffLog 'leaving bridge-quiesce lease: ownership changed'
        }
    } catch {
        if ((Test-Path -LiteralPath $tombstone) -and -not (Test-Path -LiteralPath $BridgeLeasePath)) {
            try { [System.IO.File]::Move($tombstone, $BridgeLeasePath) } catch {}
        }
        if (Test-Path -LiteralPath $tombstone) {
            Write-HandoffLog "leaving bridge-quiesce lease tombstone for recovery: $tombstone"
        } else {
            Write-HandoffLog 'leaving bridge-quiesce lease: ownership could not be verified'
        }
    }
    $script:BridgeLeaseOwned = $false
    return $released
}

function Read-UpdateMarkerSnapshot {
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $MarkerPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
        )
        if ($stream.Length -le 0 -or $stream.Length -gt 4096) { return $null }
        $bytes = New-Object byte[] ([int]$stream.Length)
        $read = $stream.Read($bytes, 0, $bytes.Length)
        if ($read -ne $bytes.Length) { return $null }
        $raw = [System.Text.Encoding]::UTF8.GetString($bytes)
        $lines = @($raw -split "`r?`n" | Where-Object { $_ -ne '' })
        $pidValue = 0L
        $startedAt = 0L
        $parsed = $lines.Count -eq 2 -and
            [int64]::TryParse($lines[0].Trim(), [ref]$pidValue) -and
            [int64]::TryParse($lines[1].Trim(), [ref]$startedAt)
        return [pscustomobject]@{
            Raw = $raw
            Parsed = [bool]$parsed
            Pid = $pidValue
            StartedAt = $startedAt
        }
    } catch {
        return $null
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Claim-UpdateMarkerAtomically {
    $now = Get-UnixTimeSeconds
    $script:UpdateMarkerStartedAt = $now
    $newRaw = "$PID`n$now`n"
    try {
        Assert-NoHandoffCasArtifacts $MarkerPath
        if (-not (Test-Path -LiteralPath $MarkerPath)) {
            Publish-HandoffFileNoGap $MarkerPath $null $newRaw 'update marker'
            $script:UpdateMarkerOwned = $true
            return $true
        }
        $snapshot = Read-UpdateMarkerSnapshot
        if (-not $snapshot -or -not $snapshot.Parsed -or
            $snapshot.Pid -le 0 -or $snapshot.Pid -gt [int]::MaxValue -or
            $snapshot.StartedAt -le 0 -or $snapshot.StartedAt -gt ($now + 5)) {
            # Readable partial/malformed/future bytes are still an unknown
            # owner. Never turn parse failure into permission to overwrite.
            return $false
        }
        $ownerState = Get-ProcessClaimState $snapshot.Pid $snapshot.StartedAt
        if ($snapshot.Pid -eq $PID) {
            if ($ownerState -ne 'live') { return $false }
            # A valid preclaim by this exact PowerShell process is already the
            # authoritative marker. Adopt its original timestamp and bytes so
            # cleanup proves the same claim; do not rewrite it.
            $script:UpdateMarkerStartedAt = [int64]$snapshot.StartedAt
            $script:UpdateMarkerOwned = $true
            return $true
        }
        if ($ownerState -ne 'dead-or-reused') {
            # A live or unreadable foreign PID is authoritative regardless of
            # marker age. Long updates must not become stealable solely
            # because they crossed a dead-marker pruning ceiling.
            return $false
        }
        Publish-HandoffFileNoGap $MarkerPath $snapshot.Raw $newRaw 'update marker'
        $script:UpdateMarkerOwned = $true
        return $true
    } catch {
        return $false
    }
}

function Show-ProgressWindow {
    if ($NoUi) { return }
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        Add-Type -AssemblyName System.Drawing | Out-Null
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "Hermes Update"
        $form.Size = New-Object System.Drawing.Size(720, 420)
        $form.StartPosition = "CenterScreen"
        $form.ControlBox = $false
        $form.TopMost = $true
        $label = New-Object System.Windows.Forms.Label
        $label.Text = "Updating Hermes -- do not close this window. Hermes restarts automatically when the update finishes."
        $label.Dock = "Top"
        $label.Height = 34
        $label.Padding = New-Object System.Windows.Forms.Padding(8, 8, 8, 0)
        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.Style = "Marquee"
        $bar.MarqueeAnimationSpeed = 30
        $bar.Dock = "Top"
        $bar.Height = 18
        $box = New-Object System.Windows.Forms.TextBox
        $box.Multiline = $true
        $box.ReadOnly = $true
        $box.ScrollBars = "Vertical"
        $box.Dock = "Fill"
        $box.Font = New-Object System.Drawing.Font("Consolas", 9)
        $form.Controls.Add($box)
        $form.Controls.Add($bar)
        $form.Controls.Add($label)
        $form.Show()
        # `cmd start /min` spawned us backgrounded; TopMost keeps the window
        # above others but does not take activation. Claim it explicitly so
        # the progress window is what the user sees during the update.
        try {
            $form.Activate()
            if ($script:Win32) { [HermesHandoff.Win32]::SetForegroundWindow($form.Handle) | Out-Null }
        } catch {}
        [System.Windows.Forms.Application]::DoEvents()
        $script:Ui = [pscustomobject]@{ Form = $form; Box = $box }
    } catch {
        # Headless session / WinForms unavailable: degrade to log-only.
        $script:Ui = $null
    }
}

function Close-ProgressWindow {
    if ($script:Ui) {
        try { $script:Ui.Form.Close() } catch {}
        $script:Ui = $null
    }
}

function New-HandoffResultJson(
    [string]$State,
    [int]$Code,
    [string]$Message,
    [bool]$UpdateMarkerReleased,
    [bool]$BridgeLeaseReleased,
    [AllowNull()][object]$DesktopProof
) {
    $hasReceipt = $null -ne $script:VerifiedUpdateReceipt
    $complete = $State -eq 'complete'
    $relaunchState = if ($complete) { 'acknowledged' } elseif ($script:RelaunchStarted) { 'failed' } else { 'failed' }
    if ($State -eq 'pending') { $relaunchState = 'pending' }
    $requestedAt = if ($script:RelaunchStarted) {
        [int64]$script:RelaunchRequestedAt
    } elseif ($hasReceipt -and $script:ReceiptVerifiedAt -gt 0) {
        [int64]$script:ReceiptVerifiedAt
    } else {
        [int64]$script:HandoffStartedAt
    }
    $finishedAt = if ($State -eq 'pending') { $null } else {
        [int64][Math]::Max((Get-UnixTimeSeconds), $requestedAt)
    }
    $obj = [ordered]@{
        schema_version = 2
        attempt_id = $script:AttemptId
        state = $State
        ok = [bool]$complete
        exit_code = if ($State -eq 'pending') { $null } else { [int]$Code }
        message = $Message
        branch = $Branch
        invocation_id = if ($hasReceipt) { [string]$script:VerifiedUpdateReceipt.invocation_id } else { $null }
        lease_id = if ($hasReceipt) { [string]$script:VerifiedUpdateReceipt.lease_id } else { $null }
        root = $InstallRoot
        receipt = $script:VerifiedUpdateReceipt
        cleanup = [ordered]@{
            update_marker_released = [bool]$UpdateMarkerReleased
            bridge_lease_released = [bool]$BridgeLeaseReleased
        }
        runtime_health = if ($hasReceipt) { $script:VerifiedUpdateReceipt.health } else { $null }
        relaunch = [ordered]@{
            state = $relaunchState
            pid = if ($script:RelaunchStarted) { [int]$script:RelaunchPid } else { $null }
            process_started_at = if ($script:RelaunchStarted) { [int64]$script:RelaunchProcessStartedAt } else { $null }
            executable = if ($script:RelaunchStarted) { $script:CanonicalRelaunchExe } else { $null }
            # Strict v2 failed results still carry a positive attempt baseline
            # even when relaunch never started. The PID/start/executable triple
            # remains all-null in that pre-spawn state.
            requested_at = $requestedAt
            acknowledged_at = if ($complete) { [int64]$DesktopProof.acknowledged_at } else { $null }
        }
        desktop = [ordered]@{
            build_id = if ($DesktopProof) { [string]$DesktopProof.build_id } else { $null }
            build_source = if ($DesktopProof) { [string]$DesktopProof.build_source } else { $null }
            root = if ($DesktopProof) { [string]$DesktopProof.root } else { $null }
            backend_ready = [bool]($DesktopProof -and $DesktopProof.backend_ready -eq $true)
            backend_mode = if ($DesktopProof) { [string]$DesktopProof.backend_mode } else { $null }
        }
        finished_at = $finishedAt
    }
    return $obj | ConvertTo-Json -Compress -Depth 12
}

function Publish-HandoffResult(
    [string]$Raw,
    [AllowNull()][object]$ExpectedRaw
) {
    try {
        if ($env:HERMES_DESKTOP_UPDATE_TEST -eq '1' -and
            $env:HERMES_TEST_RESULT_PUBLISH_FAIL -eq '1') {
            throw 'simulated result publication failure'
        }
        Publish-HandoffFileNoGap $ResultPath $ExpectedRaw $Raw 'Desktop handoff result'
        return $true
    } catch {
        Write-HandoffLog "ERROR: could not publish the exact-CAS handoff result: $($_.Exception.Message)"
        return $false
    }
}

function Read-DesktopHandoffAck {
    try {
        if (-not (Test-Path -LiteralPath $script:AckPath -PathType Leaf)) { return $null }
        $raw = [System.IO.File]::ReadAllText($script:AckPath)
        if ([string]::IsNullOrWhiteSpace($raw) -or $raw.Length -gt 65536) { return [pscustomobject]@{ Invalid = $true; Raw = $raw } }
        $ack = $raw | ConvertFrom-Json -ErrorAction Stop
        $expected = @(
            'schema_version', 'attempt_id', 'invocation_id', 'lease_id', 'pid',
            'process_started_at', 'root', 'executable', 'build_id', 'build_source',
            'backend_ready', 'backend_mode', 'acknowledged_at', 'error'
        )
        $names = @($ack.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return [pscustomobject]@{ Invalid = $true; Raw = $raw } }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return [pscustomobject]@{ Invalid = $true; Raw = $raw } }
        }
        $expectedBuild = if ($script:VerifiedUpdateReceipt.mode -eq 'git') {
            [string]$script:VerifiedUpdateReceipt.resulting_head
        } else { [string]$script:VerifiedUpdateReceipt.archive_sha }
        $ackRoot = if ($ack.root -is [string]) { Get-CanonicalInstallRoot $ack.root } else { $null }
        $ackExe = if ($ack.executable -is [string]) {
            try { [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ack.executable -ErrorAction Stop).ProviderPath) } catch { $null }
        } else { $null }
        $valid = (
            (Test-JsonInteger $ack.schema_version) -and [int64]$ack.schema_version -eq 1 -and
            $ack.attempt_id -is [string] -and [string]::Equals($ack.attempt_id, $script:AttemptId, [StringComparison]::Ordinal) -and
            $ack.invocation_id -is [string] -and [string]::Equals($ack.invocation_id, [string]$script:VerifiedUpdateReceipt.invocation_id, [StringComparison]::Ordinal) -and
            $ack.lease_id -is [string] -and [string]::Equals($ack.lease_id, [string]$script:VerifiedUpdateReceipt.lease_id, [StringComparison]::Ordinal) -and
            (Test-JsonInteger $ack.pid) -and [int64]$ack.pid -eq $script:RelaunchPid -and
            (Test-JsonInteger $ack.process_started_at) -and [int64]$ack.process_started_at -eq $script:RelaunchProcessStartedAt -and
            $ackRoot -and [string]::Equals($ackRoot, $InstallRoot, [StringComparison]::OrdinalIgnoreCase) -and
            $ackExe -and [string]::Equals($ackExe, $script:CanonicalRelaunchExe, [StringComparison]::OrdinalIgnoreCase) -and
            $ack.build_id -is [string] -and [string]::Equals($ack.build_id, $expectedBuild, [StringComparison]::OrdinalIgnoreCase) -and
            $ack.build_source -is [string] -and $ack.build_source -eq 'install-stamp' -and
            $ack.backend_ready -is [bool] -and $ack.backend_ready -eq $true -and
            $ack.backend_mode -is [string] -and $ack.backend_mode -in @('local', 'remote') -and
            (Test-JsonInteger $ack.acknowledged_at) -and [int64]$ack.acknowledged_at -ge $script:RelaunchRequestedAt -and
            [int64]$ack.acknowledged_at -le ((Get-UnixTimeSeconds) + 30) -and
            $null -eq $ack.error -and
            (Get-ProcessClaimState $script:RelaunchPid $script:RelaunchProcessStartedAt) -eq 'live'
        )
        if (-not $valid) { return [pscustomobject]@{ Invalid = $true; Raw = $raw } }
        return [pscustomobject]@{ Invalid = $false; Raw = $raw; Ack = $ack }
    } catch {
        return [pscustomobject]@{ Invalid = $true; Raw = $null }
    }
}

function Remove-DesktopAckExact([string]$ExpectedRaw) {
    $tombstone = "$($script:AckPath).cas-release-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::Move($script:AckPath, $tombstone)
        if (-not [string]::Equals([System.IO.File]::ReadAllText($tombstone), $ExpectedRaw, [StringComparison]::Ordinal)) {
            if (-not (Test-Path -LiteralPath $script:AckPath)) {
                [System.IO.File]::Move($tombstone, $script:AckPath)
            }
            return $false
        }
        Remove-Item -LiteralPath $tombstone -Force -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Wait-ForDesktopHandoffAck([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-ProcessClaimState $script:RelaunchPid $script:RelaunchProcessStartedAt) -ne 'live') {
            return [pscustomobject]@{ Status = 'exited'; Proof = $null; Raw = $null }
        }
        $observed = Read-DesktopHandoffAck
        if ($observed) {
            if ($observed.Invalid) {
                return [pscustomobject]@{ Status = 'invalid'; Proof = $null; Raw = $observed.Raw }
            }
            return [pscustomobject]@{ Status = 'acknowledged'; Proof = $observed.Ack; Raw = $observed.Raw }
        }
        Start-Sleep -Milliseconds 100
        if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
    }
    return [pscustomobject]@{ Status = 'timeout'; Proof = $null; Raw = $null }
}

function Remove-MarkerIfOwned {
    if ($NoMarkerCleanup) { return $true }
    if (-not $script:UpdateMarkerOwned) { return (-not (Test-Path -LiteralPath $MarkerPath)) }
    $tombstone = "$MarkerPath.cas-release-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        if (-not (Test-Path -LiteralPath $MarkerPath)) { return $true }
        [System.IO.File]::Move($MarkerPath, $tombstone)
        $raw = [System.IO.File]::ReadAllText($tombstone)
        $lines = @($raw -split "`r?`n" | Where-Object { $_ -ne '' })
        if ($lines.Count -eq 2 -and $lines[0].Trim() -eq "$PID" -and
            $lines[1].Trim() -eq "$($script:UpdateMarkerStartedAt)") {
            Remove-Item -LiteralPath $tombstone -Force -ErrorAction Stop
            Write-HandoffLog 'removed update marker (owned)'
            $script:UpdateMarkerOwned = $false
            return $true
        }
        if (-not (Test-Path -LiteralPath $MarkerPath)) {
            [System.IO.File]::Move($tombstone, $MarkerPath)
        }
        Write-HandoffLog 'leaving update marker: ownership changed'
    } catch {
        if ((Test-Path -LiteralPath $tombstone) -and -not (Test-Path -LiteralPath $MarkerPath)) {
            try { [System.IO.File]::Move($tombstone, $MarkerPath) } catch {}
        }
        Write-HandoffLog 'leaving update marker: exact ownership could not be verified'
    }
    $script:UpdateMarkerOwned = $false
    return $false
}

function Set-RelaunchIdentity([int]$ProcessId) {
    $deadline = (Get-Date).AddSeconds(3)
    while ((Get-Date) -lt $deadline) {
        try {
            $process = [System.Diagnostics.Process]::GetProcessById($ProcessId)
            if (-not $process.HasExited) {
                $started = [int64][DateTimeOffset]::new($process.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
                if ($started -gt 0) {
                    $script:RelaunchPid = $ProcessId
                    $script:RelaunchProcessStartedAt = $started
                    $script:RelaunchStarted = $true
                    return $true
                }
            } else { return $false }
        } catch [System.ArgumentException] {
            return $false
        } catch {
            # A transient WMI/process-table delay is retryable; access denial
            # after the bounded window withholds relaunch proof.
        }
        Start-Sleep -Milliseconds 50
    }
    return $false
}

function Start-DesktopRelaunch {
    if ($script:RelaunchSuppressed) {
        Write-HandoffLog 'Desktop relaunch suppressed because the exact single-instance handoff was not proved'
        return
    }
    if ($RelaunchExe -and (Test-Path -LiteralPath $RelaunchExe)) {
        try {
            $script:CanonicalRelaunchExe = [System.IO.Path]::GetFullPath(
                (Resolve-Path -LiteralPath $RelaunchExe -ErrorAction Stop).ProviderPath
            )
        } catch {
            Write-HandoffLog 'WARNING: requested Desktop relaunch executable could not be canonicalized'
            return
        }
        if (-not [string]::IsNullOrWhiteSpace($RelaunchAppPath)) {
            try {
                $script:CanonicalRelaunchAppPath = [System.IO.Path]::GetFullPath(
                    (Resolve-Path -LiteralPath $RelaunchAppPath -ErrorAction Stop).ProviderPath
                )
                if ($script:CanonicalRelaunchAppPath.Contains('"')) {
                    throw 'development app entry contains an unsupported quote'
                }
            } catch {
                Write-HandoffLog 'WARNING: requested Desktop relaunch app entry could not be canonicalized'
                return
            }
        }
        $script:RelaunchRequestedAt = Get-UnixTimeSeconds
        $relaunchDescription = if ($script:CanonicalRelaunchAppPath) {
            "$RelaunchExe $script:CanonicalRelaunchAppPath"
        } else {
            $RelaunchExe
        }
        Write-HandoffLog "relaunching desktop: $relaunchDescription"
        # DO NOT spawn Hermes.exe as our child: Electron/Chromium calls
        # AttachConsole(ATTACH_PARENT_PROCESS) at boot, so a Desktop launched
        # directly from this console PowerShell latches onto OUR console --
        # the console window then outlives the script (it can't close while
        # an attached process lives), and closing it kills the freshly
        # relaunched GUI with it. Create the process via WMI instead: the
        # parent becomes WmiPrvSE.exe and there is no console to inherit or
        # attach -- same detachment explorer.exe gives a normal launch.
        $spawned = $false
        try {
            $workDir = Split-Path -Parent $RelaunchExe
            $commandLine = ('"{0}"' -f $RelaunchExe)
            if ($script:CanonicalRelaunchAppPath) {
                $commandLine += (' "{0}"' -f $script:CanonicalRelaunchAppPath)
            }
            $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
                CommandLine      = $commandLine
                CurrentDirectory = $workDir
            } -ErrorAction Stop
            if ($r -and $r.ReturnValue -eq 0) {
                Write-HandoffLog "desktop relaunched detached (pid $($r.ProcessId))"
                $spawned = $true
                if (-not (Set-RelaunchIdentity ([int]$r.ProcessId))) {
                    Write-HandoffLog 'WARNING: Desktop relaunch process identity could not be proved'
                }
            } else {
                Write-HandoffLog "WARNING: WMI relaunch returned $($r.ReturnValue); falling back"
            }
        } catch {
            Write-HandoffLog "WARNING: WMI relaunch failed: $($_.Exception.Message); falling back"
        }
        if (-not $spawned) {
            try {
                # Fallback keeps the old behavior (console tie-in and all) --
                # a tethered Desktop beats no Desktop.
                $fallbackArgs = @{
                    FilePath = $RelaunchExe
                    WorkingDirectory = (Split-Path -Parent $RelaunchExe)
                    PassThru = $true
                }
                if ($script:CanonicalRelaunchAppPath) {
                    $fallbackArgs.ArgumentList = @($script:CanonicalRelaunchAppPath)
                }
                $fallback = Start-Process @fallbackArgs
                if (-not (Set-RelaunchIdentity ([int]$fallback.Id))) {
                    Write-HandoffLog 'WARNING: fallback Desktop relaunch process identity could not be proved'
                }
            } catch {
                Write-HandoffLog "WARNING: desktop relaunch failed: $($_.Exception.Message)"
            }
        }
    } elseif ($script:RelaunchRequired) {
        Write-HandoffLog 'WARNING: requested desktop relaunch target is missing'
    }
}

function Get-ExactRelaunchProcessClaims {
    if (-not $script:CanonicalRelaunchExe) {
        return [pscustomobject]@{ Valid = $false; Claims = @() }
    }
    $name = [System.IO.Path]::GetFileNameWithoutExtension($script:CanonicalRelaunchExe)
    $claims = @()
    try {
        $candidates = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        foreach ($process in $candidates) {
            try { if ($process.HasExited) { continue } } catch { continue }
            try {
                $path = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
                if (-not [string]::Equals($path, $script:CanonicalRelaunchExe, [StringComparison]::OrdinalIgnoreCase)) {
                    continue
                }
                $startedAt = [int64][DateTimeOffset]::new(
                    $process.StartTime.ToUniversalTime()
                ).ToUnixTimeSeconds()
                if ($startedAt -le 0) { return [pscustomobject]@{ Valid = $false; Claims = @() } }
                $claims += [pscustomobject]@{ Pid = [int]$process.Id; StartedAt = $startedAt }
            } catch {
                try { if ($process.HasExited) { continue } } catch {}
                # A same-image process whose path/creation identity is
                # unreadable may own Electron's single-instance lock. It is not
                # safe to launch around an unproved candidate.
                return [pscustomobject]@{ Valid = $false; Claims = @() }
            }
        }
        return [pscustomobject]@{ Valid = $true; Claims = @($claims) }
    } catch {
        return [pscustomobject]@{ Valid = $false; Claims = @() }
    }
}

function Read-RelaunchExitAck([object]$Claim) {
    $path = Join-Path $HermesHome (".hermes-update-relaunch-exit-ack-{0}-{1}.json" -f $script:AttemptId, $Claim.Pid)
    try {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
        $raw = [System.IO.File]::ReadAllText($path)
        if ([string]::IsNullOrWhiteSpace($raw) -or $raw.Length -gt 65536) { return $false }
        $ack = $raw | ConvertFrom-Json -ErrorAction Stop
        $expected = @('schema_version', 'attempt_id', 'pid', 'process_started_at', 'root', 'executable', 'acknowledged_at', 'action')
        $names = @($ack.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $false }
        foreach ($field in $expected) { if ($names -notcontains $field) { return $false } }
        $root = if ($ack.root -is [string]) { Get-CanonicalInstallRoot $ack.root } else { $null }
        $exe = if ($ack.executable -is [string]) {
            try { [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ack.executable -ErrorAction Stop).ProviderPath) } catch { $null }
        } else { $null }
        if (-not (
            (Test-JsonInteger $ack.schema_version) -and [int64]$ack.schema_version -eq 1 -and
            $ack.attempt_id -is [string] -and [string]::Equals($ack.attempt_id, $script:AttemptId, [StringComparison]::Ordinal) -and
            (Test-JsonInteger $ack.pid) -and [int64]$ack.pid -eq $Claim.Pid -and
            (Test-JsonInteger $ack.process_started_at) -and [int64]$ack.process_started_at -eq $Claim.StartedAt -and
            $root -and [string]::Equals($root, $InstallRoot, [StringComparison]::OrdinalIgnoreCase) -and
            $exe -and [string]::Equals($exe, $script:CanonicalRelaunchExe, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-JsonInteger $ack.acknowledged_at) -and [int64]$ack.acknowledged_at -ge $script:RelaunchRequestStartedAt -and
            [int64]$ack.acknowledged_at -le ((Get-UnixTimeSeconds) + 30) -and
            $ack.action -is [string] -and $ack.action -eq 'quit'
        )) {
            Write-HandoffLog "Desktop single-instance exit ACK failed exact identity validation (pid $($Claim.Pid))"
            return $false
        }
        return [pscustomobject]@{ Path = $path; Raw = $raw }
    } catch {
        Write-HandoffLog "Desktop single-instance exit ACK could not be read exactly (pid $($Claim.Pid))"
        return $false
    }
}

function Remove-RelaunchExitAckExact([object]$Observed) {
    $tombstone = "$($Observed.Path).cas-release-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::Move($Observed.Path, $tombstone)
        if (-not [string]::Equals([System.IO.File]::ReadAllText($tombstone), $Observed.Raw, [StringComparison]::Ordinal)) {
            if (-not (Test-Path -LiteralPath $Observed.Path)) { [System.IO.File]::Move($tombstone, $Observed.Path) }
            return $false
        }
        Remove-Item -LiteralPath $tombstone -Force -ErrorAction Stop
        return $true
    } catch {
        if ((Test-Path -LiteralPath $tombstone) -and -not (Test-Path -LiteralPath $Observed.Path)) {
            try { [System.IO.File]::Move($tombstone, $Observed.Path) } catch {}
        }
        return $false
    }
}

function Wait-ForExactRelaunchProcessExit([object]$Claim, [DateTime]$Deadline) {
    while ((Get-Date) -lt $Deadline) {
        if ((Get-ProcessClaimState $Claim.Pid $Claim.StartedAt) -eq 'dead-or-reused') { return $true }
        Start-Sleep -Milliseconds 50
    }
    return (Get-ProcessClaimState $Claim.Pid $Claim.StartedAt) -eq 'dead-or-reused'
}

function Prepare-DesktopSingleInstanceHandoff {
    if (-not $script:RelaunchRequired) { return $true }
    try {
        $script:CanonicalRelaunchExe = [System.IO.Path]::GetFullPath(
            (Resolve-Path -LiteralPath $RelaunchExe -ErrorAction Stop).ProviderPath
        )
    } catch {
        Write-HandoffLog 'Desktop single-instance handoff could not canonicalize the relaunch executable'
        return $false
    }
    if (-not $script:RelaunchRequestPublished) {
        $script:RelaunchRequestedAt = Get-UnixTimeSeconds
        $script:RelaunchRequestStartedAt = [int64]$script:RelaunchRequestedAt
        $request = [ordered]@{
            schema_version = 1
            attempt_id = $script:AttemptId
            root = $InstallRoot
            executable = $script:CanonicalRelaunchExe
            requested_at = [int64]$script:RelaunchRequestedAt
            expires_at = [int64]$script:RelaunchRequestedAt + 120
        } | ConvertTo-Json -Compress
        try {
            Publish-HandoffFileNoGap $script:RelaunchRequestPath $null $request 'Desktop relaunch request'
            $script:RelaunchRequestRaw = $request
            $script:RelaunchRequestPublished = $true
        } catch {
            Write-HandoffLog "Desktop single-instance request publication failed: $($_.Exception.Message)"
            return $false
        }
    }

    $deadline = (Get-Date).AddSeconds($(if ($env:HERMES_DESKTOP_UPDATE_TEST -eq '1') { 10 } else { 60 }))
    $handled = @{}
    while ((Get-Date) -lt $deadline) {
        $snapshot = Get-ExactRelaunchProcessClaims
        if (-not $snapshot.Valid) { return $false }
        $pending = @($snapshot.Claims | Where-Object { -not $handled.ContainsKey("$($_.Pid):$($_.StartedAt)") })
        if ($pending.Count -eq 0) {
            # Close the snapshot→spawn race: require two consecutive empty
            # exact-image scans while the request remains published.
            Start-Sleep -Milliseconds 100
            $verify = Get-ExactRelaunchProcessClaims
            if (-not $verify.Valid) { return $false }
            if (@($verify.Claims).Count -eq 0) {
                # A process can ACK and exit between enumeration calls. Consume
                # that exact dead-owner ACK before declaring the survivor set
                # empty; otherwise an honest fast quitter leaves an orphaned
                # attempt artifact behind.
                $ackFiles = @(Get-ChildItem -LiteralPath $HermesHome -Filter ".hermes-update-relaunch-exit-ack-$($script:AttemptId)-*.json" -File -ErrorAction SilentlyContinue)
                if ($ackFiles.Count -eq 0) { return $true }
                Write-HandoffLog "observed $($ackFiles.Count) late Desktop single-instance exit ACK(s)"
                foreach ($ackFile in $ackFiles) {
                    try {
                        $candidate = [System.IO.File]::ReadAllText($ackFile.FullName) | ConvertFrom-Json -ErrorAction Stop
                        if (-not (Test-JsonInteger $candidate.pid) -or
                            -not (Test-JsonInteger $candidate.process_started_at)) { return $false }
                        $claim = [pscustomobject]@{
                            Pid = [int]$candidate.pid
                            StartedAt = [int64]$candidate.process_started_at
                        }
                        $observed = Read-RelaunchExitAck $claim
                        if (-not $observed -or
                            -not (Wait-ForExactRelaunchProcessExit $claim $deadline) -or
                            -not (Remove-RelaunchExitAckExact $observed)) { return $false }
                        Write-HandoffLog "consumed exact late Desktop single-instance exit ACK (pid $($claim.Pid))"
                    } catch { return $false }
                }
                continue
            }
            continue
        }
        foreach ($claim in $pending) {
            Write-HandoffLog "waiting for exact Desktop single-instance exit ACK (pid $($claim.Pid))"
            $ack = $null
            while ((Get-Date) -lt $deadline -and $null -eq $ack) {
                $ack = Read-RelaunchExitAck $claim
                if ($ack -eq $false) { return $false }
                if ($null -eq $ack) { Start-Sleep -Milliseconds 50 }
            }
            if ($null -eq $ack -or -not (Remove-RelaunchExitAckExact $ack) -or
                -not (Wait-ForExactRelaunchProcessExit $claim $deadline)) {
                Write-HandoffLog "Desktop single-instance exit ACK/exit proof did not complete (pid $($claim.Pid))"
                return $false
            }
            Write-HandoffLog "consumed exact Desktop single-instance exit ACK (pid $($claim.Pid))"
            $handled["$($claim.Pid):$($claim.StartedAt)"] = $true
        }
    }
    Write-HandoffLog 'Desktop single-instance exit handoff timed out'
    return $false
}

function Remove-RelaunchRequestExact {
    if (-not $script:RelaunchRequestPublished) { return $true }
    $tombstone = "$($script:RelaunchRequestPath).cas-release-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::Move($script:RelaunchRequestPath, $tombstone)
        if (-not [string]::Equals([System.IO.File]::ReadAllText($tombstone), $script:RelaunchRequestRaw, [StringComparison]::Ordinal)) {
            if (-not (Test-Path -LiteralPath $script:RelaunchRequestPath)) {
                [System.IO.File]::Move($tombstone, $script:RelaunchRequestPath)
            }
            return $false
        }
        Remove-Item -LiteralPath $tombstone -Force -ErrorAction Stop
        $script:RelaunchRequestPublished = $false
        return (Retire-LateRelaunchExitAcks)
    } catch {
        if ((Test-Path -LiteralPath $tombstone) -and -not (Test-Path -LiteralPath $script:RelaunchRequestPath)) {
            try { [System.IO.File]::Move($tombstone, $script:RelaunchRequestPath) } catch {}
        }
        return $false
    }
}

function Retire-LateRelaunchExitAcks {
    # A Desktop may have completed its exact-byte request revalidation just
    # before this script renames the request. Give that bounded in-flight write
    # a chance to publish, then exact-validate/consume it. Three consecutive
    # empty scans after request removal prove the local publication window shut.
    $deadline = (Get-Date).AddSeconds(2)
    $emptyScans = 0
    $filter = ".hermes-update-relaunch-exit-ack-$($script:AttemptId)-*.json"
    while ((Get-Date) -lt $deadline) {
        $files = @(Get-ChildItem -LiteralPath $HermesHome -Filter $filter -File -ErrorAction SilentlyContinue)
        if ($files.Count -eq 0) {
            $emptyScans++
            if ($emptyScans -ge 3) { return $true }
            Start-Sleep -Milliseconds 50
            continue
        }
        $emptyScans = 0
        foreach ($file in $files) {
            try {
                $candidate = [System.IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json -ErrorAction Stop
                if (-not (Test-JsonInteger $candidate.pid) -or
                    -not (Test-JsonInteger $candidate.process_started_at)) { return $false }
                $claim = [pscustomobject]@{
                    Pid = [int]$candidate.pid
                    StartedAt = [int64]$candidate.process_started_at
                }
                $observed = Read-RelaunchExitAck $claim
                if (-not $observed) { return $false }
                if (-not (Wait-ForExactRelaunchProcessExit $claim $deadline)) { return $false }
                if (-not (Remove-RelaunchExitAckExact $observed)) { return $false }
            } catch {
                return $false
            }
        }
        Start-Sleep -Milliseconds 50
    }
    return $false
}

function Focus-RelaunchedDesktop {
    if ($NoUi -or -not $script:RelaunchStarted -or -not $script:Win32) { return }
    # Result publication MUST precede this optional 20-second window poll. The
    # relaunched Desktop starts consuming the result immediately; focus is not
    # part of the transaction proof and must never delay that durable record.
    try {
        [HermesHandoff.Win32]::AllowSetForegroundWindow($script:RelaunchPid) | Out-Null
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline) {
            $hwnd = [System.IntPtr]::Zero
            try {
                $process = Get-Process -Id $script:RelaunchPid -ErrorAction Stop
                $hwnd = $process.MainWindowHandle
            } catch { break }
            if ($hwnd -ne [System.IntPtr]::Zero) {
                [HermesHandoff.Win32]::ShowWindow($hwnd, 9) | Out-Null
                [HermesHandoff.Win32]::SetForegroundWindow($hwnd) | Out-Null
                Write-HandoffLog 'focused relaunched desktop window'
                break
            }
            Start-Sleep -Milliseconds 400
        }
    } catch {
        Write-HandoffLog "WARNING: could not focus relaunched desktop: $($_.Exception.Message)"
    }
}

function Invoke-StreamedHermes(
    [string]$Exe,
    [string[]]$HermesArgs,
    [string]$Tag,
    [switch]$AllowBridgeLeaseTransfer,
    [switch]$RequireBridgeLeaseReturn,
    [switch]$SkipBridgeLeaseCheck
) {
    # Start-Process + output file + poll keeps the WinForms window pumping
    # during long silent stretches (pip installs); a blocking pipeline would
    # freeze the marquee. Returns @{ Code; Output }.
    if (-not $SkipBridgeLeaseCheck -and
        -not (Refresh-BridgeQuiesceLeaseIfOwned -Force)) {
        return @{
            Code = 8
            ChildExitCode = $null
            Output = 'Update aborted: the bridge-quiesce lease could not be refreshed before mutation.'
            Stdout = ''
            Stderr = 'Update aborted: the bridge-quiesce lease could not be refreshed before mutation.'
        }
    }
    if (-not $script:JobContainmentAvailable) {
        return @{
            Code = 8
            ChildExitCode = $null
            Output = 'Update aborted: exact updater process containment is unavailable.'
            Stdout = ''
            Stderr = 'Update aborted: exact updater process containment is unavailable.'
        }
    }
    $outFile = Join-Path $env:TEMP ("hermes-handoff-{0}-{1}.out" -f $Tag, $PID)
    $errFile = Join-Path $env:TEMP ("hermes-handoff-{0}-{1}.err" -f $Tag, $PID)
    Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    $startupGate = Join-Path $env:TEMP ("hermes-updater-job-gate-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString('N'))
    $jobHandle = [System.IntPtr]::Zero
    $proc = $null
    $childStartedAtTicks = 0L

    # Process.Start has no CREATE_SUSPENDED switch on the .NET Framework used
    # by Windows PowerShell 5.1. Start a fixed wrapper that cannot invoke the
    # managed updater until Rust/PowerShell has assigned it to our private
    # kill-on-close Job Object and released this unique gate. Every process it
    # creates afterward is contained automatically.
    $wrapperScript = @'
$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {}
$gate = $env:HERMES_INTERNAL_UPDATE_JOB_GATE
for ($attempt = 0; $attempt -lt 300 -and -not (Test-Path -LiteralPath $gate); $attempt++) {
    Start-Sleep -Milliseconds 100
}
if (-not (Test-Path -LiteralPath $gate)) { exit 13 }
$request = $env:HERMES_INTERNAL_UPDATE_REQUEST | ConvertFrom-Json -ErrorAction Stop
$program = [string]$request.program
$childArgs = @($request.arguments | ForEach-Object { [string]$_ })
$env:HERMES_INTERNAL_UPDATE_WRAPPER_PID = "$PID"
& $program @childArgs
if ($null -eq $LASTEXITCODE) { exit 1 }
exit $LASTEXITCODE
'@
    try {
        Remove-Item -LiteralPath $startupGate -Force -ErrorAction SilentlyContinue
        $jobHandle = [HermesHandoff.UpdaterJob]::CreateKillOnClose()
        $encodedWrapper = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($wrapperScript))
        $powershellPath = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        if ([string]::IsNullOrWhiteSpace($powershellPath)) {
            throw 'could not resolve the current PowerShell executable'
        }

        # System.Diagnostics.Process directly: Start-Process's .ExitCode is
        # unreliably $null under PS 5.1 even with the Handle-touch workaround.
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $powershellPath
        $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encodedWrapper"
        # The managed interpreter must import this checkout even when the
        # Desktop handoff was launched from another Hermes worktree.
        $psi.WorkingDirectory = $InstallRoot
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        # The wrapper and managed Python both emit UTF-8 through these pipes.
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
        $psi.EnvironmentVariables['HERMES_INTERNAL_UPDATE_JOB_GATE'] = $startupGate
        $request = [ordered]@{
            program = $Exe
            arguments = [object[]]$HermesArgs
        } | ConvertTo-Json -Compress -Depth 3
        $psi.EnvironmentVariables['HERMES_INTERNAL_UPDATE_REQUEST'] = $request
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc) { throw 'the contained updater launcher did not start' }
        $childStartedAtTicks = $proc.StartTime.ToUniversalTime().Ticks
        [HermesHandoff.UpdaterJob]::Assign($jobHandle, $proc.Handle)
        # Gate release is deliberately after AssignProcessToJobObject. The
        # wrapper cannot create the real Python updater during the gap.
        [System.IO.File]::WriteAllText(
            $startupGate,
            'go',
            (New-Object System.Text.UTF8Encoding($false))
        )
    } catch {
        if ($proc) {
            Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
        }
        Close-UpdaterContainment $jobHandle $startupGate
        return @{
            Code = 8
            ChildExitCode = $null
            Output = 'Update aborted: the spawned Hermes updater could not be contained before execution.'
            Stdout = ''
            Stderr = 'Update aborted: the spawned Hermes updater could not be contained before execution.'
        }
    }
    $outWriter = $null
    $errWriter = $null
    $containmentFinished = $false
    try {
    $outWriter = [System.IO.File]::CreateText($outFile)
    $errWriter = [System.IO.File]::CreateText($errFile)
    $nextBridgeLeaseRefresh = (Get-Date).AddSeconds(30)
    $outTask = $proc.StandardOutput.ReadLineAsync()
    $errTask = $proc.StandardError.ReadLineAsync()
    $outClosed = $false
    $errClosed = $false
    $leaseLost = $false
    $suppressChildOutput = $Tag -eq 'preflight'
    $expectLeaseTransfer = $AllowBridgeLeaseTransfer -and $script:BridgeLeaseRequired
    $leaseTransferObserved = $false
    $leaseReturnedToScript = $false
    $unreadableLeasePolls = 0
    $stageDeadline = (Get-Date).AddSeconds((Get-ManagedStageTimeoutSeconds $Tag))
    $rootExitedAt = $null
    $stageTimedOut = $false
    # Read stdout and stderr concurrently. EndOfStream/ReadLine are blocking
    # calls on silent pipes; using them here used to stall the UI, stop lease
    # renewal, and deadlock when stderr filled its pipe buffer.
    while (-not $proc.HasExited -or -not $outClosed -or -not $errClosed) {
        if ((Get-Date) -ge $stageDeadline) {
            $stageTimedOut = $true
            Write-HandoffLog "the contained $Tag stage exceeded its bounded deadline; terminating its exact Job"
            Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
        }
        if ($proc.HasExited) {
            if (-not $rootExitedAt) { $rootExitedAt = Get-Date }
            if ((-not $outClosed -or -not $errClosed) -and
                (Get-Date) -ge $rootExitedAt.AddSeconds($script:OuterJobDrainSeconds)) {
                $stageTimedOut = $true
                Write-HandoffLog "the $Tag launcher exited but contained descendants kept its streams open; terminating its exact Job"
                Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
            }
        }
        $tasks = @()
        if (-not $outClosed) { $tasks += $outTask }
        if (-not $errClosed) { $tasks += $errTask }
        if ($tasks.Count -gt 0) {
            $completed = [System.Threading.Tasks.Task]::WaitAny(
                [System.Threading.Tasks.Task[]]$tasks,
                150
            )
            if ($completed -ge 0) {
                $task = $tasks[$completed]
                if ([object]::ReferenceEquals($task, $outTask)) {
                    $ln = $outTask.Result
                    if ($null -eq $ln) {
                        $outClosed = $true
                    } else {
                        $outWriter.WriteLine($ln)
                        if (-not $suppressChildOutput -and $ln.Trim()) {
                            Write-HandoffLog ("{0}| {1}" -f $Tag, $ln)
                        }
                        $outTask = $proc.StandardOutput.ReadLineAsync()
                    }
                } else {
                    $ln = $errTask.Result
                    if ($null -eq $ln) {
                        $errClosed = $true
                    } else {
                        $errWriter.WriteLine($ln)
                        if (-not $suppressChildOutput -and $ln.Trim()) {
                            Write-HandoffLog ("{0}!| {1}" -f $Tag, $ln)
                        }
                        $errTask = $proc.StandardError.ReadLineAsync()
                    }
                }
            }
        } else {
            Start-Sleep -Milliseconds 150
        }
        if ($expectLeaseTransfer -and -not $leaseLost) {
            $leaseState = Get-BridgeLeaseOwnerState $proc.Id $jobHandle
            if ($leaseState -eq 'child') {
                if (-not $leaseTransferObserved) {
                    $leaseTransferObserved = $true
                    $script:BridgeLeaseOwned = $false
                    Write-HandoffLog "contained Hermes updater adopted bridge-quiesce lease (pid $($script:BridgeLeaseTransferredPid))"
                }
                $unreadableLeasePolls = 0
            } elseif ($leaseState -eq 'script' -and -not $leaseTransferObserved) {
                $unreadableLeasePolls = 0
                if ((Get-Date) -ge $nextBridgeLeaseRefresh) {
                    if (-not (Refresh-BridgeQuiesceLeaseIfOwned)) { $leaseLost = $true }
                    $nextBridgeLeaseRefresh = (Get-Date).AddSeconds(30)
                }
            } elseif ($leaseState -eq 'script' -and $leaseTransferObserved -and $RequireBridgeLeaseReturn) {
                $script:BridgeLeaseOwned = $true
                $leaseReturnedToScript = $true
                $unreadableLeasePolls = 0
            } elseif ($leaseState -eq 'missing' -and $leaseTransferObserved) {
                # The child clears only its own lease in its terminal cleanup.
                # Allow a short process-exit edge, but never a live mutator with
                # no quiesce marker.
                if (-not $proc.WaitForExit(500)) { $leaseLost = $true }
            } elseif ($leaseState -eq 'unreadable') {
                $unreadableLeasePolls++
                if ($unreadableLeasePolls -ge 3) { $leaseLost = $true }
            } else {
                $leaseLost = $true
            }
            if ($leaseLost) {
                Write-HandoffLog 'bridge-quiesce lease was lost; stopping the exact spawned Hermes update tree'
                Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
            }
        } elseif ($script:BridgeLeaseOwned -and (Get-Date) -ge $nextBridgeLeaseRefresh) {
            if (-not (Refresh-BridgeQuiesceLeaseIfOwned)) {
                Write-HandoffLog 'bridge-quiesce lease was lost; stopping the exact spawned Hermes process tree'
                Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
                $leaseLost = $true
            }
            $nextBridgeLeaseRefresh = (Get-Date).AddSeconds(30)
        }
        if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
    }
    $outWriter.Close(); $errWriter.Close()
    $proc.WaitForExit()
    $childExitCode = [int]$proc.ExitCode
    $code = $childExitCode
    $drainDeadline = (Get-Date).AddSeconds($script:OuterJobDrainSeconds)
    $activeProcesses = [uint32]::MaxValue
    while ((Get-Date) -lt $drainDeadline) {
        try { $activeProcesses = [HermesHandoff.UpdaterJob]::ActiveProcesses($jobHandle) } catch { break }
        if ($activeProcesses -eq 0) { break }
        Start-Sleep -Milliseconds 50
    }
    if ($activeProcesses -ne 0) {
        Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
        $stageTimedOut = $true
        $code = 8
        Write-HandoffLog "the $Tag launcher exited with contained descendants still active; completion withheld"
    }
    $stdoutText = ""
    try { $stdoutText = [System.IO.File]::ReadAllText($outFile) } catch {}
    $errText = ""
    try { $errText = [System.IO.File]::ReadAllText($errFile) } catch {}
    $all = $stdoutText
    if ($errText) { $all += "`n" + $errText }
    if ($expectLeaseTransfer) {
        if ($RequireBridgeLeaseReturn) {
            $finalLeaseState = Get-BridgeLeaseOwnerState $proc.Id $jobHandle
            if (-not $leaseTransferObserved -or -not $leaseReturnedToScript -or
                $finalLeaseState -ne 'script') {
                $leaseLost = $true
            }
        } else {
            if (-not $leaseTransferObserved -or
                -not (Wait-ForTransferredBridgeLeaseCleanup $proc.Id $jobHandle)) {
                $leaseLost = $true
            } else {
                $script:BridgeLeaseRequired = $false
                $script:BridgeLeaseTransferredPid = 0
            }
        }
    } elseif (-not $SkipBridgeLeaseCheck -and
        -not (Refresh-BridgeQuiesceLeaseIfOwned -Force)) {
        $leaseLost = $true
    }
    if ($leaseLost) {
        $code = 8
        $all += "`nUpdate aborted: the bridge-quiesce lease was lost during the $Tag stage."
    }
    if ($stageTimedOut) {
        $code = 8
        $all += "`nUpdate aborted: the contained $Tag stage did not reach a bounded, drained terminal state."
    }
    $containmentFinished = $true
    return @{
        Code = $code
        ChildExitCode = $childExitCode
        Output = $all
        Stdout = $stdoutText
        Stderr = $errText
    }
    } catch {
        Write-HandoffLog "the contained $Tag process failed before a verified terminal state"
        return @{
            Code = 8
            ChildExitCode = $null
            Output = "Update aborted: the contained $Tag process could not be monitored safely."
            Stdout = ''
            Stderr = "Update aborted: the contained $Tag process could not be monitored safely."
        }
    } finally {
        if (-not $containmentFinished -and $proc) {
            Stop-ExactSpawnedProcessTree $proc $childStartedAtTicks $jobHandle
        }
        if ($outWriter) { try { $outWriter.Dispose() } catch {} }
        if ($errWriter) { try { $errWriter.Dispose() } catch {} }
        Close-UpdaterContainment $jobHandle $startupGate
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-DeferredGatewayConsumeArtifacts {
    $parent = Split-Path -Parent $script:GatewayPlanPath
    $leaf = Split-Path -Leaf $script:GatewayPlanPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { return @() }
    return @([System.IO.Directory]::EnumerateFiles($parent, "$leaf.consume-*"))
}

function Get-Sha256Hex([string]$Value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Read-DeferredGatewayPlanProof {
    try {
        $pending = Test-Path -LiteralPath $script:GatewayPlanPath -PathType Leaf
        $completed = Test-Path -LiteralPath $script:GatewayPlanCompletedPath -PathType Leaf
        if ($pending -eq $completed -or @(Get-DeferredGatewayConsumeArtifacts).Count -gt 0) {
            return $null
        }
        $source = if ($pending) { 'pending' } else { 'completed' }
        $path = if ($pending) { $script:GatewayPlanPath } else { $script:GatewayPlanCompletedPath }
        $raw = [System.IO.File]::ReadAllText($path)
        if ([string]::IsNullOrWhiteSpace($raw) -or $raw.Length -gt 262144) { return $null }
        $plan = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($plan -isnot [pscustomobject]) { return $null }
        $expected = @(
            'schema_version', 'invocation_id', 'lease_fingerprint', 'install_root',
            'created_at', 'expires_at', 'profiles', 'cold_start_if_installed', 'auth'
        )
        $names = @($plan.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $null }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $null }
        }
        if (-not (Test-JsonInteger $plan.schema_version) -or [int64]$plan.schema_version -ne 1 -or
            $plan.invocation_id -isnot [string] -or
            -not [string]::Equals($plan.invocation_id, $script:InvocationId, [StringComparison]::Ordinal) -or
            $plan.lease_fingerprint -isnot [string] -or
            -not [string]::Equals($plan.lease_fingerprint, (Get-Sha256Hex $BridgeLeaseId), [StringComparison]::OrdinalIgnoreCase) -or
            $plan.install_root -isnot [string] -or
            $plan.cold_start_if_installed -isnot [bool] -or
            $plan.auth -isnot [string] -or $plan.auth -notmatch '^[0-9a-fA-F]{64}$' -or
            -not (Test-JsonInteger $plan.created_at) -or
            -not (Test-JsonInteger $plan.expires_at)) {
            return $null
        }
        $planRoot = Get-CanonicalInstallRoot $plan.install_root
        if (-not $planRoot -or
            -not [string]::Equals($planRoot, $InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        $now = Get-UnixTimeSeconds
        $created = [int64]$plan.created_at
        $expires = [int64]$plan.expires_at
        if ($created -le 0 -or $created -gt ($now + 5) -or
            $expires -lt $created -or $expires -lt $now -or
            ($expires - $created) -gt $script:GatewayPlanMaxLifetimeSeconds) {
            return $null
        }
        if ($plan.profiles -isnot [System.Array]) { return $null }
        $profileNames = @{}
        foreach ($profile in @($plan.profiles)) {
            if ($profile -isnot [pscustomobject]) { return $null }
            $profileFields = @($profile.PSObject.Properties | ForEach-Object { $_.Name })
            if ($profileFields.Count -ne 3 -or
                $profileFields -notcontains 'name' -or
                $profileFields -notcontains 'old_pid' -or
                $profileFields -notcontains 'created_at' -or
                $profile.name -isnot [string] -or
                $profile.name -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
                $profileNames.ContainsKey($profile.name) -or
                -not (Test-JsonInteger $profile.old_pid) -or [int64]$profile.old_pid -le 0) {
                return $null
            }
            $createdValue = 0.0
            try { $createdValue = [double]$profile.created_at } catch { return $null }
            if ([double]::IsNaN($createdValue) -or [double]::IsInfinity($createdValue) -or
                $createdValue -le 0) { return $null }
            $profileNames[$profile.name] = $true
        }
        # The exact hidden Python child validates the HMAC before it can emit
        # its adoption frame or consume these bytes. Native proof pins the same
        # raw pending->completed transition so a sampled lease state is never
        # the sole acknowledgment.
        return [pscustomobject]@{ Source = $source; Path = $path; Raw = $raw; Plan = $plan }
    } catch {
        return $null
    }
}

function ConvertTo-CanonicalAsciiJsonString([string]$Value) {
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    foreach ($character in $Value.ToCharArray()) {
        $code = [int][char]$character
        # `continue` inside a PowerShell switch advances the switch, not this
        # foreach, which previously appended the original character again.
        if ($code -eq 8) { [void]$builder.Append('\b'); continue }
        if ($code -eq 9) { [void]$builder.Append('\t'); continue }
        if ($code -eq 10) { [void]$builder.Append('\n'); continue }
        if ($code -eq 12) { [void]$builder.Append('\f'); continue }
        if ($code -eq 13) { [void]$builder.Append('\r'); continue }
        if ($code -eq 34) { [void]$builder.Append('\"'); continue }
        if ($code -eq 92) { [void]$builder.Append('\\'); continue }
        if ($code -lt 32 -or $code -gt 126) {
            [void]$builder.Append(('\u{0:x4}' -f $code))
        } else {
            [void]$builder.Append($character)
        }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-HmacSha256Hex([string]$Value, [string]$Key) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256(
        ,([System.Text.Encoding]::UTF8.GetBytes($Key))
    )
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($hmac.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hmac.Dispose()
    }
}

function Read-PreparedGatewayNativeProofCore(
    [string]$ExpectedPlanRaw,
    [System.IntPtr]$JobHandle,
    [bool]$RequireLiveRuntimes
) {
    $script:PreparedGatewayProofState = 'unreadable'
    $runtimeHandles = New-Object 'System.Collections.Generic.List[System.IntPtr]'
    $launcherHandles = New-Object 'System.Collections.Generic.List[System.IntPtr]'
    $proofSucceeded = $false
    try {
        if (($RequireLiveRuntimes -and $JobHandle -eq [System.IntPtr]::Zero) -or
            (Test-Path -LiteralPath $script:GatewayPlanPath) -or
            (Test-Path -LiteralPath $script:GatewayPlanCompletedPath) -or
            -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $script:GatewayRuntimeManifestPath -PathType Leaf) -or
            @(Get-DeferredGatewayConsumeArtifacts).Count -gt 0) {
            $script:PreparedGatewayProofState = 'artifact-state'
            return $null
        }
        $preparedRaw = [System.IO.File]::ReadAllText($script:GatewayPlanPreparedPath)
        if (-not [string]::Equals($preparedRaw, $ExpectedPlanRaw, [StringComparison]::Ordinal)) {
            $script:PreparedGatewayProofState = 'plan-bytes'
            return $null
        }
        $manifestInfo = Get-Item -LiteralPath $script:GatewayRuntimeManifestPath -ErrorAction Stop
        if ($manifestInfo.Length -le 0 -or $manifestInfo.Length -gt 65536) {
            $script:PreparedGatewayProofState = 'manifest-size'
            return $null
        }
        $manifestRaw = [System.IO.File]::ReadAllText($script:GatewayRuntimeManifestPath)
        $manifest = $manifestRaw | ConvertFrom-Json -ErrorAction Stop
        if ($manifest -isnot [pscustomobject]) {
            $script:PreparedGatewayProofState = 'manifest-shape'
            return $null
        }
        $expectedFields = @(
            'schema_version', 'invocation_id', 'install_root', 'plan_sha256',
            'launchers', 'runtimes', 'auth'
        )
        $manifestFields = @($manifest.PSObject.Properties | ForEach-Object { $_.Name })
        if ($manifestFields.Count -ne $expectedFields.Count) {
            $script:PreparedGatewayProofState = 'manifest-fields'
            return $null
        }
        foreach ($field in $expectedFields) {
            if ($manifestFields -notcontains $field) {
                $script:PreparedGatewayProofState = 'manifest-fields'
                return $null
            }
        }
        if (-not (Test-JsonInteger $manifest.schema_version) -or [int64]$manifest.schema_version -ne 2 -or
            $manifest.invocation_id -isnot [string] -or
            -not [string]::Equals($manifest.invocation_id, $script:InvocationId, [StringComparison]::Ordinal) -or
            $manifest.install_root -isnot [string] -or
            $manifest.plan_sha256 -isnot [string] -or $manifest.plan_sha256 -notmatch '^[0-9a-f]{64}$' -or
            -not [string]::Equals($manifest.plan_sha256, (Get-Sha256Hex $ExpectedPlanRaw), [StringComparison]::Ordinal) -or
            $manifest.auth -isnot [string] -or $manifest.auth -notmatch '^[0-9a-f]{64}$' -or
            $manifest.launchers -isnot [System.Array] -or
            $manifest.runtimes -isnot [System.Array]) {
            $script:PreparedGatewayProofState = 'manifest-core'
            return $null
        }
        $manifestRoot = Get-CanonicalInstallRoot $manifest.install_root
        if (-not $manifestRoot -or
            -not [string]::Equals($manifestRoot, $InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $script:PreparedGatewayProofState = 'install-root'
            return $null
        }
        $runtimeValues = @($manifest.runtimes)
        if ($runtimeValues.Count -gt 64) {
            $script:PreparedGatewayProofState = 'runtime-count'
            return $null
        }
        $runtimeJson = New-Object System.Collections.Generic.List[string]
        $runtimePidsByProfile = @{}
        $runtimePids = @{}
        $lastProfile = $null
        $lastPid = 0
        foreach ($runtime in $runtimeValues) {
            if ($runtime -isnot [pscustomobject]) {
                $script:PreparedGatewayProofState = 'runtime-shape'
                return $null
            }
            $runtimeFields = @($runtime.PSObject.Properties | ForEach-Object { $_.Name })
            $expectedRuntimeFields = @(
                'profile', 'pid', 'created_at', 'creation_file_time',
                'executable_path', 'profile_home'
            )
            if ($runtimeFields.Count -ne $expectedRuntimeFields.Count) {
                $script:PreparedGatewayProofState = 'runtime-fields'
                return $null
            }
            foreach ($field in $expectedRuntimeFields) {
                if ($runtimeFields -notcontains $field) {
                    $script:PreparedGatewayProofState = 'runtime-fields'
                    return $null
                }
            }
            if ($runtime.profile -isnot [string] -or $runtime.profile -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
                -not (Test-JsonInteger $runtime.pid) -or [int64]$runtime.pid -le 0 -or [int64]$runtime.pid -gt [int]::MaxValue -or
                $runtime.creation_file_time -isnot [string] -or
                $runtime.creation_file_time -notmatch '^[1-9][0-9]{0,19}$' -or
                $runtime.executable_path -isnot [string] -or $runtime.profile_home -isnot [string]) {
                $script:PreparedGatewayProofState = 'runtime-core'
                return $null
            }
            $createdAt = 0.0
            try { $createdAt = [double]$runtime.created_at } catch {
                $script:PreparedGatewayProofState = 'runtime-created'
                return $null
            }
            if ([double]::IsNaN($createdAt) -or [double]::IsInfinity($createdAt) -or $createdAt -le 0) {
                $script:PreparedGatewayProofState = 'runtime-created'
                return $null
            }
            $creationFileTime = [uint64]0
            if (-not [uint64]::TryParse(
                [string]$runtime.creation_file_time,
                [System.Globalization.NumberStyles]::None,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [ref]$creationFileTime
            ) -or $creationFileTime -le 0) {
                $script:PreparedGatewayProofState = 'runtime-filetime'
                return $null
            }
            $profileCompare = if ($null -eq $lastProfile) { 1 } else {
                [string]::CompareOrdinal([string]$runtime.profile, [string]$lastProfile)
            }
            if ($profileCompare -lt 0 -or ($profileCompare -eq 0 -and [int]$runtime.pid -le $lastPid)) {
                $script:PreparedGatewayProofState = 'runtime-order'
                return $null
            }
            $lastProfile = [string]$runtime.profile
            $lastPid = [int]$runtime.pid
            $runtimePidsByProfile[[string]$runtime.profile] = [int]$runtime.pid
            $runtimePids[[int]$runtime.pid] = $true
            $expectedProfileHome = if ($runtime.profile -eq 'default') {
                $HermesHome
            } else {
                Join-Path (Join-Path $HermesHome 'profiles') $runtime.profile
            }
            $profileHome = (Resolve-Path -LiteralPath $runtime.profile_home -ErrorAction Stop).ProviderPath
            $profileHome = [System.IO.Path]::GetFullPath($profileHome).TrimEnd([char[]]@('\', '/'))
            $expectedProfileHome = [System.IO.Path]::GetFullPath($expectedProfileHome).TrimEnd([char[]]@('\', '/'))
            if (-not [string]::Equals($profileHome, $expectedProfileHome, [StringComparison]::OrdinalIgnoreCase)) {
                $script:PreparedGatewayProofState = 'profile-home'
                return $null
            }
            $executablePath = (Resolve-Path -LiteralPath $runtime.executable_path -ErrorAction Stop).ProviderPath
            $executablePath = [System.IO.Path]::GetFullPath($executablePath)
            $installPrefix = $InstallRoot.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
            if (-not $executablePath.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                $script:PreparedGatewayProofState = 'executable-scope'
                return $null
            }
            if ($RequireLiveRuntimes) {
                $runtimeHandle = [HermesHandoff.UpdaterJob]::OpenRuntimeProcess([int]$runtime.pid)
                $runtimeHandles.Add($runtimeHandle)
                if (-not [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive($runtimeHandle) -or
                    -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($JobHandle, $runtimeHandle)) {
                    $script:PreparedGatewayProofState = 'job-membership'
                    return $null
                }
                $liveCreationFileTime = [HermesHandoff.UpdaterJob]::RuntimeCreationFileTime($runtimeHandle)
                if (-not [string]::Equals(
                    $liveCreationFileTime,
                    [string]$runtime.creation_file_time,
                    [StringComparison]::Ordinal
                )) {
                    $script:PreparedGatewayProofState = 'runtime-generation'
                    return $null
                }
                $liveExecutablePath = [System.IO.Path]::GetFullPath(
                    [HermesHandoff.UpdaterJob]::RuntimeExecutablePath($runtimeHandle)
                )
                if (-not [string]::Equals(
                    $liveExecutablePath,
                    $executablePath,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    $script:PreparedGatewayProofState = 'runtime-executable'
                    return $null
                }
                $liveCreatedAt = [Math]::Round(($creationFileTime / 10000000.0) - 11644473600.0, 2)
                if ([Math]::Abs($liveCreatedAt - $createdAt) -gt 0.001) {
                    $script:PreparedGatewayProofState = 'runtime-generation'
                    return $null
                }
                $runtimeStatusPath = Join-Path $profileHome 'gateway_state.json'
                $runtimeStatus = [System.IO.File]::ReadAllText($runtimeStatusPath) | ConvertFrom-Json -ErrorAction Stop
                if ($runtimeStatus -isnot [pscustomobject] -or
                    $runtimeStatus.kind -ne 'hermes-gateway' -or $runtimeStatus.gateway_state -ne 'running' -or
                    -not (Test-JsonInteger $runtimeStatus.pid) -or [int64]$runtimeStatus.pid -ne [int64]$runtime.pid -or
                    -not (Test-JsonInteger $runtimeStatus.start_time) -or
                    [int64]$runtimeStatus.start_time -ne [int64][Math]::Round($createdAt * 100) -or
                    $runtimeStatus.hermes_home -isnot [string]) {
                    $script:PreparedGatewayProofState = 'runtime-status'
                    return $null
                }
                $statusHome = (Resolve-Path -LiteralPath $runtimeStatus.hermes_home -ErrorAction Stop).ProviderPath
                $statusHome = [System.IO.Path]::GetFullPath($statusHome).TrimEnd([char[]]@('\', '/'))
                if (-not [string]::Equals($statusHome, $profileHome, [StringComparison]::OrdinalIgnoreCase)) {
                    $script:PreparedGatewayProofState = 'runtime-status-home'
                    return $null
                }
            }
            $createdText = $createdAt.ToString('0.0#', [System.Globalization.CultureInfo]::InvariantCulture)
            $runtimeJson.Add(
                '{"created_at":' + $createdText +
                ',"creation_file_time":' + (ConvertTo-CanonicalAsciiJsonString ([string]$runtime.creation_file_time)) +
                ',"executable_path":' + (ConvertTo-CanonicalAsciiJsonString ([string]$runtime.executable_path)) +
                ',"pid":' + ([int]$runtime.pid).ToString([System.Globalization.CultureInfo]::InvariantCulture) +
                ',"profile":' + (ConvertTo-CanonicalAsciiJsonString ([string]$runtime.profile)) +
                ',"profile_home":' + (ConvertTo-CanonicalAsciiJsonString ([string]$runtime.profile_home)) + '}'
            )
        }
        $launcherValues = @($manifest.launchers)
        if ($launcherValues.Count -gt 128) {
            $script:PreparedGatewayProofState = 'launcher-count'
            return $null
        }
        $launcherJson = New-Object System.Collections.Generic.List[string]
        $launcherPids = @{}
        $launcherKinds = @{}
        $lastLauncherProfile = $null
        $lastLauncherPid = 0
        foreach ($launcher in $launcherValues) {
            if ($launcher -isnot [pscustomobject]) {
                $script:PreparedGatewayProofState = 'launcher-shape'
                return $null
            }
            $launcherFields = @($launcher.PSObject.Properties | ForEach-Object { $_.Name })
            $expectedLauncherFields = @(
                'profile', 'pid', 'created_at', 'creation_file_time', 'executable_path'
            )
            if ($launcherFields.Count -ne $expectedLauncherFields.Count) {
                $script:PreparedGatewayProofState = 'launcher-fields'
                return $null
            }
            foreach ($field in $expectedLauncherFields) {
                if ($launcherFields -notcontains $field) {
                    $script:PreparedGatewayProofState = 'launcher-fields'
                    return $null
                }
            }
            if ($launcher.profile -isnot [string] -or $launcher.profile -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
                -not $runtimePidsByProfile.ContainsKey([string]$launcher.profile) -or
                -not (Test-JsonInteger $launcher.pid) -or [int64]$launcher.pid -le 0 -or
                [int64]$launcher.pid -gt [int]::MaxValue -or
                $runtimePids.ContainsKey([int]$launcher.pid) -or
                $launcherPids.ContainsKey([int]$launcher.pid) -or
                $launcher.creation_file_time -isnot [string] -or
                $launcher.creation_file_time -notmatch '^[1-9][0-9]{0,19}$' -or
                $launcher.executable_path -isnot [string]) {
                $script:PreparedGatewayProofState = 'launcher-core'
                return $null
            }
            $launcherCreatedAt = 0.0
            try { $launcherCreatedAt = [double]$launcher.created_at } catch {
                $script:PreparedGatewayProofState = 'launcher-created'
                return $null
            }
            if ([double]::IsNaN($launcherCreatedAt) -or
                [double]::IsInfinity($launcherCreatedAt) -or $launcherCreatedAt -le 0) {
                $script:PreparedGatewayProofState = 'launcher-created'
                return $null
            }
            $launcherCreationFileTime = [uint64]0
            if (-not [uint64]::TryParse(
                [string]$launcher.creation_file_time,
                [System.Globalization.NumberStyles]::None,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [ref]$launcherCreationFileTime
            ) -or $launcherCreationFileTime -le 0) {
                $script:PreparedGatewayProofState = 'launcher-filetime'
                return $null
            }
            $launcherProfileCompare = if ($null -eq $lastLauncherProfile) { 1 } else {
                [string]::CompareOrdinal([string]$launcher.profile, [string]$lastLauncherProfile)
            }
            if ($launcherProfileCompare -lt 0 -or
                ($launcherProfileCompare -eq 0 -and [int]$launcher.pid -le $lastLauncherPid)) {
                $script:PreparedGatewayProofState = 'launcher-order'
                return $null
            }
            $lastLauncherProfile = [string]$launcher.profile
            $lastLauncherPid = [int]$launcher.pid
            $launcherPids[[int]$launcher.pid] = $true
            $launcherExecutablePath = (
                Resolve-Path -LiteralPath $launcher.executable_path -ErrorAction Stop
            ).ProviderPath
            $launcherExecutablePath = [System.IO.Path]::GetFullPath($launcherExecutablePath)
            $installPrefix = $InstallRoot.TrimEnd([char[]]@('\', '/')) +
                [System.IO.Path]::DirectorySeparatorChar
            $systemConsoleHost = [System.IO.Path]::GetFullPath(
                (Join-Path $env:SystemRoot 'System32\conhost.exe')
            )
            $launcherKind = if ([string]::Equals(
                $launcherExecutablePath, $systemConsoleHost,
                [StringComparison]::OrdinalIgnoreCase
            )) { 'console' } else { 'launcher' }
            $launcherKindKey = ([string]$launcher.profile) + '|' + $launcherKind
            if ($launcherKinds.ContainsKey($launcherKindKey) -or
                ($launcherKind -eq 'launcher' -and -not $launcherExecutablePath.StartsWith(
                    $installPrefix, [StringComparison]::OrdinalIgnoreCase
                ))) {
                $script:PreparedGatewayProofState = 'launcher-scope'
                return $null
            }
            $launcherKinds[$launcherKindKey] = $true
            if ($RequireLiveRuntimes) {
                $launcherHandle = [HermesHandoff.UpdaterJob]::OpenRuntimeProcess([int]$launcher.pid)
                $launcherHandles.Add($launcherHandle)
                if (-not [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive($launcherHandle) -or
                    -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($JobHandle, $launcherHandle) -or
                    -not [string]::Equals(
                        [HermesHandoff.UpdaterJob]::RuntimeCreationFileTime($launcherHandle),
                        [string]$launcher.creation_file_time,
                        [StringComparison]::Ordinal
                    ) -or
                    -not [string]::Equals(
                        [System.IO.Path]::GetFullPath(
                            [HermesHandoff.UpdaterJob]::RuntimeExecutablePath($launcherHandle)
                        ),
                        $launcherExecutablePath,
                        [StringComparison]::OrdinalIgnoreCase
                    )) {
                    $script:PreparedGatewayProofState = 'launcher-identity'
                    return $null
                }
                $liveLauncherCreatedAt = [Math]::Round(
                    ($launcherCreationFileTime / 10000000.0) - 11644473600.0, 2
                )
                if ([Math]::Abs($liveLauncherCreatedAt - $launcherCreatedAt) -gt 0.001) {
                    $script:PreparedGatewayProofState = 'launcher-generation'
                    return $null
                }
            }
            $launcherCreatedText = $launcherCreatedAt.ToString(
                '0.0#', [System.Globalization.CultureInfo]::InvariantCulture
            )
            $launcherJson.Add(
                '{"created_at":' + $launcherCreatedText +
                ',"creation_file_time":' +
                    (ConvertTo-CanonicalAsciiJsonString ([string]$launcher.creation_file_time)) +
                ',"executable_path":' +
                    (ConvertTo-CanonicalAsciiJsonString ([string]$launcher.executable_path)) +
                ',"pid":' + ([int]$launcher.pid).ToString(
                    [System.Globalization.CultureInfo]::InvariantCulture
                ) +
                ',"profile":' +
                    (ConvertTo-CanonicalAsciiJsonString ([string]$launcher.profile)) + '}'
            )
        }
        if ($RequireLiveRuntimes) {
            # The terminated wrapper can remain in Job accounting for a short
            # native teardown edge after WaitForExit. Wait only for that bounded
            # decrement; an unauthorized live member keeps the proof failed.
            $memberCountDeadline = (Get-Date).AddMilliseconds(750)
            do {
                $activeRuntimeCount = [HermesHandoff.UpdaterJob]::ActiveProcesses($JobHandle)
                if ($activeRuntimeCount -eq [uint32]($runtimeValues.Count + $launcherValues.Count)) { break }
                Start-Sleep -Milliseconds 25
            } while ((Get-Date) -lt $memberCountDeadline)
            if ($activeRuntimeCount -ne [uint32]($runtimeValues.Count + $launcherValues.Count)) {
                $script:PreparedGatewayProofState = 'job-member-count'
                return $null
            }
        }
        $unsigned = '{"install_root":' + (ConvertTo-CanonicalAsciiJsonString ([string]$manifest.install_root)) +
            ',"invocation_id":' + (ConvertTo-CanonicalAsciiJsonString ([string]$manifest.invocation_id)) +
            ',"launchers":[' + ([string]::Join(',', $launcherJson.ToArray())) + ']' +
            ',"plan_sha256":' + (ConvertTo-CanonicalAsciiJsonString ([string]$manifest.plan_sha256)) +
            ',"runtimes":[' + ([string]::Join(',', $runtimeJson.ToArray())) + '],"schema_version":2}'
        if ($TestMode -and
            [System.IO.Path]::IsPathRooted($env:HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE)) {
            [System.IO.File]::WriteAllText(
                ($env:HERMES_TEST_MANIFEST_UNSIGNED_CAPTURE + '.native'),
                $unsigned,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        $expectedAuth = Get-HmacSha256Hex $unsigned $BridgeLeaseId
        if (-not [string]::Equals($expectedAuth, [string]$manifest.auth, [StringComparison]::Ordinal)) {
            $script:PreparedGatewayProofState = 'manifest-auth'
            return $null
        }
        $script:PreparedGatewayProofState = 'valid'
        $proof = [pscustomobject]@{
            PreparedRaw = $preparedRaw
            ManifestRaw = $manifestRaw
            Runtimes = $runtimeValues
            Launchers = $launcherValues
            RuntimeHandles = @($runtimeHandles.ToArray())
            LauncherHandles = @($launcherHandles.ToArray())
        }
        $proofSucceeded = $true
        return $proof
    } catch {
        return $null
    } finally {
        if (-not $proofSucceeded) {
            foreach ($runtimeHandle in $runtimeHandles) {
                [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($runtimeHandle)
            }
            foreach ($launcherHandle in $launcherHandles) {
                [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($launcherHandle)
            }
        }
    }
}

function Read-AuthenticatedPreparedGatewayState([string]$ExpectedPlanRaw) {
    # This proof authenticates durable bytes and canonical scope only. It is
    # intentionally independent of runtime liveness so a failed, already-
    # drained attempt can still restore its exact pending plan safely.
    return Read-PreparedGatewayNativeProofCore `
        $ExpectedPlanRaw ([System.IntPtr]::Zero) $false
}

function Read-PreparedGatewayNativeCommitProof(
    [string]$ExpectedPlanRaw,
    [System.IntPtr]$JobHandle
) {
    # Commit authority additionally requires every authenticated runtime to be
    # live, exact-generation, in the private Job, and the complete Job member
    # set at this final native boundary.
    return Read-PreparedGatewayNativeProofCore $ExpectedPlanRaw $JobHandle $true
}

function Restore-PreparedGatewayPlanExact([string]$ExpectedPlanRaw) {
    try {
        if ((Test-Path -LiteralPath $script:GatewayPlanPath) -or
            (Test-Path -LiteralPath $script:GatewayPlanCompletedPath) -or
            -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath -PathType Leaf) -or
            -not [string]::Equals(
                [System.IO.File]::ReadAllText($script:GatewayPlanPreparedPath),
                $ExpectedPlanRaw,
                [StringComparison]::Ordinal
            )) {
            return $false
        }
        [HermesHandoff.UpdaterJob]::HardLink($script:GatewayPlanPath, $script:GatewayPlanPreparedPath)
        if (-not [string]::Equals(
            [System.IO.File]::ReadAllText($script:GatewayPlanPath),
            $ExpectedPlanRaw,
            [StringComparison]::Ordinal
        )) { return $false }
        Remove-Item -LiteralPath $script:GatewayPlanPreparedPath -Force -ErrorAction Stop
        return -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath) -and
            [string]::Equals([System.IO.File]::ReadAllText($script:GatewayPlanPath), $ExpectedPlanRaw, [StringComparison]::Ordinal)
    } catch {
        return $false
    }
}

function Commit-PreparedGatewayPlanExact([string]$ExpectedPlanRaw, [string]$ExpectedManifestRaw) {
    $moved = $false
    try {
        if ((Test-Path -LiteralPath $script:GatewayPlanPath) -or
            (Test-Path -LiteralPath $script:GatewayPlanCompletedPath) -or
            -not [string]::Equals([System.IO.File]::ReadAllText($script:GatewayPlanPreparedPath), $ExpectedPlanRaw, [StringComparison]::Ordinal) -or
            -not [string]::Equals([System.IO.File]::ReadAllText($script:GatewayRuntimeManifestPath), $ExpectedManifestRaw, [StringComparison]::Ordinal)) {
            return $false
        }
        [System.IO.File]::Move($script:GatewayPlanPreparedPath, $script:GatewayPlanCompletedPath)
        $moved = $true
        if (-not [string]::Equals([System.IO.File]::ReadAllText($script:GatewayPlanCompletedPath), $ExpectedPlanRaw, [StringComparison]::Ordinal)) {
            throw 'prepared gateway plan changed during native commit'
        }
        Remove-Item -LiteralPath $script:GatewayRuntimeManifestPath -Force -ErrorAction Stop
        if ((Test-Path -LiteralPath $script:GatewayRuntimeManifestPath) -or
            -not [string]::Equals([System.IO.File]::ReadAllText($script:GatewayPlanCompletedPath), $ExpectedPlanRaw, [StringComparison]::Ordinal)) {
            throw 'prepared gateway runtime manifest could not be retired'
        }
        return $true
    } catch {
        if ($moved -and (Test-Path -LiteralPath $script:GatewayPlanCompletedPath -PathType Leaf)) {
            try {
                if ([string]::Equals([System.IO.File]::ReadAllText($script:GatewayPlanCompletedPath), $ExpectedPlanRaw, [StringComparison]::Ordinal)) {
                    if (-not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath)) {
                        [System.IO.File]::Move($script:GatewayPlanCompletedPath, $script:GatewayPlanPreparedPath)
                    } else {
                        Remove-Item -LiteralPath $script:GatewayPlanCompletedPath -Force -ErrorAction Stop
                    }
                }
            } catch {}
        }
        return $false
    }
}

function Restore-PreparedGatewayAttemptAfterNativeFailure(
    [string]$ExpectedPlanRaw,
    [AllowEmptyString()][string]$ExpectedManifestRaw
) {
    $restoredPending = Restore-PreparedGatewayPlanExact $ExpectedPlanRaw
    $manifestRetired = -not (Test-Path -LiteralPath $script:GatewayRuntimeManifestPath)
    if (-not $manifestRetired -and -not [string]::IsNullOrEmpty($ExpectedManifestRaw)) {
        try {
            $manifestStillExact = [string]::Equals(
                [System.IO.File]::ReadAllText($script:GatewayRuntimeManifestPath),
                $ExpectedManifestRaw,
                [StringComparison]::Ordinal
            )
            if ($manifestStillExact) {
                Remove-Item -LiteralPath $script:GatewayRuntimeManifestPath -Force -ErrorAction Stop
                $manifestRetired = -not (Test-Path -LiteralPath $script:GatewayRuntimeManifestPath)
            }
        } catch {
            $manifestRetired = $false
        }
    }
    return $restoredPending -and $manifestRetired -and
        -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath) -and
        @(Get-DeferredGatewayConsumeArtifacts).Count -eq 0
}

function Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure(
    [string]$ExpectedPlanRaw
) {
    # A child can fail before consuming the pending plan or can restore it
    # itself. Treat only the exact original pending bytes with no remaining
    # prepared protocol artifacts as already restored.
    try {
        if ((Test-Path -LiteralPath $script:GatewayPlanPath -PathType Leaf) -and
            -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath) -and
            -not (Test-Path -LiteralPath $script:GatewayRuntimeManifestPath) -and
            -not (Test-Path -LiteralPath $script:GatewayPlanCompletedPath) -and
            @(Get-DeferredGatewayConsumeArtifacts).Count -eq 0 -and
            [string]::Equals(
                [System.IO.File]::ReadAllText($script:GatewayPlanPath),
                $ExpectedPlanRaw,
                [StringComparison]::Ordinal
            )) {
            return $true
        }
    } catch {}

    # Once the Job is drained, runtime liveness is expected to be false. Use
    # only authenticated durable bytes as rollback authority; never infer
    # authority from a dead PID, a path, or an unauthenticated manifest.
    $authenticatedState = Read-AuthenticatedPreparedGatewayState $ExpectedPlanRaw
    if (-not $authenticatedState) { return $false }
    return Restore-PreparedGatewayAttemptAfterNativeFailure `
        $ExpectedPlanRaw ([string]$authenticatedState.ManifestRaw)
}

function Remove-DeferredGatewayStartupGateExact([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $true }
    try {
        if (Test-Path -LiteralPath $Path) {
            Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
        }
        $parent = Split-Path -Parent $Path
        $leaf = Split-Path -Leaf $Path
        $artifacts = @(
            [System.IO.Directory]::EnumerateFiles($parent, "$leaf.next-*")
            [System.IO.Directory]::EnumerateFiles($parent, "$leaf.previous-*")
        )
        return -not (Test-Path -LiteralPath $Path) -and $artifacts.Count -eq 0
    } catch {
        return $false
    }
}

function New-DeferredGatewayStartupGate {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    for ($attempt = 0; $attempt -lt 4; $attempt++) {
        $path = Join-Path ([System.IO.Path]::GetTempPath()) (
            "hermes-deferred-gateway-start-{0}-{1}.gate" -f $PID, [Guid]::NewGuid().ToString('N')
        )
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $path,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
            )
            $bytes = $encoding.GetBytes('wait')
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
            return $path
        } catch [System.IO.IOException] {
            if ($attempt -ge 3) { throw }
        } finally {
            if ($stream) { $stream.Dispose() }
        }
    }
    throw 'the deferred gateway startup gate could not be created exclusively'
}

function Set-DeferredGatewayStartupGateStateExact(
    [string]$Path,
    [string]$Expected,
    [ValidateSet('armed', 'abort')][string]$State
) {
    if ([string]::IsNullOrWhiteSpace($Path) -or
        ($Expected -ne 'wait' -and $Expected -ne 'armed')) {
        throw 'the deferred gateway startup gate transition is invalid'
    }
    $snapshot = $null
    $shadow = "$Path.next-$PID-$([Guid]::NewGuid().ToString('N'))"
    $previous = "$Path.previous-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        $snapshot = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
        )
        if ($snapshot.Length -le 0 -or $snapshot.Length -gt 16) {
            throw 'the deferred gateway startup gate is malformed'
        }
        $bytes = New-Object byte[] ([int]$snapshot.Length)
        $count = $snapshot.Read($bytes, 0, $bytes.Length)
        if ($count -ne $bytes.Length) {
            throw 'the deferred gateway startup gate is unreadable'
        }
        $raw = [System.Text.Encoding]::UTF8.GetString($bytes)
        if (-not [string]::Equals($raw, $Expected, [StringComparison]::Ordinal)) {
            throw 'the deferred gateway startup gate changed before transition'
        }

        $encoding = New-Object System.Text.UTF8Encoding($false)
        $writer = [System.IO.File]::Open(
            $shadow,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::Read
        )
        try {
            $nextBytes = $encoding.GetBytes($State)
            $writer.Write($nextBytes, 0, $nextBytes.Length)
            $writer.Flush($true)
        } finally {
            $writer.Dispose()
        }

        # The child polls with a short-lived read handle. Retry only sharing
        # collisions; every other native failure remains fail-closed.
        $snapshot.Dispose()
        $snapshot = $null
        $replaceDeadline = (Get-Date).AddSeconds(2)
        while ($true) {
            try {
                [System.IO.File]::Replace($shadow, $Path, $previous, $true)
                break
            } catch [System.IO.IOException] {
                if ((Get-Date) -ge $replaceDeadline) { throw }
                Start-Sleep -Milliseconds 25
            }
        }
        if (-not [string]::Equals(
            [System.IO.File]::ReadAllText($previous, [System.Text.Encoding]::UTF8),
            $Expected,
            [StringComparison]::Ordinal
        ) -or -not [string]::Equals(
            [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8),
            $State,
            [StringComparison]::Ordinal
        )) {
            throw 'the deferred gateway startup gate transition could not be proved'
        }
        Remove-Item -LiteralPath $previous -Force -ErrorAction Stop
    } finally {
        if ($snapshot) { $snapshot.Dispose() }
        if (Test-Path -LiteralPath $shadow) {
            Remove-Item -LiteralPath $shadow -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $previous) {
            Remove-Item -LiteralPath $previous -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-UnassignedDeferredGatewayResumeAndProve(
    [System.Diagnostics.Process]$Process,
    [int64]$StartedAtTicks,
    [string]$StartupGate,
    [switch]$SkipInitialKill
) {
    if (-not $Process) { return $true }
    try {
        if ($Process.HasExited -or
            $Process.StartTime.ToUniversalTime().Ticks -ne $StartedAtTicks) {
            return $true
        }
    } catch {
        return $false
    }

    try {
        Set-DeferredGatewayStartupGateStateExact $StartupGate 'wait' 'abort'
    } catch {
        # Keep the exact wait file in place. Missing/malformed/inaccessible
        # state is never converted into authorization to spawn a writer.
    }

    if (-not $SkipInitialKill) {
        try {
            $Process.Kill()
            $Process.WaitForExit()
        } catch {}
    }

    $deadline = (Get-Date).AddSeconds(11)
    while ((Get-Date) -lt $deadline) {
        try {
            if ($Process.HasExited) { return $true }
            if ($Process.StartTime.ToUniversalTime().Ticks -ne $StartedAtTicks) { return $true }
        } catch {
            return $false
        }
        Start-Sleep -Milliseconds 25
    }

    try {
        if (-not $Process.HasExited -and
            $Process.StartTime.ToUniversalTime().Ticks -eq $StartedAtTicks) {
            $Process.Kill()
            $Process.WaitForExit()
        }
        return $Process.HasExited
    } catch {
        return $false
    }
}

function Open-DeferredGatewayTargetStartFrame(
    [string]$Line,
    [string]$ExpectedExecutable,
    [System.IntPtr]$JobHandle
) {
    $targetHandle = [System.IntPtr]::Zero
    try {
        $frame = $Line | ConvertFrom-Json -ErrorAction Stop
        if ($frame -isnot [pscustomobject]) { return $false }
        $expected = @(
            'schema_version', 'event', 'target_pid',
            'creation_file_time', 'executable_path'
        )
        $names = @($frame.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $false }
        foreach ($name in $expected) { if ($names -notcontains $name) { return $false } }
        if (-not (Test-JsonInteger $frame.schema_version) -or [int64]$frame.schema_version -ne 1 -or
            $frame.event -isnot [string] -or $frame.event -ne 'deferred-gateway-target-started' -or
            -not (Test-JsonInteger $frame.target_pid) -or [int64]$frame.target_pid -le 0 -or
            [int64]$frame.target_pid -gt [int]::MaxValue -or
            $frame.creation_file_time -isnot [string] -or
            $frame.creation_file_time -notmatch '^[1-9][0-9]{0,19}$' -or
            $frame.executable_path -isnot [string] -or
            $JobHandle -eq [System.IntPtr]::Zero) {
            return $null
        }
        $expectedPath = [HermesHandoff.UpdaterJob]::CanonicalFilePath(
            [System.IO.Path]::GetFullPath(
                (Resolve-Path -LiteralPath $ExpectedExecutable -ErrorAction Stop).ProviderPath
            )
        )
        $framePath = [HermesHandoff.UpdaterJob]::CanonicalFilePath(
            [System.IO.Path]::GetFullPath(
                (Resolve-Path -LiteralPath ([string]$frame.executable_path) -ErrorAction Stop).ProviderPath
            )
        )
        if (-not [string]::Equals($framePath, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        $targetPid = [int]$frame.target_pid
        # The wrapper retains its Process handle until this parent responds on
        # the target gate. That pins the process object while this exact handle
        # is opened, preventing PID reuse between the frame and native proof.
        $targetHandle = [HermesHandoff.UpdaterJob]::OpenRuntimeProcess($targetPid)
        if ([HermesHandoff.UpdaterJob]::RuntimeProcessId($targetHandle) -ne $targetPid -or
            -not [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive($targetHandle) -or
            -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($JobHandle, $targetHandle) -or
            -not [string]::Equals(
                [HermesHandoff.UpdaterJob]::RuntimeCreationFileTime($targetHandle),
                [string]$frame.creation_file_time,
                [StringComparison]::Ordinal
            )) {
            return $null
        }
        $livePath = [HermesHandoff.UpdaterJob]::CanonicalFilePath(
            [System.IO.Path]::GetFullPath(
                [HermesHandoff.UpdaterJob]::RuntimeExecutablePath($targetHandle)
            )
        )
        if (-not [string]::Equals($livePath, $expectedPath, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($livePath, $framePath, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        $retainedHandle = $targetHandle
        $targetHandle = [System.IntPtr]::Zero
        return [pscustomobject]@{
            Pid = $targetPid
            Handle = $retainedHandle
            CreationFileTime = [string]$frame.creation_file_time
            ExecutablePath = $livePath
        }
    } catch {
        return $null
    } finally {
        if ($targetHandle -ne [System.IntPtr]::Zero) {
            [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($targetHandle)
        }
    }
}

function Test-DeferredGatewayAdoptionFrame(
    [string]$Line,
    [int]$ExpectedTargetPid,
    [System.IntPtr]$TargetHandle,
    [string]$TargetCreationFileTime,
    [System.IntPtr]$JobHandle
) {
    try {
        $frame = $Line | ConvertFrom-Json -ErrorAction Stop
        if ($frame -isnot [pscustomobject]) { return $false }
        $expected = @('schema_version', 'event', 'invocation_id', 'owner_pid')
        $names = @($frame.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $false }
        foreach ($name in $expected) { if ($names -notcontains $name) { return $false } }
        if (-not (Test-JsonInteger $frame.schema_version) -or [int64]$frame.schema_version -ne 1 -or
            $frame.event -isnot [string] -or $frame.event -ne 'deferred-gateway-lease-adopted' -or
            $frame.invocation_id -isnot [string] -or
            -not [string]::Equals($frame.invocation_id, $script:InvocationId, [StringComparison]::Ordinal) -or
            -not (Test-JsonInteger $frame.owner_pid) -or [int64]$frame.owner_pid -ne $ExpectedTargetPid -or
            $TargetHandle -eq [System.IntPtr]::Zero -or $JobHandle -eq [System.IntPtr]::Zero -or
            [HermesHandoff.UpdaterJob]::RuntimeProcessId($TargetHandle) -ne $ExpectedTargetPid -or
            -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($JobHandle, $TargetHandle) -or
            -not [string]::Equals(
                [HermesHandoff.UpdaterJob]::RuntimeCreationFileTime($TargetHandle),
                $TargetCreationFileTime,
                [StringComparison]::Ordinal
            )) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function Invoke-DeferredGatewayResume(
    [string]$Exe,
    [string[]]$HermesArgs
) {
    # The mutation child has drained its private kill-on-close Job and returned
    # the same lease to this script before this function is entered. The trusted
    # resume command must run outside that Job so the exact fleet it proves
    # ready can survive. It receives only fixed internal switches plus the
    # already-validated invocation/lease/root identities; no captured argv.
    $initialPlan = Read-DeferredGatewayPlanProof
    if (-not $initialPlan) {
        return @{
            Code = 8
            Output = 'Gateway recovery was not started because its authenticated fleet plan is missing, expired, or malformed.'
            Stdout = ''
            Stderr = 'Gateway recovery was not started because its authenticated fleet plan is missing, expired, or malformed.'
            PendingPlanVerified = $false
        }
    }
    if (-not $script:BridgeLeaseOwned -or
        -not (Refresh-BridgeQuiesceLeaseIfOwned -Force)) {
        return @{
            Code = 8
            Output = 'Gateway recovery was not started because the returned bridge-quiesce lease could not be proved.'
            Stdout = ''
            Stderr = 'Gateway recovery was not started because the returned bridge-quiesce lease could not be proved.'
            PendingPlanVerified = [bool]($initialPlan.Source -eq 'pending')
        }
    }

    $proc = $null
    $startedAtTicks = 0L
    $out = New-Object System.Text.StringBuilder
    $err = New-Object System.Text.StringBuilder
    $transferObserved = $false
    $returnedToScript = $false
    $cleanupObserved = $false
    $adoptionFrameObserved = $false
    $adoptionFrameInvalid = $false
    $targetStartFrameObserved = $false
    $targetStartFrameInvalid = $false
    $stdoutProtocolPhase = 'target-start'
    $leaseLost = $false
    $timedOut = $false
    $jobHandle = [System.IntPtr]::Zero
    $jobAssigned = $false
    $jobMembershipProved = $false
    $containedTreeDrained = $false
    $startupGate = ''
    $targetGate = ''
    $retainedTargetHandle = [System.IntPtr]::Zero
    $retainedTargetPid = 0
    $retainedTargetCreationFileTime = ''
    $recoveryCommitted = $false
    $retainedRuntimeHandles = @()
    $unassignedChildDeathProved = $false
    $diagnosticLinesLogged = 0
    $diagnosticSuppressionLogged = $false
    try {
        # Windows venv python.exe can be a redirector process whose child runs
        # Python. Launch the canonical base interpreter exactly as CPython's
        # multiprocessing module does, so the retained Process handle, adoption
        # frame PID, and lease owner all identify one generation.
        $pythonLaunch = Resolve-ManagedVenvPythonLaunch $Exe
        $wrapperScript = @'
$ErrorActionPreference = 'Stop'
$gate = $env:HERMES_DEFERRED_GATEWAY_STARTUP_GATE
if ([string]::IsNullOrWhiteSpace($gate) -or -not [System.IO.Path]::IsPathRooted($gate)) {
    [Console]::Error.WriteLine('deferred wrapper rejected its startup gate')
    exit 13
}
$testCapture = $env:HERMES_INTERNAL_TEST_DEFERRED_GATEWAY_CONTAINMENT_CAPTURE
if (-not [string]::IsNullOrWhiteSpace($testCapture)) {
    try {
        [System.IO.File]::AppendAllText(
            $testCapture,
            "waiting:${PID}:$([System.Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks)" + [Environment]::NewLine
        )
    } catch {}
}
$deadline = (Get-Date).AddSeconds(15)
$unreadableSince = $null
while ((Get-Date) -lt $deadline) {
    try {
        $stream = [System.IO.File]::Open(
            $gate,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
        )
        try {
            if ($stream.Length -le 0 -or $stream.Length -gt 16) {
                [Console]::Error.WriteLine('deferred wrapper rejected its startup gate')
                exit 13
            }
            $bytes = New-Object byte[] ([int]$stream.Length)
            if ($stream.Read($bytes, 0, $bytes.Length) -ne $bytes.Length) {
                [Console]::Error.WriteLine('deferred wrapper could not read its startup gate')
                exit 13
            }
            $state = [System.Text.Encoding]::UTF8.GetString($bytes)
        } finally {
            $stream.Dispose()
        }
        $unreadableSince = $null
    } catch [System.IO.IOException] {
        if (-not $unreadableSince) { $unreadableSince = Get-Date }
        if ((Get-Date) -ge $deadline -or
            (Get-Date) -ge $unreadableSince.AddMilliseconds(2250)) {
            [Console]::Error.WriteLine('deferred wrapper could not read its startup gate')
            exit 13
        }
        Start-Sleep -Milliseconds 25
        continue
    } catch [System.UnauthorizedAccessException] {
        if (-not $unreadableSince) { $unreadableSince = Get-Date }
        if ((Get-Date) -ge $deadline -or
            (Get-Date) -ge $unreadableSince.AddMilliseconds(2250)) {
            [Console]::Error.WriteLine('deferred wrapper could not read its startup gate')
            exit 13
        }
        Start-Sleep -Milliseconds 25
        continue
    } catch {
        [Console]::Error.WriteLine('deferred wrapper rejected its startup gate')
        exit 13
    }
    if ([string]::Equals($state, 'armed', [StringComparison]::Ordinal)) { break }
    if ([string]::Equals($state, 'abort', [StringComparison]::Ordinal) -and
        $env:HERMES_INTERNAL_TEST_DEFERRED_GATEWAY_IGNORE_ABORT -eq '1') {
        Start-Sleep -Milliseconds 25
        continue
    }
    if (-not [string]::Equals($state, 'wait', [StringComparison]::Ordinal)) {
        [Console]::Error.WriteLine('deferred wrapper rejected its startup gate')
        exit 13
    }
    Start-Sleep -Milliseconds 25
}
if (-not [string]::Equals($state, 'armed', [StringComparison]::Ordinal)) {
    [Console]::Error.WriteLine('deferred wrapper timed out before authorization')
    exit 13
}
try {
    $request = $env:HERMES_INTERNAL_DEFERRED_GATEWAY_REQUEST | ConvertFrom-Json -ErrorAction Stop
    $program = [string]$request.program
    $argumentLine = [string]$request.argument_line
    $targetGate = [string]$request.target_gate
    $targetStartFault = [string]$request.target_start_fault
    $wrapperExitHoldMilliseconds = [int]$request.wrapper_exit_hold_ms
    $requestFields = @($request.PSObject.Properties | ForEach-Object { $_.Name })
    if ($requestFields.Count -ne 5 -or $requestFields -notcontains 'program' -or
        $requestFields -notcontains 'argument_line' -or
        $requestFields -notcontains 'target_gate' -or
        $requestFields -notcontains 'target_start_fault' -or
        $requestFields -notcontains 'wrapper_exit_hold_ms' -or
        [string]::IsNullOrWhiteSpace($program) -or
        -not [System.IO.Path]::IsPathRooted($program) -or
        [string]::IsNullOrWhiteSpace($targetGate) -or
        -not [System.IO.Path]::IsPathRooted($targetGate) -or
        $wrapperExitHoldMilliseconds -lt 0 -or $wrapperExitHoldMilliseconds -gt 5000 -or
        $targetStartFault -notin @('none', 'malformed', 'duplicate', 'wrong-path', 'wrong-filetime', 'stall')) {
        [Console]::Error.WriteLine('deferred wrapper rejected its target request')
        exit 13
    }
    Remove-Item Env:HERMES_INTERNAL_DEFERRED_GATEWAY_REQUEST -ErrorAction SilentlyContinue
    $targetInfo = New-Object System.Diagnostics.ProcessStartInfo
    $targetInfo.FileName = $program
    $targetInfo.Arguments = $argumentLine
    $targetInfo.WorkingDirectory = [Environment]::CurrentDirectory
    $targetInfo.UseShellExecute = $false
    $targetInfo.CreateNoWindow = $true
    $targetInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $targetInfo.RedirectStandardOutput = $true
    $targetInfo.RedirectStandardError = $true
    $targetInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $targetInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $targetInfo.EnvironmentVariables['HERMES_DEFERRED_GATEWAY_STARTUP_GATE'] = $targetGate
    $targetInfo.EnvironmentVariables.Remove('HERMES_INTERNAL_DEFERRED_GATEWAY_REQUEST')
    $targetInfo.EnvironmentVariables.Remove('HERMES_INTERNAL_TEST_DEFERRED_GATEWAY_IGNORE_ABORT')
    $target = [System.Diagnostics.Process]::Start($targetInfo)
    if (-not $target) { exit 13 }
    try {
        $creationFileTime = $target.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        $targetExecutable = [System.IO.Path]::GetFullPath($program)
        if ($targetStartFault -eq 'wrong-path') {
            $targetExecutable = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        } elseif ($targetStartFault -eq 'wrong-filetime') {
            $creationFileTime = (([uint64]$creationFileTime) + [uint64]1).ToString(
                [System.Globalization.CultureInfo]::InvariantCulture
            )
        }
        $targetStartFrame = if ($targetStartFault -eq 'malformed') {
            '{}'
        } else {
            [ordered]@{
                schema_version = 1
                event = 'deferred-gateway-target-started'
                target_pid = [int]$target.Id
                creation_file_time = $creationFileTime
                executable_path = $targetExecutable
            } | ConvertTo-Json -Compress
        }
        if ($targetStartFault -ne 'stall') {
            [Console]::Out.WriteLine($targetStartFrame)
            if ($targetStartFault -eq 'duplicate') {
                [Console]::Out.WriteLine($targetStartFrame)
            }
            [Console]::Out.Flush()
        }

        $targetGateDeadline = (Get-Date).AddSeconds(15)
        $targetGateState = ''
        $targetGateUnreadableSince = $null
        while ((Get-Date) -lt $targetGateDeadline) {
            try {
                $targetGateStream = [System.IO.File]::Open(
                    $targetGate,
                    [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read,
                    ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
                )
                try {
                    if ($targetGateStream.Length -le 0 -or $targetGateStream.Length -gt 16) {
                        throw 'invalid target gate length'
                    }
                    $targetGateBytes = New-Object byte[] ([int]$targetGateStream.Length)
                    if ($targetGateStream.Read($targetGateBytes, 0, $targetGateBytes.Length) -ne
                        $targetGateBytes.Length) {
                        throw 'incomplete target gate read'
                    }
                    $targetGateState = [System.Text.Encoding]::UTF8.GetString($targetGateBytes)
                } finally {
                    $targetGateStream.Dispose()
                }
                $targetGateUnreadableSince = $null
            } catch [System.IO.IOException] {
                if (-not $targetGateUnreadableSince) { $targetGateUnreadableSince = Get-Date }
                if ((Get-Date) -ge $targetGateDeadline -or
                    (Get-Date) -ge $targetGateUnreadableSince.AddMilliseconds(2250)) {
                    $targetGateState = 'invalid'
                } else {
                    Start-Sleep -Milliseconds 25
                    continue
                }
            } catch [System.UnauthorizedAccessException] {
                if (-not $targetGateUnreadableSince) { $targetGateUnreadableSince = Get-Date }
                if ((Get-Date) -ge $targetGateDeadline -or
                    (Get-Date) -ge $targetGateUnreadableSince.AddMilliseconds(2250)) {
                    $targetGateState = 'invalid'
                } else {
                    Start-Sleep -Milliseconds 25
                    continue
                }
            } catch {
                $targetGateState = 'invalid'
            }
            if ([string]::Equals($targetGateState, 'armed', [StringComparison]::Ordinal)) { break }
            if ([string]::Equals($targetGateState, 'abort', [StringComparison]::Ordinal)) {
                try {
                    if (-not $target.HasExited) {
                        $target.Kill()
                        $target.WaitForExit()
                    }
                } catch {}
                exit 13
            }
            if (-not [string]::Equals($targetGateState, 'wait', [StringComparison]::Ordinal)) {
                [Console]::Error.WriteLine('deferred wrapper rejected its target gate')
                exit 13
            }
            Start-Sleep -Milliseconds 25
        }
        if (-not [string]::Equals($targetGateState, 'armed', [StringComparison]::Ordinal)) {
            try {
                if (-not $target.HasExited) {
                    $target.Kill()
                    $target.WaitForExit()
                }
            } catch {}
            [Console]::Error.WriteLine('deferred wrapper timed out before target authorization')
            exit 13
        }

        $targetOutTask = $target.StandardOutput.ReadLineAsync()
        $targetErrTask = $target.StandardError.ReadLineAsync()
        $targetOutClosed = $false
        $targetErrClosed = $false
        while (-not $target.HasExited -or -not $targetOutClosed -or -not $targetErrClosed) {
            if (-not $targetOutClosed -and $targetOutTask.Wait(10)) {
                $line = $targetOutTask.Result
                if ($null -eq $line) {
                    $targetOutClosed = $true
                } else {
                    [Console]::Out.WriteLine($line)
                    [Console]::Out.Flush()
                    $targetOutTask = $target.StandardOutput.ReadLineAsync()
                }
            }
            if (-not $targetErrClosed -and $targetErrTask.Wait(10)) {
                $line = $targetErrTask.Result
                if ($null -eq $line) {
                    $targetErrClosed = $true
                } else {
                    [Console]::Error.WriteLine($line)
                    [Console]::Error.Flush()
                    $targetErrTask = $target.StandardError.ReadLineAsync()
                }
            }
        }
        $target.WaitForExit()
        if ($wrapperExitHoldMilliseconds -gt 0) {
            Start-Sleep -Milliseconds $wrapperExitHoldMilliseconds
        }
        exit [int]$target.ExitCode
    } finally {
        $target.Dispose()
    }
} catch {
    [Console]::Error.WriteLine('deferred wrapper could not execute its target')
    exit 13
}
'@
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $powershellPath = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        if ([string]::IsNullOrWhiteSpace($powershellPath)) {
            throw 'the trusted deferred gateway wrapper executable could not be resolved'
        }
        $encodedWrapper = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($wrapperScript))
        $psi.FileName = $powershellPath
        $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encodedWrapper"
        $psi.WorkingDirectory = $InstallRoot
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
        $psi.EnvironmentVariables['__PYVENV_LAUNCHER__'] = $pythonLaunch.Launcher
        $psi.EnvironmentVariables['HERMES_HOME'] = $HermesHome
        $psi.EnvironmentVariables['HERMES_UPDATE_HANDOFF_PID'] = "$PID"
        $startupGate = New-DeferredGatewayStartupGate
        $targetGate = New-DeferredGatewayStartupGate
        $psi.EnvironmentVariables['HERMES_DEFERRED_GATEWAY_STARTUP_GATE'] = $startupGate
        if ($NoUi -and -not [string]::IsNullOrWhiteSpace($env:HERMES_TEST_RESUME_CONTAINMENT_CAPTURE)) {
            $psi.EnvironmentVariables['HERMES_INTERNAL_TEST_DEFERRED_GATEWAY_CONTAINMENT_CAPTURE'] =
                $env:HERMES_TEST_RESUME_CONTAINMENT_CAPTURE
        }
        if ($NoUi -and $TestDeferredGatewayInitialKillFailure) {
            $psi.EnvironmentVariables['HERMES_INTERNAL_TEST_DEFERRED_GATEWAY_IGNORE_ABORT'] = '1'
        }
        if ($NoUi -and -not [string]::IsNullOrWhiteSpace($env:HERMES_TEST_RESUME_GATE_CAPTURE)) {
            [System.IO.File]::WriteAllText(
                $env:HERMES_TEST_RESUME_GATE_CAPTURE,
                $startupGate,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        $quotedHermesArgs = @($HermesArgs | ForEach-Object {
            [HermesHandoff.UpdaterJob]::QuoteArgument([string]$_)
        })
        $request = [ordered]@{
            program = $pythonLaunch.Executable
            argument_line = [string]::Join(' ', $quotedHermesArgs)
            target_gate = $targetGate
            target_start_fault = $TestDeferredGatewayTargetStartFault
            wrapper_exit_hold_ms = $TestDeferredGatewayWrapperExitHoldMilliseconds
        } | ConvertTo-Json -Compress -Depth 2
        $psi.EnvironmentVariables['HERMES_INTERNAL_DEFERRED_GATEWAY_REQUEST'] = $request
        $jobHandle = [HermesHandoff.UpdaterJob]::CreateKillOnClose()
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc) { throw 'the trusted deferred gateway resume child did not start' }
        $startedAtTicks = $proc.StartTime.ToUniversalTime().Ticks
        if ($TestDeferredGatewayPreAssignHoldMilliseconds -gt 0) {
            Start-Sleep -Milliseconds $TestDeferredGatewayPreAssignHoldMilliseconds
        }
        if ($TestDeferredGatewayAssignFailure) {
            throw 'the deferred gateway resume child could not be assigned to containment'
        }
        [HermesHandoff.UpdaterJob]::Assign($jobHandle, $proc.Handle)
        $jobAssigned = $true
        if (-not [HermesHandoff.UpdaterJob]::ContainsPid($jobHandle, $proc.Id)) {
            throw 'the deferred gateway resume child Job membership could not be proved'
        }
        $jobMembershipProved = $true
        Set-DeferredGatewayStartupGateStateExact $startupGate 'wait' 'armed'

        $outTask = $proc.StandardOutput.ReadLineAsync()
        $errTask = $proc.StandardError.ReadLineAsync()
        $outClosed = $false
        $errClosed = $false
        $nextLeaseRefresh = (Get-Date).AddSeconds(30)
        $deadline = (Get-Date).AddSeconds((Get-ManagedStageTimeoutSeconds 'gateway-resume'))
        $missingSince = $null
        $leaseProofGap = $null
        $rootExitedAt = $null
        while (-not $proc.HasExited -or -not $outClosed -or -not $errClosed) {
            if ((Get-Date) -ge $deadline) {
                $timedOut = $true
                Write-HandoffLog 'trusted gateway recovery exceeded its bounded deadline; stopping its exact process tree'
                Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
            }
            if ($proc.HasExited) {
                if (-not $rootExitedAt) { $rootExitedAt = Get-Date }
                if ((-not $outClosed -or -not $errClosed) -and
                    (Get-Date) -ge $rootExitedAt.AddSeconds($script:OuterJobDrainSeconds)) {
                    $timedOut = $true
                    Write-HandoffLog 'trusted gateway recovery exited but inherited streams did not close within the bounded drain window'
                    try { $proc.StandardOutput.Close() } catch {}
                    try { $proc.StandardError.Close() } catch {}
                    $outClosed = $true
                    $errClosed = $true
                }
            }

            $tasks = @()
            if (-not $outClosed) { $tasks += $outTask }
            if (-not $errClosed) { $tasks += $errTask }
            if ($tasks.Count -gt 0) {
                $completed = [System.Threading.Tasks.Task]::WaitAny(
                    [System.Threading.Tasks.Task[]]$tasks,
                    100
                )
                if ($completed -ge 0) {
                    $task = $tasks[$completed]
                    if ([object]::ReferenceEquals($task, $outTask)) {
                        $line = $outTask.Result
                        if ($null -eq $line) { $outClosed = $true }
                        else {
                            if ($line.Trim()) {
                                $acceptedProtocolFrame = ''
                                $isTargetStartFrame = $line -match 'deferred-gateway-target-started'
                                $isAdoptionFrame = $line -match 'deferred-gateway-lease-adopted'
                                if ($stdoutProtocolPhase -eq 'target-start') {
                                    if ($TestDeferredGatewayTargetRetentionHoldMilliseconds -gt 0) {
                                        Start-Sleep -Milliseconds $TestDeferredGatewayTargetRetentionHoldMilliseconds
                                    }
                                    $targetIdentity = Open-DeferredGatewayTargetStartFrame `
                                        $line $pythonLaunch.Executable $jobHandle
                                    if ($targetIdentity) {
                                        $retainedTargetHandle = $targetIdentity.Handle
                                        $retainedTargetPid = [int]$targetIdentity.Pid
                                        $retainedTargetCreationFileTime = [string]$targetIdentity.CreationFileTime
                                        try {
                                            Set-DeferredGatewayStartupGateStateExact $targetGate 'wait' 'armed'
                                            if ($TestDeferredGatewayPostTargetGateHoldMilliseconds -gt 0) {
                                                Start-Sleep -Milliseconds $TestDeferredGatewayPostTargetGateHoldMilliseconds
                                            }
                                            $targetStartFrameObserved = $true
                                            $stdoutProtocolPhase = 'adoption'
                                            $acceptedProtocolFrame = 'target-start'
                                        } catch {
                                            [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($retainedTargetHandle)
                                            $retainedTargetHandle = [System.IntPtr]::Zero
                                            $retainedTargetPid = 0
                                            $retainedTargetCreationFileTime = ''
                                            $targetStartFrameInvalid = $true
                                            $stdoutProtocolPhase = 'failed'
                                        }
                                    } else {
                                        $targetStartFrameInvalid = $true
                                        $stdoutProtocolPhase = 'failed'
                                    }
                                    if ($targetStartFrameInvalid) {
                                        try {
                                            Set-DeferredGatewayStartupGateStateExact $targetGate 'wait' 'abort'
                                        } catch {}
                                    }
                                } elseif ($stdoutProtocolPhase -eq 'adoption') {
                                    if ($isTargetStartFrame) {
                                        $targetStartFrameInvalid = $true
                                        $adoptionFrameInvalid = $true
                                        $stdoutProtocolPhase = 'failed'
                                    } elseif ($isAdoptionFrame) {
                                        if (Test-DeferredGatewayAdoptionFrame `
                                            $line $retainedTargetPid $retainedTargetHandle `
                                            $retainedTargetCreationFileTime $jobHandle) {
                                            $adoptionFrameObserved = $true
                                            $transferObserved = $true
                                            $script:BridgeLeaseOwned = $false
                                            $script:BridgeLeaseTransferredPid = $retainedTargetPid
                                            $stdoutProtocolPhase = 'diagnostic'
                                            $acceptedProtocolFrame = 'adoption'
                                        } else {
                                            $adoptionFrameInvalid = $true
                                            $stdoutProtocolPhase = 'failed'
                                        }
                                    } else {
                                        if ($initialPlan.Source -eq 'pending') {
                                            $adoptionFrameInvalid = $true
                                            $stdoutProtocolPhase = 'failed'
                                        } else {
                                            # A completed replay can legitimately
                                            # produce no adoption frame because it
                                            # no longer owns a transferable lease.
                                            $stdoutProtocolPhase = 'diagnostic'
                                        }
                                    }
                                } elseif ($isTargetStartFrame -or $isAdoptionFrame) {
                                    if ($isTargetStartFrame) { $targetStartFrameInvalid = $true }
                                    if ($isAdoptionFrame) { $adoptionFrameInvalid = $true }
                                    $stdoutProtocolPhase = 'failed'
                                }
                                $safeDiagnostic = if ($acceptedProtocolFrame -eq 'target-start') {
                                    'target start frame accepted'
                                } elseif ($acceptedProtocolFrame -eq 'adoption') {
                                    'adoption frame accepted'
                                } elseif ($isTargetStartFrame -or $isAdoptionFrame -or
                                    $targetStartFrameInvalid -or $adoptionFrameInvalid) {
                                    'unexpected recovery protocol frame rejected'
                                } else {
                                    $diagnostic = ConvertTo-DeferredGatewayDiagnostic $line $startupGate 'stdout'
                                    Protect-DeferredGatewayDiagnosticDetail $diagnostic $targetGate
                                }
                                Add-BoundedDeferredGatewayOutput $out $safeDiagnostic
                                Write-BoundedDeferredGatewayDiagnostic 'gateway-resume|' $safeDiagnostic `
                                    ([ref]$diagnosticLinesLogged) ([ref]$diagnosticSuppressionLogged)
                                if ($targetStartFrameInvalid -or $adoptionFrameInvalid) {
                                    $leaseLost = $true
                                    if ($adoptionFrameInvalid -and $jobAssigned) {
                                        Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                                        $containedTreeDrained = $true
                                    }
                                }
                            }
                            $outTask = $proc.StandardOutput.ReadLineAsync()
                        }
                    } else {
                        $line = $errTask.Result
                        if ($null -eq $line) { $errClosed = $true }
                        else {
                            if ($line.Trim()) {
                                # stdout and stderr are independent OS pipes;
                                # WaitAny cannot infer write order between them.
                                # Authorization still requires the exact first
                                # nonempty stdout frame, so stderr is diagnostic
                                # only and cannot authorize adoption.
                                $safeDiagnostic = ConvertTo-DeferredGatewayDiagnostic $line $startupGate 'stderr'
                                $safeDiagnostic = Protect-DeferredGatewayDiagnosticDetail $safeDiagnostic $targetGate
                                Add-BoundedDeferredGatewayOutput $err $safeDiagnostic
                                Write-BoundedDeferredGatewayDiagnostic 'gateway-resume!|' $safeDiagnostic `
                                    ([ref]$diagnosticLinesLogged) ([ref]$diagnosticSuppressionLogged)
                            }
                            $errTask = $proc.StandardError.ReadLineAsync()
                        }
                    }
                }
            } else {
                Start-Sleep -Milliseconds 100
            }

            if (-not $leaseLost -and $targetStartFrameObserved) {
                $leaseState = Get-RetainedTargetBridgeLeaseOwnerState `
                    $retainedTargetPid $retainedTargetHandle `
                    $retainedTargetCreationFileTime $jobHandle
                $targetStillLive = $true
                try {
                    $targetStillLive = [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive(
                        $retainedTargetHandle
                    )
                } catch {
                    # Unknown is not absence. Keep the exact target classified
                    # as live so the bounded failure path retains containment.
                    $targetStillLive = $true
                }
                if ($leaseState -eq 'child') {
                    $transferObserved = $true
                    $script:BridgeLeaseOwned = $false
                    $missingSince = $null
                    $leaseProofGap = $null
                } elseif ($leaseState -eq 'script') {
                    if ($transferObserved) {
                        $returnedToScript = $true
                        $script:BridgeLeaseOwned = $true
                    } elseif ((Get-Date) -ge $nextLeaseRefresh) {
                        if (-not (Refresh-BridgeQuiesceLeaseIfOwned)) { $leaseLost = $true }
                        $nextLeaseRefresh = (Get-Date).AddSeconds(30)
                    }
                    $missingSince = $null
                    $leaseProofGap = $null
                } elseif ($leaseState -eq 'missing' -and $transferObserved) {
                    $cleanupObserved = $true
                    $leaseProofGap = $null
                    if (-not $missingSince) { $missingSince = Get-Date }
                    # Successful resume clears its exact lease immediately
                    # before exit. A still-running child without the lease for
                    # more than the bounded edge is no longer authorized.
                    if ($targetStillLive -and
                        (Get-Date) -ge $missingSince.AddMilliseconds(750)) {
                        $leaseLost = $true
                    }
                } elseif (($leaseState -eq 'missing' -or $leaseState -eq 'unreadable') -and
                    -not $transferObserved) {
                    # After the exact target is retained and armed, it can adopt
                    # and clear the lease before this parent consumes the already-
                    # buffered adoption frame. Unknown state is never success, but
                    # give that frame one bounded monotonic proof window. Engine
                    # scheduling must not turn three fast polls into a false loss.
                    if (-not $leaseProofGap) {
                        $leaseProofGap = [System.Diagnostics.Stopwatch]::StartNew()
                    }
                    if ($targetStillLive -and $leaseProofGap.ElapsedMilliseconds -ge 750) {
                        $leaseLost = $true
                    }
                } elseif ($leaseState -eq 'unreadable' -and $targetStillLive) {
                    if (-not $leaseProofGap) {
                        $leaseProofGap = [System.Diagnostics.Stopwatch]::StartNew()
                    }
                    if ($leaseProofGap.ElapsedMilliseconds -ge 750) { $leaseLost = $true }
                } else {
                    $leaseLost = $true
                }
                if ($leaseLost) {
                    Write-HandoffLog 'trusted gateway recovery lost its bridge-quiesce lease; stopping its exact process tree'
                    Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                }
            }
            if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
        }

        $proc.WaitForExit()
        $code = [int]$proc.ExitCode
        $finalLeaseState = if ($targetStartFrameObserved) {
            Get-RetainedTargetBridgeLeaseOwnerState `
                $retainedTargetPid $retainedTargetHandle `
                $retainedTargetCreationFileTime $jobHandle
        } else { 'unreadable' }
        if ($code -eq 0) {
            $nativeCommitProof = $null
            if ($initialPlan.Source -eq 'completed') {
                $terminalPlan = Read-DeferredGatewayPlanProof
                $planStateProved = $terminalPlan -and
                    $terminalPlan.Source -eq 'completed' -and
                    [string]::Equals($terminalPlan.Raw, $initialPlan.Raw, [StringComparison]::Ordinal) -and
                    -not (Test-Path -LiteralPath $script:GatewayPlanPath) -and
                    -not (Test-Path -LiteralPath $script:GatewayPlanPreparedPath) -and
                    -not (Test-Path -LiteralPath $script:GatewayRuntimeManifestPath) -and
                    @(Get-DeferredGatewayConsumeArtifacts).Count -eq 0 -and
                    [HermesHandoff.UpdaterJob]::ActiveProcesses($jobHandle) -eq 0
            } else {
                $nativeCommitProof = Read-PreparedGatewayNativeCommitProof $initialPlan.Raw $jobHandle
                $planStateProved = $null -ne $nativeCommitProof
                if ($planStateProved) {
                    $retainedRuntimeHandles = @($nativeCommitProof.RuntimeHandles) +
                        @($nativeCommitProof.LauncherHandles)
                }
                if (-not $planStateProved) {
                    $proofState = if ($script:PreparedGatewayProofState -match '^[a-z-]{1,40}$') {
                        $script:PreparedGatewayProofState
                    } else { 'invalid' }
                    Write-HandoffLog ("gateway-resume!|state=prepared-proof-{0}" -f $proofState)
                }
            }
            $adoptionProved = if ($initialPlan.Source -eq 'completed') {
                # Idempotent replay starts with the exact authenticated
                # completed bytes. With an active returned lease Python emits
                # the frame; a fully completed replay with no lease does not.
                $targetStartFrameObserved -and -not $targetStartFrameInvalid -and
                    -not $adoptionFrameInvalid
            } else {
                $targetStartFrameObserved -and -not $targetStartFrameInvalid -and
                    $adoptionFrameObserved -and -not $adoptionFrameInvalid
            }
            $cleanupProved = $adoptionProved -and $planStateProved -and
                ($cleanupObserved -or $finalLeaseState -eq 'missing') -and
                -not (Test-Path -LiteralPath $BridgeLeasePath) -and
                @(Get-HandoffCasArtifacts $BridgeLeasePath).Count -eq 0
            if (-not $cleanupProved) {
                $code = 8
                $leaseLost = $true
                if ($initialPlan.Source -ne 'completed' -and $jobAssigned) {
                    Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                    $containedTreeDrained = $true
                    if (-not (Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure `
                        $initialPlan.Raw)) {
                        Write-HandoffLog 'gateway-resume!|state=prepared-recovery-artifact-retained'
                    }
                }
            } else {
                $nativeBoundaryProved = $true
                if ($initialPlan.Source -ne 'completed') {
                    if ($retainedRuntimeHandles.Count -ne
                        (@($nativeCommitProof.Runtimes).Count + @($nativeCommitProof.Launchers).Count)) {
                        $nativeBoundaryProved = $false
                    }
                    foreach ($runtimeHandle in $retainedRuntimeHandles) {
                        if (-not $nativeBoundaryProved) { break }
                        try {
                            if (-not [HermesHandoff.UpdaterJob]::RuntimeProcessIsLive($runtimeHandle) -or
                                -not [HermesHandoff.UpdaterJob]::ContainsProcessHandle($jobHandle, $runtimeHandle)) {
                                $nativeBoundaryProved = $false
                            }
                        } catch {
                            $nativeBoundaryProved = $false
                        }
                    }
                    try {
                        if ([HermesHandoff.UpdaterJob]::ActiveProcesses($jobHandle) -ne
                            [uint32]$retainedRuntimeHandles.Count) {
                            $nativeBoundaryProved = $false
                        }
                    } catch {
                        $nativeBoundaryProved = $false
                    }
                    if ($TestDeferredGatewayNativeBoundaryFailure) {
                        $nativeBoundaryProved = $false
                    }
                }
                if (-not $nativeBoundaryProved) {
                    Write-HandoffLog 'gateway-resume!|state=native-boundary-changed'
                    $code = 8
                    $leaseLost = $true
                    Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                    $containedTreeDrained = $true
                    if (-not (Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure `
                        $initialPlan.Raw)) {
                        Write-HandoffLog 'gateway-resume!|state=prepared-recovery-artifact-retained'
                    }
                } else {
                # The trusted Python child may only prepare the durable plan.
                # Validate every surviving Job member first, then disarm the
                # private Job, and only then publish the exact completed plan.
                # If publication fails after disarm, the still-open Job handle
                # retains explicit TerminateJob authority and the attempt is
                # drained before this function can return.
                [HermesHandoff.UpdaterJob]::DisarmKillOnClose($jobHandle)
                if ($initialPlan.Source -eq 'completed' -or
                    (Commit-PreparedGatewayPlanExact `
                        $nativeCommitProof.PreparedRaw $nativeCommitProof.ManifestRaw)) {
                    $script:BridgeLeaseOwned = $false
                    $script:BridgeLeaseRequired = $false
                    $script:BridgeLeaseTransferredPid = 0
                    $recoveryCommitted = $true
                } else {
                    $code = 8
                    $leaseLost = $true
                    Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                    $containedTreeDrained = $true
                    if (-not (Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure `
                        $initialPlan.Raw)) {
                        Write-HandoffLog 'gateway-resume!|state=prepared-recovery-artifact-retained'
                    }
                }
                }
            }
        } else {
            if ($initialPlan.Source -ne 'completed' -and $jobAssigned) {
                Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                $containedTreeDrained = $true
                if (-not (Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure `
                    $initialPlan.Raw)) {
                    Write-HandoffLog 'gateway-resume!|state=prepared-recovery-artifact-retained'
                }
            }
            # A failed recovery command must return the same lease to this live
            # parent. This preserves continuous quiesce while the original
            # update failure is surfaced and terminal cleanup runs.
            if ($transferObserved -and ($returnedToScript -or $finalLeaseState -eq 'script')) {
                $script:BridgeLeaseOwned = $true
                if (-not (Refresh-BridgeQuiesceLeaseIfOwned -Force)) {
                    $code = 8
                    $leaseLost = $true
                }
            } else {
                $code = 8
                $leaseLost = $true
            }
        }
        if ($timedOut -or $leaseLost) { $code = 8 }
        $stdoutText = $out.ToString()
        $stderrText = $err.ToString()
        return @{
            Code = $code
            Output = $stdoutText + $stderrText
            Stdout = $stdoutText
            Stderr = $stderrText
            PendingPlanVerified = [bool]($initialPlan.Source -eq 'pending')
        }
    } catch {
        Write-HandoffLog ("trusted gateway recovery containment failed closed ({0})" -f $_.Exception.GetType().Name)
        if ($proc) {
            if ($jobAssigned) {
                Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                $containedTreeDrained = $true
            } else {
                $unassignedChildDeathProved = Stop-UnassignedDeferredGatewayResumeAndProve `
                    $proc $startedAtTicks $startupGate -SkipInitialKill:$TestDeferredGatewayInitialKillFailure
                if (-not $unassignedChildDeathProved) {
                    # Never release the startup gate while this exact
                    # uncontained generation may still execute.
                    while ($true) {
                        try {
                            if ($proc.HasExited) {
                                $unassignedChildDeathProved = $true
                                break
                            }
                        } catch {}
                        Start-Sleep -Milliseconds 50
                    }
                }
            }
        } else {
            $unassignedChildDeathProved = $true
        }
        if ($initialPlan.Source -ne 'completed' -and $jobAssigned) {
            if (-not (Restore-AuthenticatedPreparedGatewayAttemptAfterNativeFailure `
                $initialPlan.Raw)) {
                Write-HandoffLog 'gateway-resume!|state=prepared-recovery-artifact-retained'
            }
        }
        return @{
            Code = 8
            Output = 'The trusted deferred gateway recovery process could not be monitored safely.'
            Stdout = ''
            Stderr = 'The trusted deferred gateway recovery process could not be monitored safely.'
            PendingPlanVerified = [bool]($initialPlan.Source -eq 'pending')
        }
    } finally {
        if ($targetGate -and -not $recoveryCommitted) {
            try {
                $expectedTargetGateState = if ($targetStartFrameObserved) { 'armed' } else { 'wait' }
                Set-DeferredGatewayStartupGateStateExact `
                    $targetGate $expectedTargetGateState 'abort'
            } catch {}
        }
        if ($jobHandle -ne [System.IntPtr]::Zero) {
            try {
                if ($recoveryCommitted) {
                    # Survivor validation, disarm, and exact prepared-to-
                    # completed publication already succeeded above.
                } elseif ($jobAssigned -and
                    [HermesHandoff.UpdaterJob]::ActiveProcesses($jobHandle) -ne 0) {
                    Stop-ExactSpawnedProcessTree $proc $startedAtTicks $jobHandle
                    $containedTreeDrained = $true
                } elseif ($jobAssigned) {
                    $containedTreeDrained = $true
                }
            } finally {
                foreach ($runtimeHandle in $retainedRuntimeHandles) {
                    [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($runtimeHandle)
                }
                $retainedRuntimeHandles = @()
                if ($retainedTargetHandle -ne [System.IntPtr]::Zero) {
                    [HermesHandoff.UpdaterJob]::CloseRuntimeProcess($retainedTargetHandle)
                    $retainedTargetHandle = [System.IntPtr]::Zero
                }
                [HermesHandoff.UpdaterJob]::Close($jobHandle)
            }
        }
        $gateCleanupSafe = $jobMembershipProved -or $containedTreeDrained -or
            $unassignedChildDeathProved -or -not $proc
        if ($startupGate -and $gateCleanupSafe) {
            Remove-DeferredGatewayStartupGateExact $startupGate | Out-Null
        }
        if ($targetGate -and $gateCleanupSafe) {
            Remove-DeferredGatewayStartupGateExact $targetGate | Out-Null
        }
        if ($proc) { try { $proc.Dispose() } catch {} }
    }
}

function Test-GitBranchName([object]$Value) {
    if ($Value -isnot [string] -or [string]::IsNullOrEmpty($Value) -or
        $Value.Length -gt 255 -or $Value -eq '@' -or
        $Value.StartsWith('-') -or $Value.StartsWith('/') -or
        $Value.EndsWith('/') -or $Value.EndsWith('.') -or
        $Value.Contains('//') -or $Value.Contains('..') -or
        $Value.Contains('@{') -or $Value -match '[\x00-\x20\x7f~^:?*\[\\]') {
        return $false
    }
    foreach ($component in @($Value -split '/')) {
        if ([string]::IsNullOrEmpty($component) -or $component.StartsWith('.') -or
            $component.EndsWith('.') -or $component.EndsWith('.lock')) {
            return $false
        }
    }
    return $true
}

function Test-TransactionalGitPreflight([object]$Git) {
    try {
        if ($Git -isnot [pscustomobject]) { return $false }
        $expected = @(
            'head', 'branch', 'dirty', 'tracking_remote',
            'target_branch', 'target_ref', 'target_sha'
        )
        $names = @($Git.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $false }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $false }
        }
        if ($Git.head -isnot [string] -or $Git.head -notmatch '^[0-9a-fA-F]{40}$' -or
            -not (Test-GitBranchName $Git.branch) -or
            $Git.dirty -isnot [bool] -or $Git.dirty -ne $false -or
            $Git.tracking_remote -isnot [string] -or
            $Git.tracking_remote -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
            $Git.target_branch -isnot [string] -or $Git.target_branch -ne $Branch -or
            -not (Test-GitBranchName $Git.target_branch) -or
            $Git.target_ref -isnot [string] -or
            $Git.target_ref -ne "refs/remotes/$($Git.tracking_remote)/$Branch" -or
            $Git.target_sha -isnot [string] -or
            $Git.target_sha -notmatch '^[0-9a-fA-F]{40}$') {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function Invoke-DesktopActivationAction([string]$Action) {
    if ($Action -notin @('activate', 'publish-receipt', 'rollback', 'rollback-source', 'commit') -or
        -not $script:ActivationBasePython -or
        -not (Test-Path -LiteralPath $script:ActivationBasePython -PathType Leaf)) {
        Write-HandoffLog 'transactional activation action was unavailable'
        return $false
    }
    $names = @(
        'HERMES_INTERNAL_DESKTOP_UPDATE_ROOT',
        'HERMES_INTERNAL_DESKTOP_UPDATE_HOME',
        'HERMES_INTERNAL_DESKTOP_UPDATE_INVOCATION',
        'HERMES_INTERNAL_DESKTOP_UPDATE_LEASE',
        'HERMES_INTERNAL_DESKTOP_UPDATE_PRE_HEAD'
    )
    $prior = @{}
    foreach ($name in $names) {
        $prior[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        [Environment]::SetEnvironmentVariable('HERMES_INTERNAL_DESKTOP_UPDATE_ROOT', $InstallRoot, 'Process')
        [Environment]::SetEnvironmentVariable('HERMES_INTERNAL_DESKTOP_UPDATE_HOME', $HermesHome, 'Process')
        [Environment]::SetEnvironmentVariable('HERMES_INTERNAL_DESKTOP_UPDATE_INVOCATION', $script:InvocationId, 'Process')
        [Environment]::SetEnvironmentVariable('HERMES_INTERNAL_DESKTOP_UPDATE_LEASE', $BridgeLeaseId, 'Process')
        [Environment]::SetEnvironmentVariable('HERMES_INTERNAL_DESKTOP_UPDATE_PRE_HEAD', $script:PreUpdateGitHead, 'Process')
        $activationArgs = @('-B', '-m', 'hermes_cli.desktop_update_activation', $Action)
        $activation = Invoke-StreamedHermes $script:ActivationBasePython $activationArgs 'activation' -SkipBridgeLeaseCheck
        if ($activation.Code -eq 0) {
            Write-HandoffLog "transactional activation action completed: $Action"
            return $true
        }
        Write-HandoffLog "transactional activation action failed: $Action"
        return $false
    } finally {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $prior[$name], 'Process')
        }
    }
}

function Restore-DesktopActivationBeforeRelaunch([switch]$RelaunchExitProved) {
    if (-not $script:ActivationStarted) { return $true }
    if ($script:RelaunchStarted -and -not $RelaunchExitProved) { return $false }
    if (-not (Invoke-DesktopActivationAction 'rollback')) { return $false }
    $script:ActivationStarted = $false
    $script:ActivationReceiptPublished = $false
    $script:VerifiedUpdateReceipt = $null
    Write-HandoffLog 'terminal failure restored the previous installation before relaunch'
    return $true
}

function Test-DesktopBuildStamp([string]$ExpectedBuildId) {
    try {
        $stampPaths = @(
            (Join-Path $InstallRoot 'apps\desktop\build\install-stamp.json'),
            (Join-Path $InstallRoot 'apps\desktop\release\win-unpacked\resources\install-stamp.json')
        )
        foreach ($stampPath in $stampPaths) {
            $raw = Read-BoundedUtf8Text $stampPath 65536
            $stamp = ConvertFrom-DesktopStampJson $raw
            if ($stamp -isnot [pscustomobject]) { return $false }
            $expected = @('schemaVersion', 'commit', 'branch', 'builtAt', 'dirty', 'source')
            $names = @($stamp.PSObject.Properties | ForEach-Object { $_.Name })
            if ($names.Count -ne $expected.Count) { return $false }
            foreach ($name in $expected) {
                if ($names -notcontains $name) { return $false }
            }
            $builtAt = [DateTimeOffset]::MinValue
            if (-not (Test-JsonInteger $stamp.schemaVersion) -or
                [int64]$stamp.schemaVersion -ne 1 -or
                $stamp.commit -isnot [string] -or
                -not [string]::Equals($stamp.commit, $ExpectedBuildId, [StringComparison]::OrdinalIgnoreCase) -or
                $stamp.branch -isnot [string] -or $stamp.branch -ne $Branch -or
                $stamp.builtAt -isnot [string] -or $stamp.builtAt.Length -gt 64 -or
                -not [DateTimeOffset]::TryParseExact(
                    $stamp.builtAt,
                    'yyyy-MM-ddTHH:mm:ss.fffZ',
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::AssumeUniversal,
                    [ref]$builtAt
                ) -or
                $stamp.dirty -isnot [bool] -or $stamp.dirty -ne $false -or
                $stamp.source -isnot [string] -or $stamp.source -ne 'ci') {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Publish-DesktopHealthProof([string]$ExpectedBuildId) {
    try {
        if (-not (Test-DesktopBuildStamp $ExpectedBuildId)) { return $false }
        $canonicalRoot = Get-CanonicalInstallRoot $InstallRoot
        if (-not $canonicalRoot) { return $false }
        $proof = [ordered]@{
            schema_version = 1
            invocation_id = $script:InvocationId
            lease_id = $BridgeLeaseId
            root = $canonicalRoot
            target_head = $ExpectedBuildId.ToLowerInvariant()
            branch = $Branch
            build_exit_code = 0
            node_dependencies = $true
            desktop_rebuild = $true
            created_at = [int64](Get-UnixTimeSeconds)
        } | ConvertTo-Json -Compress
        if ([string]::IsNullOrWhiteSpace($proof) -or
            [Text.Encoding]::UTF8.GetByteCount($proof) -gt 65536) {
            return $false
        }
        Publish-HandoffFileNoGap $DesktopHealthPath $null $proof 'Desktop health proof'
        $published = Read-BoundedUtf8Text $DesktopHealthPath 65536
        return [string]::Equals($published, $proof, [StringComparison]::Ordinal)
    } catch {
        return $false
    }
}

function Read-ActivationTargetHead {
    try {
        $raw = Read-BoundedUtf8Text $ActivationManifestPath 262144
        $manifest = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($manifest -isnot [pscustomobject]) { return $null }
        $expected = @(
            'schema_version', 'invocation_id', 'lease_id', 'root',
            'candidate_rel', 'provisioned_generation', 'pre_update_head',
            'pre_update_branch', 'selected_pre_head', 'target_head', 'branch',
            'remote', 'target_ref', 'prior_receipt_sha256',
            'python_health', 'created_at'
        )
        $names = @($manifest.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $null }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $null }
        }
        $canonicalRoot = Get-CanonicalInstallRoot $InstallRoot
        $manifestRoot = if ($manifest.root -is [string]) {
            Get-CanonicalInstallRoot $manifest.root
        } else { $null }
        if (-not (Test-JsonInteger $manifest.schema_version) -or
            [int64]$manifest.schema_version -ne 2 -or
            $manifest.invocation_id -isnot [string] -or
            -not [string]::Equals($manifest.invocation_id, $script:InvocationId, [StringComparison]::Ordinal) -or
            $manifest.lease_id -isnot [string] -or
            -not [string]::Equals($manifest.lease_id, $BridgeLeaseId, [StringComparison]::Ordinal) -or
            -not $canonicalRoot -or -not $manifestRoot -or
            -not [string]::Equals($manifestRoot, $canonicalRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $manifest.candidate_rel -isnot [string] -or
            $manifest.candidate_rel -notmatch '^\.hermes-runtime/venv-candidate-[A-Za-z0-9._-]{8,96}$' -or
            $manifest.pre_update_head -isnot [string] -or
            -not [string]::Equals($manifest.pre_update_head, $script:PreUpdateGitHead, [StringComparison]::OrdinalIgnoreCase) -or
            $manifest.pre_update_branch -isnot [string] -or
            -not [string]::Equals($manifest.pre_update_branch, $script:PreUpdateGitBranch, [StringComparison]::Ordinal) -or
            -not (Test-GitBranchName $manifest.pre_update_branch) -or
            ($null -ne $manifest.selected_pre_head -and
                ($manifest.selected_pre_head -isnot [string] -or
                 $manifest.selected_pre_head -notmatch '^[0-9a-fA-F]{40}$')) -or
            $manifest.target_head -isnot [string] -or
            $manifest.target_head -notmatch '^[0-9a-fA-F]{40}$' -or
            -not [string]::Equals($manifest.target_head, $script:PreflightTargetGitHead, [StringComparison]::OrdinalIgnoreCase) -or
            $manifest.branch -isnot [string] -or $manifest.branch -ne $Branch -or
            $manifest.remote -isnot [string] -or
            $manifest.remote -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
            -not [string]::Equals($manifest.remote, $script:PreflightTargetGitRemote, [StringComparison]::Ordinal) -or
            $manifest.target_ref -isnot [string] -or
            $manifest.target_ref -ne "refs/remotes/$($manifest.remote)/$Branch" -or
            -not [string]::Equals($manifest.target_ref, $script:PreflightTargetGitRef, [StringComparison]::Ordinal) -or
            ($null -ne $manifest.prior_receipt_sha256 -and
                ($manifest.prior_receipt_sha256 -isnot [string] -or
                 $manifest.prior_receipt_sha256 -notmatch '^[0-9a-f]{64}$')) -or
            $manifest.python_health -isnot [pscustomobject] -or
            -not (Test-JsonInteger $manifest.created_at) -or
            [int64]$manifest.created_at -le 0) {
            return $null
        }
        if ($null -ne $manifest.provisioned_generation) {
            if ($manifest.provisioned_generation -isnot [pscustomobject]) { return $null }
            $generationNames = @($manifest.provisioned_generation.PSObject.Properties | ForEach-Object { $_.Name })
            if ($generationNames.Count -ne 2 -or
                $generationNames -notcontains 'rel' -or
                $generationNames -notcontains 'identity_sha256' -or
                $manifest.provisioned_generation.rel -isnot [string] -or
                $manifest.provisioned_generation.rel -notmatch '^\.hermes-runtime/python/generation-[A-Za-z0-9._-]{8,96}$' -or
                $manifest.provisioned_generation.identity_sha256 -isnot [string] -or
                $manifest.provisioned_generation.identity_sha256 -notmatch '^[0-9a-f]{64}$') {
                return $null
            }
        }
        $healthNames = @($manifest.python_health.PSObject.Properties | ForEach-Object { $_.Name })
        $expectedHealth = @('critical_imports', 'critical_syntax', 'dependencies')
        if ($healthNames.Count -ne $expectedHealth.Count) { return $null }
        foreach ($name in $expectedHealth) {
            if ($healthNames -notcontains $name -or
                $manifest.python_health.$name -isnot [bool] -or
                $manifest.python_health.$name -ne $true) {
                return $null
            }
        }
        return [string]$manifest.target_head
    } catch {
        return $null
    }
}

function Test-UpdatePreflightUnavailable([object]$Result) {
    if ($Result.Code -ne 2 -or -not [string]::IsNullOrWhiteSpace([string]$Result.Stdout)) {
        return $false
    }
    # Distinguish an older checkout only so the result can explain the repair
    # path. It still aborts: no unsupported/malformed response is clear enough
    # to authorize venv or source mutation.
    return [bool]([string]$Result.Stderr -match '(?im)unrecognized arguments?:[^\r\n]*(^|\s)--preflight(\s|$)')
}

function Test-JsonInteger([object]$Value) {
    return $Value -is [int] -or $Value -is [long]
}

function Read-UpdatePreflightPayload([object]$Result) {
    try {
        $payload = ([string]$Result.Stdout).Trim() | ConvertFrom-Json -ErrorAction Stop
        if ($payload -isnot [pscustomobject]) { return $null }
        $expected = @(
            'schema_version', 'mode', 'ok', 'ready', 'blocked', 'reason', 'root', 'venv',
            'processes', 'mcp_bridges', 'pausable_gateways',
            'pausable_gateway_processes', 'git', 'last_update_receipt',
            'lease', 'actions', 'error'
        )
        $names = @($payload.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $null }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $null }
        }
        if (-not (Test-JsonInteger $payload.schema_version) -or [int64]$payload.schema_version -ne 1) {
            return $null
        }
        if ($payload.mode -isnot [string] -or $payload.mode -ne 'preflight') { return $null }
        if ($payload.ok -isnot [bool] -or $payload.ready -isnot [bool] -or
            $payload.blocked -isnot [bool]) {
            return $null
        }
        if ($null -ne $payload.reason -and $payload.reason -isnot [string]) { return $null }
        if ($payload.root -isnot [string] -or $payload.venv -isnot [string]) { return $null }
        $expectedRoot = Get-CanonicalInstallRoot $InstallRoot
        $payloadRoot = Get-CanonicalInstallRoot $payload.root
        $expectedVenv = Get-CanonicalInstallRoot (Join-Path $InstallRoot 'venv')
        $payloadVenv = Get-CanonicalInstallRoot $payload.venv
        if (-not $expectedRoot -or -not $payloadRoot -or -not $expectedVenv -or -not $payloadVenv -or
            -not [string]::Equals($expectedRoot, $payloadRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($expectedVenv, $payloadVenv, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        foreach ($arrayName in @('processes', 'mcp_bridges', 'pausable_gateway_processes', 'actions')) {
            if ($payload.$arrayName -isnot [System.Array]) { return $null }
        }
        if (-not (Test-JsonInteger $payload.pausable_gateways) -or [int64]$payload.pausable_gateways -lt 0) {
            return $null
        }
        foreach ($objectName in @('git', 'last_update_receipt', 'lease', 'error')) {
            if ($null -ne $payload.$objectName -and $payload.$objectName -isnot [pscustomobject]) {
                return $null
            }
        }
        return $payload
    } catch {
        return $null
    }
}

function Test-VerifiedUpdateReceipt([object]$Receipt) {
    try {
        if ($Receipt -isnot [pscustomobject]) { return $false }
        $expected = @(
            'schema_version', 'invocation_id', 'lease_id', 'mode', 'root',
            'remote', 'branch', 'target_ref', 'target_sha', 'resulting_head',
            'archive_sha', 'timestamp', 'success', 'gateway_resume_deferred', 'health'
        )
        $names = @($Receipt.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne $expected.Count) { return $false }
        foreach ($name in $expected) {
            if ($names -notcontains $name) { return $false }
        }
        if (-not (Test-JsonInteger $Receipt.schema_version) -or [int64]$Receipt.schema_version -ne 1) {
            return $false
        }
        if ($Receipt.invocation_id -isnot [string] -or
            $Receipt.invocation_id -notmatch '^[A-Za-z0-9._-]{16,128}$') { return $false }
        if (-not [string]::Equals(
            [string]$Receipt.invocation_id,
            $script:InvocationId,
            [StringComparison]::Ordinal
        )) { return $false }
        if ($Receipt.lease_id -isnot [string] -or
            $Receipt.lease_id -notmatch '^[A-Za-z0-9._-]{16,128}$') { return $false }
        if ($BridgeLeaseId -and
            -not [string]::Equals($Receipt.lease_id, $BridgeLeaseId, [StringComparison]::Ordinal)) {
            return $false
        }
        if ($Receipt.mode -isnot [string] -or $Receipt.mode -ne 'git') { return $false }
        $expectedRoot = Get-CanonicalInstallRoot $InstallRoot
        $receiptRoot = if ($Receipt.root -is [string]) { Get-CanonicalInstallRoot $Receipt.root } else { $null }
        if (-not $expectedRoot -or -not $receiptRoot -or
            -not [string]::Equals($expectedRoot, $receiptRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        if ($Receipt.branch -isnot [string] -or $Receipt.branch -ne $Branch) { return $false }
        if (-not (Test-JsonInteger $Receipt.timestamp) -or [int64]$Receipt.timestamp -le 0) {
            return $false
        }
        if ($Receipt.success -isnot [bool] -or $Receipt.success -ne $true) { return $false }
        if ($Receipt.gateway_resume_deferred -isnot [bool] -or
            $Receipt.gateway_resume_deferred -ne $true) { return $false }
        if ($Receipt.health -isnot [pscustomobject]) { return $false }
        $healthNames = @($Receipt.health.PSObject.Properties | ForEach-Object { $_.Name })
        $expectedHealth = @('critical_syntax', 'critical_imports', 'dependencies', 'node_dependencies')
        if ($healthNames.Count -ne $expectedHealth.Count) { return $false }
        foreach ($name in $expectedHealth) {
            if ($healthNames -notcontains $name -or $Receipt.health.$name -isnot [bool] -or
                $Receipt.health.$name -ne $true) { return $false }
        }
        if ($Receipt.remote -isnot [string] -or
            $Receipt.remote -notmatch '^[A-Za-z0-9._-]{1,128}$' -or
            -not [string]::Equals($Receipt.remote, $script:PreflightTargetGitRemote, [StringComparison]::Ordinal) -or
            $Receipt.target_ref -isnot [string] -or
            $Receipt.target_ref -ne "refs/remotes/$($Receipt.remote)/$Branch" -or
            -not [string]::Equals($Receipt.target_ref, $script:PreflightTargetGitRef, [StringComparison]::Ordinal) -or
            $Receipt.target_sha -isnot [string] -or $Receipt.target_sha -notmatch '^[0-9a-fA-F]{40}$' -or
            -not [string]::Equals($Receipt.target_sha, $script:PreflightTargetGitHead, [StringComparison]::OrdinalIgnoreCase) -or
            $Receipt.resulting_head -isnot [string] -or $Receipt.resulting_head -notmatch '^[0-9a-fA-F]{40}$' -or
            -not [string]::Equals($Receipt.target_sha, $Receipt.resulting_head, [StringComparison]::OrdinalIgnoreCase) -or
            $null -ne $Receipt.archive_sha) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

$finalCode = 1
$finalMsg = "update did not complete"
$script:HandoffAbort = "hermes-handoff-abort-$([Guid]::NewGuid().ToString('N'))"
try {
    New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction SilentlyContinue | Out-Null
    Show-ProgressWindow
    Write-HandoffLog "hand-off start: root=$InstallRoot branch=$Branch desktopPid=$DesktopPid pid=$PID"
    if ((Test-Path -LiteralPath $ResultPath) -or
        @(Get-HandoffCasArtifacts $ResultPath).Count -gt 0) {
        $finalCode = 8
        $finalMsg = 'Update aborted: a prior Desktop handoff result has not been consumed safely. Nothing was changed.'
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    }

    # -- 0. Adopt the bridge-quiesce lease with OUR real pid ---------------
    # Electron can only initially hand the lease to the short-lived cmd.exe
    # wrapper. The unguessable lease id crosses that process boundary; adopt
    # it before the Desktop exits so a live owner or its bounded grace proves
    # continuity. A missing lease is compatible with older Desktop builds.
    if (-not (Adopt-BridgeQuiesceLease)) {
        $finalCode = 8
        $finalMsg = 'Update aborted: the Codex bridge-quiesce handoff could not be verified. Nothing was changed. Retry the update from Hermes.'
        throw $script:HandoffAbort
    }

    # -- 1. Claim the update marker with OUR pid ---------------------------
    if (-not (Claim-UpdateMarkerAtomically)) {
        $finalCode = 8
        $finalMsg = 'Update aborted: the update handoff marker could not be claimed atomically. Nothing was changed.'
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    }
    Write-HandoffLog "claimed update marker (pid $PID)"

    # -- 2. Wait for the Desktop to exit (FAIL CLOSED) ----------------------
    if ($DesktopPid -gt 0) {
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            $proc = Get-Process -Id $DesktopPid -ErrorAction SilentlyContinue
            if (-not $proc) { break }
            Start-Sleep -Milliseconds 300
            if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
        }
        if (Get-Process -Id $DesktopPid -ErrorAction SilentlyContinue) {
            # A live Desktop means a live backend re-locking the venv at any
            # moment. Updating under it is how installs brick. Abort.
            $finalCode = 4
            $finalMsg = "Update aborted: the Hermes window (pid $DesktopPid) did not exit within 30s. Nothing was changed. Close Hermes fully and try again."
            Write-HandoffLog $finalMsg
            throw $script:HandoffAbort
        }
        Write-HandoffLog "desktop exited"
    }

    # -- 3. Wait for the venv shim to unlock (FAIL CLOSED) ------------------
    $shim = Join-Path $InstallRoot "venv\Scripts\hermes.exe"
    if (Test-Path -LiteralPath $shim) {
        $unlocked = $false
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline) {
            try {
                $fs = [System.IO.File]::Open($shim, 'Open', 'ReadWrite', 'None')
                $fs.Close()
                $unlocked = $true
                break
            } catch {
                Start-Sleep -Milliseconds 400
                if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
            }
        }
        if (-not $unlocked) {
            # Something still maps the venv. --force-ing past it guarantees a
            # half-updated venv (the exact 2026-08-09 Access-denied brick).
            $finalCode = 5
            $finalMsg = "Update aborted: another process is still holding the Hermes install open (venv\Scripts\hermes.exe locked after 20s). Nothing was changed. Close other Hermes windows/terminals and try again."
            Write-HandoffLog $finalMsg
            throw $script:HandoffAbort
        }
        Write-HandoffLog "venv shim unlocked"
    }

    # -- 4. Run the update from the CURRENT checkout ------------------------
    # --force skips only the hermes.exe shim guard, which step 2 just PROVED
    # is unlocked; the venv-python holder guard (orphan reap included) stays
    # active. Our marker claim is adopted by the child via update_lock.py's
    # process-ancestry rule.
    # Invoke the managed interpreter directly. The Windows hermes.exe console
    # shim spawns Python as a second PID, which cannot be the exact owner used
    # for lease transfer/cancellation proof.
    $hermesPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $hermesPython)) {
        $finalCode = 3
        $finalMsg = "Update aborted: the managed Hermes Python is missing. The install needs repair (run the Hermes installer or `hermes doctor`)."
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    }
    $cliPrefix = @('-B', '-m', 'hermes_cli.main')

    # New checkouts expose a stable, read-only JSON preflight. Its answer is
    # authoritative: require exit 0, valid JSON, and a literal ready=true.
    # An older checkout that lacks the stable flag also aborts. The downstream
    # legacy guard ran too late to prove a mutation-free handoff.
    $stablePreflightAvailable = $false
    $preflightArgs = $cliPrefix + @(
        'update', '--preflight', '--json', '--branch', $Branch
    )
    if ($BridgeLeaseId) {
        # The stable readiness probe uses the same private capability to
        # recognize the script-owned lease as this handoff, never as a foreign
        # blocker. Output/argv remain suppressed so the token is not disclosed.
        $preflightArgs += @('--bridge-lease-id', $BridgeLeaseId)
    }
    Write-HandoffLog 'running update safety preflight'
    $preflight = Invoke-StreamedHermes $hermesPython $preflightArgs 'preflight'
    if (Test-UpdatePreflightUnavailable $preflight) {
        $finalCode = 7
        $finalMsg = 'Update aborted: this checkout does not provide the stable update safety preflight. Nothing was changed. Repair or refresh Hermes before retrying.'
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    } else {
        $preflightPayload = Read-UpdatePreflightPayload $preflight
        if (-not $preflightPayload) {
            $finalCode = 7
            $finalMsg = 'Update aborted: the Hermes safety preflight failed or returned invalid JSON. Nothing was changed. Close other Hermes processes and try again.'
            Write-HandoffLog $finalMsg
            throw $script:HandoffAbort
        }
        $gitPreflightClear = Test-TransactionalGitPreflight $preflightPayload.git
        $preflightClear = (
            $preflight.Code -eq 0 -and
            $preflightPayload.ok -eq $true -and
            $preflightPayload.ready -eq $true -and
            $preflightPayload.blocked -eq $false -and
            $null -eq $preflightPayload.reason -and
            @($preflightPayload.processes).Count -eq 0 -and
            @($preflightPayload.mcp_bridges).Count -eq 0 -and
            $gitPreflightClear -and
            $null -eq $preflightPayload.error
        )
        if (-not $preflightClear) {
            $finalCode = 7
            $finalMsg = 'Update aborted: the Hermes safety preflight reported an active blocker. Nothing was changed. Close the listed Hermes process or terminal and try again.'
            Write-HandoffLog $finalMsg
            throw $script:HandoffAbort
        }
        $script:PreUpdateGitHead = [string]$preflightPayload.git.head
        $script:PreUpdateGitBranch = [string]$preflightPayload.git.branch
        $script:PreflightTargetGitHead = [string]$preflightPayload.git.target_sha
        $script:PreflightTargetGitRemote = [string]$preflightPayload.git.tracking_remote
        $script:PreflightTargetGitRef = [string]$preflightPayload.git.target_ref
        $stablePreflightAvailable = $true
        Write-HandoffLog 'update safety preflight passed'
    }
    try {
        $activationLaunch = Resolve-ManagedVenvPythonLaunch $hermesPython
        $script:ActivationBasePython = [string]$activationLaunch.Executable
    } catch {
        $finalCode = 3
        $finalMsg = 'Update aborted: the private managed base interpreter could not be authenticated. Nothing was changed.'
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    }
    if (-not $script:ActivationBasePython -or
        $BridgeLeaseId -notmatch '^[A-Za-z0-9._-]{16,128}$') {
        $finalCode = 3
        $finalMsg = 'Update aborted: the private managed update capability is invalid. Nothing was changed.'
        Write-HandoffLog $finalMsg
        throw $script:HandoffAbort
    }

    $updateArgs = $cliPrefix + @(
        'update', '--yes', '--gateway', '--defer-gateway-resume',
        '--invocation-id', $script:InvocationId,
        '--force', '--branch', $Branch
    )
    $requiresChildLeaseAdoption = $stablePreflightAvailable -and $script:BridgeLeaseRequired
    if ($requiresChildLeaseAdoption) {
        $updateArgs += @('--bridge-lease-id', $BridgeLeaseId)
    }
    # Never log the capability-bearing argv. The stage and branch are enough
    # for diagnostics; the unguessable lease token stays private.
    Write-HandoffLog "running Hermes update (branch $Branch)"
    $res = Invoke-StreamedHermes $hermesPython $updateArgs "update" -AllowBridgeLeaseTransfer:$requiresChildLeaseAdoption -RequireBridgeLeaseReturn:$requiresChildLeaseAdoption
    Write-HandoffLog "hermes update exit code: $($res.Code)"

    # The contained child stages a complete side-by-side venv and returns the
    # lease; it never rewrites the running venv. Activate only after that Job
    # has drained. Commit only after build, health, and receipt verification,
    # and before any gateway or Desktop process is restarted.
    $desktopBuildFailed = $false
    $activationRollbackFailed = $false
    $expectedBuildId = $null
    if ($res.Code -eq 0) {
        $expectedBuildId = Read-ActivationTargetHead
        if (-not $expectedBuildId -or
            -not (Invoke-DesktopActivationAction 'activate')) {
            $res.Code = 9
            Write-HandoffLog 'candidate activation failed; success withheld'
        } else {
            $script:ActivationStarted = $true
            $hermesPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
        }
    }

    # Post-activation launcher repair belongs here, before the shared failure
    # rollback, Desktop build, and receipt.  After the upstream 999703 refresh,
    # wire only the dedicated missing-only repair action at this boundary; it
    # must never overwrite an existing bin\hermes.exe, and receipt publication
    # must wait for its proof.

    if ($res.Code -ne 0) {
        $rollbackAction = if ($script:ActivationStarted -or
            (Test-Path -LiteralPath $ActivationManifestPath) -or
            (Test-Path -LiteralPath $ActivationStatePath)) { 'rollback' } else { 'rollback-source' }
        if (-not (Invoke-DesktopActivationAction $rollbackAction)) {
            $activationRollbackFailed = $true
            $res.Code = 15
            Write-HandoffLog 'pre-recovery installation rollback could not be proven'
        } else {
            $script:ActivationStarted = $false
            $script:ActivationReceiptPublished = $false
            $script:VerifiedUpdateReceipt = $null
            $hermesPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
        }
    }

    # -- 5. Truthful completion: don't trust exit 0 -------------------------
    # `hermes update` treats a Desktop GUI build failure as NON-fatal (prints
    # a one-line warning, exits 0). For a Desktop-DRIVEN update that warning
    # is fatal: we would relaunch the old exe and call it success. Detect it,
    # retry the build once, and propagate honestly.
    if ($res.Code -eq 0 -and $script:ActivationStarted) {
        # Always force one target-correlated build. A content-hash no-op can
        # otherwise preserve an old install-stamp when the updated commit did
        # not touch Desktop sources. Transactional activation supports only a
        # clean, authenticated Git target and pins its exact 40-hex HEAD.
        $oldGithubSha = $env:GITHUB_SHA
        $oldGithubRefName = $env:GITHUB_REF_NAME
        try {
            $env:GITHUB_SHA = $expectedBuildId
            $env:GITHUB_REF_NAME = $Branch
            Write-HandoffLog 'running receipt-correlated Desktop rebuild'
            $rebuild = Invoke-StreamedHermes $hermesPython ($cliPrefix + @('desktop', '--force-build', '--build-only')) 'rebuild'
            Write-HandoffLog "desktop rebuild exit code: $($rebuild.Code)"
            if ($rebuild.Code -ne 0 -or
                -not (Publish-DesktopHealthProof $expectedBuildId)) {
                $desktopBuildFailed = $true
                Write-HandoffLog 'Desktop rebuild or its exact dependency proof failed; success withheld'
            }
        } finally {
            if ($null -eq $oldGithubSha) { Remove-Item Env:GITHUB_SHA -ErrorAction SilentlyContinue } else { $env:GITHUB_SHA = $oldGithubSha }
            if ($null -eq $oldGithubRefName) { Remove-Item Env:GITHUB_REF_NAME -ErrorAction SilentlyContinue } else { $env:GITHUB_REF_NAME = $oldGithubRefName }
        }

        if (-not $desktopBuildFailed) {
            if (Invoke-DesktopActivationAction 'publish-receipt') {
                $script:ActivationReceiptPublished = $true
                $candidateReceipt = $null
                try {
                    $receiptRaw = Read-BoundedUtf8Text $UpdateReceiptPath 65536
                    if (-not [string]::IsNullOrWhiteSpace($receiptRaw)) {
                        $candidateReceipt = $receiptRaw | ConvertFrom-Json -ErrorAction Stop
                    }
                } catch {}
                if (Test-VerifiedUpdateReceipt $candidateReceipt) {
                    $script:VerifiedUpdateReceipt = $candidateReceipt
                    $script:ReceiptVerifiedAt = [int64][Math]::Max(
                        (Get-UnixTimeSeconds),
                        [int64]$candidateReceipt.timestamp
                    )
                    Write-HandoffLog 'activated update receipt verified'
                } else {
                    $res.Code = 9
                    Write-HandoffLog 'activated receipt verification failed; success withheld'
                }
            } else {
                $res.Code = 9
                Write-HandoffLog 'activated receipt could not be published; success withheld'
            }
        }

        if ($desktopBuildFailed -or $res.Code -ne 0) {
            if (-not (Invoke-DesktopActivationAction 'rollback')) {
                $activationRollbackFailed = $true
                $res.Code = 15
                Write-HandoffLog 'post-build installation rollback could not be proven'
            } else {
                $script:ActivationStarted = $false
                $script:ActivationReceiptPublished = $false
                $script:VerifiedUpdateReceipt = $null
                $hermesPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
                Write-HandoffLog 'previous source, venv, receipt, and Desktop build restored'
            }
        }
    }

    if ($res.Code -eq 0 -and -not $desktopBuildFailed -and
        $script:ActivationStarted -and $script:ActivationReceiptPublished -and
        $script:VerifiedUpdateReceipt) {
        if (Invoke-DesktopActivationAction 'commit') {
            $script:ActivationCommitted = $true
            $script:ActivationStarted = $false
            Write-HandoffLog 'verified installation committed before process restart'
        } else {
            # Once commit is attempted, its durable commit-cleaning phase may
            # already forbid rollback. Keep every Hermes process stopped and
            # let an exact retry finish cleanup; never start mixed generations.
            $script:ActivationStarted = $false
            $script:RelaunchSuppressed = $true
            $res.Code = 16
            Write-HandoffLog 'installation commit did not finish; all process restart was withheld'
        }
    }

    if ($res.Code -eq 16) {
        $finalCode = 16
        $finalMsg = 'The verified update reached its commit boundary, but cleanup did not finish. Hermes was left stopped so a retry can finish safely.'
        throw $script:HandoffAbort
    }

    # The update child intentionally does not restart gateways inside the
    # mutation Job: persistent processes there would die when the Job closes,
    # while breakaway permission would let arbitrary mutators escape. Keep the
    # parent-owned lease through the final rebuild, then resume only the
    # authenticated invocation-correlated plan outside containment. Recovery is
    # attempted even after update/rebuild failure. A recovery failure is itself
    # terminal and the result message retains the original update detail.
    Write-HandoffLog 'restoring the verified gateway fleet outside mutation containment'
    $resumeArgs = $cliPrefix + @(
        'update', '--resume-deferred-gateway',
        '--invocation-id', $script:InvocationId,
        '--bridge-lease-id', $BridgeLeaseId,
        '--root', $InstallRoot
    )
    $resume = Invoke-DeferredGatewayResume $hermesPython $resumeArgs
    Write-HandoffLog "deferred gateway recovery exit code: $($resume.Code)"
    if ($resume.Code -ne 0) {
        $res.Output += "`nThe verified gateway fleet could not be restored after the update attempt."
    }

    if ($resume.Code -ne 0 -and $script:ActivationStarted) {
        if (Invoke-DesktopActivationAction 'rollback') {
            $script:ActivationStarted = $false
            $script:ActivationReceiptPublished = $false
            $script:VerifiedUpdateReceipt = $null
            $hermesPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
            Write-HandoffLog 'gateway recovery failed; previous installation restored'
        } else {
            $activationRollbackFailed = $true
            Write-HandoffLog 'gateway recovery and installation rollback both failed'
        }
    }

    if ($resume.Code -ne 0) {
        $finalCode = if ($activationRollbackFailed) { 15 } else { 13 }
        $recoveryFailure = if ($resume.PendingPlanVerified) {
            'The update attempt ended without restoring the verified Hermes gateway fleet.'
        } else {
            'Hermes could not verify whether gateway recovery was required or completed.'
        }
        if ($res.Code -ne 0) {
            $reportedUpdateCode = if ($null -ne $res.ChildExitCode) {
                [int]$res.ChildExitCode
            } else {
                [int]$res.Code
            }
            $finalMsg = "Hermes update failed (exit $reportedUpdateCode). See logs\desktop-update-handoff.log. $recoveryFailure"
        } elseif ($desktopBuildFailed) {
            $finalMsg = "The Desktop app rebuild failed and the previous installation was restored. $recoveryFailure"
        } else {
            $finalMsg = $recoveryFailure
        }
    } elseif ($res.Code -eq 0 -and -not $desktopBuildFailed -and
        $script:ActivationCommitted -and $script:ActivationReceiptPublished -and
        $script:VerifiedUpdateReceipt) {
        $finalCode = 0
        $finalMsg = "Update complete."
    } elseif ($desktopBuildFailed) {
        $finalCode = 6
        $finalMsg = 'The Desktop app rebuild failed and the previous installation was restored. Retry the update after reviewing the handoff log.'
    } else {
        $finalCode = $res.Code
        $finalMsg = "hermes update failed (exit $($res.Code)). See logs\desktop-update-handoff.log."
    }
    throw $script:HandoffAbort
} catch {
    if ($_.Exception.Message -ne $script:HandoffAbort) {
        $finalCode = 1
        $finalMsg = 'Update handoff failed unexpectedly. No unverified diagnostic detail was returned.'
        Write-HandoffLog $finalMsg
    }
} finally {
    if ($finalCode -ne 0 -and $script:ActivationStarted -and
        -not (Restore-DesktopActivationBeforeRelaunch)) {
        $finalCode = 15
        $finalMsg = 'The update failed and the previous installation could not be restored automatically.'
        Write-HandoffLog $finalMsg
    }
    if ($script:RelaunchRequired -and -not (Prepare-DesktopSingleInstanceHandoff)) {
        $script:RelaunchSuppressed = $true
        if ($finalCode -eq 0) { $finalCode = 14 }
        $finalMsg = 'The update attempt ended without a proved Desktop single-instance exit handoff; automatic relaunch was suppressed.'
        Write-HandoffLog $finalMsg
        if (-not (Remove-RelaunchRequestExact)) {
            Write-HandoffLog 'WARNING: the failed relaunch request could not be retired exactly; it will expire automatically'
        }
    }
    if ($finalCode -ne 0 -and $script:ActivationStarted -and
        -not (Restore-DesktopActivationBeforeRelaunch)) {
        $finalCode = 15
        $finalMsg = 'The update failed and the previous installation could not be restored automatically.'
        Write-HandoffLog $finalMsg
    }
    $updateMarkerReleased = [bool](Remove-MarkerIfOwned)
    $bridgeLeaseReleased = [bool](Remove-BridgeQuiesceLeaseIfOwned)
    Close-ProgressWindow
    if ($script:RelaunchRequired -and -not $script:RelaunchSuppressed -and
        -not (Prepare-DesktopSingleInstanceHandoff)) {
        $script:RelaunchSuppressed = $true
        if ($finalCode -eq 0) { $finalCode = 14 }
        $finalMsg = 'A late Desktop instance could not be handed off safely; automatic relaunch was suppressed.'
        Write-HandoffLog $finalMsg
        if (-not (Remove-RelaunchRequestExact)) {
            Write-HandoffLog 'WARNING: the late relaunch request could not be retired exactly; it will expire automatically'
        }
    }
    if ($finalCode -ne 0 -and $script:ActivationStarted -and
        -not (Restore-DesktopActivationBeforeRelaunch)) {
        $finalCode = 15
        $finalMsg = 'The update failed and the previous installation could not be restored automatically.'
        Write-HandoffLog $finalMsg
    }
    Start-DesktopRelaunch
    $receiptVerified = $null -ne $script:VerifiedUpdateReceipt
    $relaunchRequested = $script:RelaunchRequired -and $script:RelaunchStarted
    $proofComplete = $receiptVerified -and $updateMarkerReleased -and
        $bridgeLeaseReleased -and $relaunchRequested
    if ($finalCode -eq 0 -and -not $proofComplete) {
        $finalCode = 10
        $finalMsg = 'Update finished without complete receipt, cleanup, or relaunch-request proof; refusing to report success.'
        Write-HandoffLog $finalMsg
    }
    if ($finalCode -eq 0) {
        $pendingMessage = 'Update mutation and cleanup are verified; waiting for the relaunched Desktop readiness acknowledgment.'
        $pendingRaw = New-HandoffResultJson 'pending' 0 $pendingMessage $updateMarkerReleased $bridgeLeaseReleased $null
        if (-not (Publish-HandoffResult $pendingRaw $null)) {
            $finalCode = 11
            $finalMsg = 'Update finished, but its pending relaunch proof could not be published durably.'
        } else {
            if (-not (Remove-RelaunchRequestExact)) {
                $finalCode = 11
                $finalMsg = 'The relaunched Desktop was authorized, but the single-instance request could not be retired exactly.'
                $failedRaw = New-HandoffResultJson 'failed' $finalCode $finalMsg $updateMarkerReleased $bridgeLeaseReleased $null
                if (-not (Publish-HandoffResult $failedRaw $pendingRaw)) {
                    Write-HandoffLog 'ERROR: pending relaunch state could not be terminalized after request cleanup failed'
                }
            } else {
                $ack = Wait-ForDesktopHandoffAck 180
                if ($ack.Status -eq 'acknowledged' -and (Remove-DesktopAckExact $ack.Raw)) {
                    $completeRaw = New-HandoffResultJson 'complete' 0 'Update complete.' $updateMarkerReleased $bridgeLeaseReleased $ack.Proof
                    if (-not (Publish-HandoffResult $completeRaw $pendingRaw)) {
                        $finalCode = 11
                        $finalMsg = 'Desktop became ready, but terminal handoff completion could not be published by exact CAS.'
                        $failedRaw = New-HandoffResultJson 'failed' $finalCode $finalMsg $updateMarkerReleased $bridgeLeaseReleased $null
                        if (-not (Publish-HandoffResult $failedRaw $pendingRaw)) {
                            Write-HandoffLog 'ERROR: pending relaunch state could not be terminalized after completion publication failed'
                        }
                    }
                } else {
                    $finalCode = 12
                    $finalMsg = switch ($ack.Status) {
                        'exited' { 'The relaunched Desktop exited before it became ready.' }
                        'invalid' { 'The relaunched Desktop wrote a mismatched or unhealthy readiness acknowledgment.' }
                        default { 'The relaunched Desktop did not become ready within the bounded acknowledgment window.' }
                    }
                    if ($ack.Status -eq 'exited' -and $script:ActivationStarted) {
                        if (Restore-DesktopActivationBeforeRelaunch -RelaunchExitProved) {
                            Write-HandoffLog 'relaunch exited before readiness; previous installation restored'
                        } else {
                            $finalCode = 15
                            $finalMsg = 'The relaunched Desktop exited and the previous installation could not be restored automatically.'
                        }
                    }
                    $failedRaw = New-HandoffResultJson 'failed' $finalCode $finalMsg $updateMarkerReleased $bridgeLeaseReleased $null
                    if (-not (Publish-HandoffResult $failedRaw $pendingRaw)) {
                        $finalCode = 11
                        Write-HandoffLog 'ERROR: pending relaunch state could not be terminalized by exact CAS'
                    }
                }
            }
        }
    } else {
        $failedRaw = New-HandoffResultJson 'failed' $finalCode $finalMsg $updateMarkerReleased $bridgeLeaseReleased $null
        if (-not (Publish-HandoffResult $failedRaw $null)) {
            if ($finalCode -eq 0) { $finalCode = 11 }
            Write-HandoffLog 'ERROR: no durable terminal handoff failure was published'
        } elseif (-not (Remove-RelaunchRequestExact)) {
            Write-HandoffLog 'WARNING: terminal failure was published but its relaunch request could not be retired exactly'
        }
    }
    Focus-RelaunchedDesktop
}
exit $finalCode
