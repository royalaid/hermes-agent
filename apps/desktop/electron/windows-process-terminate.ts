/**
 * SuperF4-style exact process termination for Windows update force-release.
 *
 * Behavioral reference (not copied): stefansundin/superf4 commit
 * 6b677d422553e6b908b9eeaff4333b8b457e7bef, superf4.c lines 236-289.
 * SuperF4 is GPL-3.0 — do not copy its implementation. This module follows
 * Microsoft Win32 documentation for:
 *   OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE)
 *   TerminateProcess
 *   WaitForSingleObject
 *   CloseHandle
 * and optionally AdjustTokenPrivileges(SeDebugPrivilege) when present.
 *
 * The PowerShell child that performs TerminateProcess is itself bounded by a
 * hard wall-clock budget. On expiry the child tree is killed and the call
 * returns only after the child PID is confirmed gone (or a Windows Job Object
 * KILL_ON_JOB_CLOSE terminal is applied). No late mutation after return.
 */

import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder, ForceReleaseTerminateResult } from './windows-update-force-release'

const execFileAsync = promisify(execFile)

export type StartTargetJobWatcher = (
  ownerPid: number,
  ownerCreatedAt: number,
  targetJobName: string,
  watcherReadyPath: string,
  watcherReadyNonce: string,
  deadlineAt: number
) => ChildProcess | undefined

export type HardBoundaryDependencies = {
  startWatcher?: StartTargetJobWatcher
}

const WRAPPER_MARKER_POLL_MS = 10
const WRAPPER_PHASE_NAMES = [
  'marker-published',
  'target-job-created',
  'watcher-READY-observed',
  'inner-started',
  'inner-exited',
  'target-terminated',
  'finally-close',
  'handles-closed'
] as const

type TargetJobWatcherDiagnostics = {
  spawnedAt: number
  pid?: number
  errorAt?: number
  errorCode?: string | number
  errorMessage?: string
  exitAt?: number
  exitCode?: number | null
  signalCode?: NodeJS.Signals | null
}

export type RunPowerShell = (
  script: string,
  timeoutMs?: number,
  signal?: AbortSignal,
  deadlineAt?: number,
  dependencies?: HardBoundaryDependencies
) => Promise<{ stdout: string; stderr: string; code: number; pid?: number }>

/** Shared kill/identity-confirm reserve used by the terminate boundary. */
export const TERMINATE_KILL_CONFIRM_MS = 1_500
export const TERMINATE_KILL_CONFIRM_MIN_MS = 400
export const TERMINATE_KILL_CONFIRM_RATIO = 0.3

export function terminateKillReserveMs(budgetMs: number): number {
  const budget = Math.max(0, Math.trunc(budgetMs))
  if (budget <= 0) return 0
  return Math.min(
    budget,
    TERMINATE_KILL_CONFIRM_MS,
    Math.max(Math.min(TERMINATE_KILL_CONFIRM_MIN_MS, budget), Math.floor(budget * TERMINATE_KILL_CONFIRM_RATIO))
  )
}

function powershellExecutable(): string {
  const windowsRoot = process.env.SystemRoot || 'C:\\Windows'
  return path.join(windowsRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

/**
 * The mutating PowerShell script runs behind a KILL_ON_JOB_CLOSE supervisor.
 * If the supervisor is killed during cancellation, Windows closes its job
 * handle and terminates the nested script as a hard terminal boundary. The
 * target script travels through an environment value and a temporary file so
 * it is never interpolated into the supervisor's PowerShell source.
 */
export const TERMINATE_JOB_WRAPPER_COMMAND = String.raw`
$ErrorActionPreference = 'Stop'
$targetScript = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_SCRIPT')
$helperJobName = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_JOB_NAME')
$targetJobName = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_JOB_NAME')
$targetWaitText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_WAIT_MS')
$deadlineAtText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_DEADLINE_AT')
$watcherReadyPath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_READY_PATH')
$watcherReadyNonce = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_READY_NONCE')
$wrapperPidMarkerPath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WRAPPER_PID_MARKER_PATH')
$wrapperPidMarkerNonce = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WRAPPER_PID_MARKER_NONCE')
$wrapperPhasePath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WRAPPER_PHASE_PATH')
$wrapperPhaseNonce = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WRAPPER_PHASE_NONCE')
$helperScriptPath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_HELPER_SCRIPT_PATH')
$helperGatePath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_HELPER_GATE_PATH')
if (
    [string]::IsNullOrWhiteSpace($targetScript) -or
    [string]::IsNullOrWhiteSpace($helperJobName) -or
    [string]::IsNullOrWhiteSpace($targetJobName) -or
    [string]::IsNullOrWhiteSpace($watcherReadyPath) -or
    [string]::IsNullOrWhiteSpace($watcherReadyNonce) -or
    [string]::IsNullOrWhiteSpace($helperScriptPath) -or
    [string]::IsNullOrWhiteSpace($helperGatePath)
) { exit 87 }
function Write-WrapperPidMarker {
    $hasMarkerPath = -not [string]::IsNullOrWhiteSpace($wrapperPidMarkerPath)
    $hasMarkerNonce = -not [string]::IsNullOrWhiteSpace($wrapperPidMarkerNonce)
    if (-not $hasMarkerPath -and -not $hasMarkerNonce) { return }
    if (-not $hasMarkerPath -or -not $hasMarkerNonce) { throw 'WRAPPER_MARKER_INVALID_INPUT' }
    $process = Get-Process -Id $PID -ErrorAction Stop
    $createdAtMs = [DateTimeOffset]::new($process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()
    $markerValue = 'PID:' + [string]$PID + ';CREATED_AT_MS:' + [string]$createdAtMs + ';NONCE:' + $wrapperPidMarkerNonce
    $markerTempPath = $wrapperPidMarkerPath + '.tmp'
    $stream = $null
    $writer = $null
    try {
        $stream = [IO.File]::Open($markerTempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
        $writer.Write($markerValue)
        $writer.Flush()
        $writer.Dispose()
        $writer = $null
        $stream = $null
        [IO.File]::Move($markerTempPath, $wrapperPidMarkerPath)
    } catch {
        if ($null -ne $writer) { try { $writer.Dispose() } catch {} }
        elseif ($null -ne $stream) { try { $stream.Dispose() } catch {} }
        try { Remove-Item -LiteralPath $markerTempPath -Force -ErrorAction SilentlyContinue } catch {}
        throw
    }
}
function Write-WrapperPhase([string]$phase, [string]$detail = '') {
    if ([string]::IsNullOrWhiteSpace($wrapperPhasePath) -or [string]::IsNullOrWhiteSpace($wrapperPhaseNonce)) { return }
    $safePhase = $phase -replace '[^A-Za-z0-9_-]', '_'
    $phasePath = $wrapperPhasePath + '.' + $safePhase + '.marker'
    $phaseTempPath = $phasePath + '.tmp'
    $safeDetail = ([string]$detail) -replace '[\r\n;]', '_'
    $phaseValue = 'PHASE:' + $wrapperPhaseNonce + ';NAME:' + $safePhase + ';TICKS:' + [Diagnostics.Stopwatch]::GetTimestamp() + ';DETAIL:' + $safeDetail
    $stream = $null
    $writer = $null
    try {
        $stream = [IO.File]::Open($phaseTempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
        $writer.Write($phaseValue)
        $writer.Flush()
        $writer.Dispose()
        $writer = $null
        $stream = $null
        [IO.File]::Move($phaseTempPath, $phasePath)
    } catch {
        if ($null -ne $writer) { try { $writer.Dispose() } catch {} }
        elseif ($null -ne $stream) { try { $stream.Dispose() } catch {} }
        try { Remove-Item -LiteralPath $phaseTempPath -Force -ErrorAction SilentlyContinue } catch {}
    }
}
Write-WrapperPidMarker
Write-WrapperPhase 'marker-published'
$targetWaitMs = 1500
$parsedTargetWaitMs = 0
if ([int]::TryParse($targetWaitText, [ref]$parsedTargetWaitMs)) {
    $targetWaitMs = [Math]::Max(0, $parsedTargetWaitMs)
}
$deadlineAt = 0L
$parsedDeadlineAt = 0L
if ([long]::TryParse($deadlineAtText, [ref]$parsedDeadlineAt)) {
    $deadlineAt = [Math]::Max(0L, $parsedDeadlineAt)
}
function Get-TargetWaitMs {
    if ($deadlineAt -le 0) { return $targetWaitMs }
    $remaining = $deadlineAt - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($remaining -le 0) { return -1 }
    return [Math]::Min($targetWaitMs, [int][Math]::Min($remaining, [long][int]::MaxValue))
}
function Wait-TargetWatcherArmed {
    $expectedReadyValue = 'ARMED:' + $watcherReadyNonce
    $expectedFailurePrefix = 'FAILED:' + $watcherReadyNonce
    $waitDeadline = $deadlineAt
    if ($waitDeadline -le 0) {
        $waitDeadline = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + [Math]::Max(1, $targetWaitMs)
    }
    while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $waitDeadline) {
        if (Test-Path -LiteralPath $watcherReadyPath) {
            try {
                $readyValue = [IO.File]::ReadAllText($watcherReadyPath).Trim()
                if ($readyValue -eq $expectedReadyValue) {
                    Write-WrapperPhase 'watcher-READY-observed'
                    return
                }
                if ($readyValue.StartsWith($expectedFailurePrefix, [StringComparison]::Ordinal)) {
                    throw $readyValue
                }
            } catch {
                if ($_.Exception.Message.StartsWith($expectedFailurePrefix, [StringComparison]::Ordinal)) { throw }
            }
        }
        Start-Sleep -Milliseconds 10
    }
    throw 'TARGET_WATCHER_NOT_ARMED'
}

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class HermesTerminateJob {
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
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateKillOnClose(string name) {
        IntPtr job = CreateJobObject(IntPtr.Zero, name);
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
    public static void Assign(IntPtr job, IntPtr process) {
        if (!AssignProcessToJobObject(job, process)) throw new Win32Exception();
    }
    public static int TerminateAndWait(IntPtr job, int waitMs) {
        if (!TerminateJobObject(job, 1)) {
            int error = Marshal.GetLastWin32Error();
            return error == 0 ? -1 : -error;
        }
        uint result = WaitForSingleObject(job, (uint)Math.Max(0, waitMs));
        if (result == 0) return 0;
        if (result == 258) return -258;
        int waitError = Marshal.GetLastWin32Error();
        return waitError == 0 ? -1 : -waitError;
    }
    public static void Close(IntPtr job) {
        if (job != IntPtr.Zero) CloseHandle(job);
    }
    public static string QuoteArgument(string value) {
        if (value == null) return "\"\"";
        var quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char current in value) {
            if (current == '\\') { slashes++; continue; }
            if (current == '"') {
                quoted.Append('\\', slashes * 2 + 1).Append('"');
                slashes = 0;
                continue;
            }
            quoted.Append('\\', slashes).Append(current);
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2).Append('"');
        return quoted.ToString();
    }
}
'@ -ErrorAction Stop

$helperJob = [IntPtr]::Zero
$targetJob = [IntPtr]::Zero
$child = $null
$tempScript = $helperScriptPath
$targetTerminationComplete = $false
$finalExitCode = 1
try {
    $gatePrologue = @'
$helperGatePath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_HELPER_GATE_PATH')
$helperGateDeadlineText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_DEADLINE_AT')
$helperGateDeadline = 0L
[void][long]::TryParse($helperGateDeadlineText, [ref]$helperGateDeadline)
while (-not (Test-Path -LiteralPath $helperGatePath)) {
    if ($helperGateDeadline -gt 0 -and [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -ge $helperGateDeadline) { exit 87 }
    Start-Sleep -Milliseconds 5
}
'@
    [IO.File]::WriteAllText(
        $tempScript,
        ($gatePrologue + [Environment]::NewLine + $targetScript),
        [Text.UTF8Encoding]::new($false)
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $psi.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + [HermesTerminateJob]::QuoteArgument($tempScript)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['HERMES_TERMINATE_HELPER_GATE_PATH'] = $helperGatePath

    # The wrapper owns two independent kill-on-close jobs. The gated inner
    # helper is assigned to helperJob before its script can run; the inner script
    # opens targetJob only to assign the separately authenticated external target
    # tree. Wrapper death therefore contains helper descendants without changing
    # the target tree's job membership contract.
    $targetJob = [HermesTerminateJob]::CreateKillOnClose($targetJobName)
    $helperJob = [HermesTerminateJob]::CreateKillOnClose($helperJobName)
    Write-WrapperPhase 'target-job-created'
    # The wrapper must not start the mutating helper until a detached watcher
    # has authenticated this exact wrapper process and opened this exact job.
    # Without this gate a watcher startup failure could be mistaken for a
    # completed boundary while the nested helper still owns a job handle.
    Wait-TargetWatcherArmed
    $child = [System.Diagnostics.Process]::Start($psi)
    [HermesTerminateJob]::Assign($helperJob, $child.Handle)
    [IO.File]::WriteAllText($helperGatePath, 'GO', [Text.UTF8Encoding]::new($false))
    Write-WrapperPhase 'inner-started' ('pid=' + [string]$child.Id)
    $stdoutTask = $child.StandardOutput.ReadToEndAsync()
    $stderrTask = $child.StandardError.ReadToEndAsync()
    $child.WaitForExit()
    $childStdout = $stdoutTask.Result
    $childStderr = $stderrTask.Result
    Write-WrapperPhase 'inner-exited' ('code=' + [string]$child.ExitCode)
    # A successful inner TERMINATED marker means the inner script already
    # called TerminateJobObject and waited for the shared target job to drain.
    # Do not start a second relative drain after the absolute deadline has
    # expired; the wrapper's owned handle still closes in finally.
    if ($child.ExitCode -eq 0 -and $childStdout -match '(?m)^\s*TERMINATED\s*$') {
        $targetTerminationComplete = $true
        Write-WrapperPhase 'target-terminated'
        [Console]::Out.Write($childStdout)
        [Console]::Error.Write($childStderr)
        $finalExitCode = $child.ExitCode
    } else {
        $drainWaitMs = [int](Get-TargetWaitMs)
        if ($drainWaitMs -lt 0) {
            [Console]::Out.Write($childStdout)
            [Console]::Error.Write($childStderr)
            [Console]::Error.WriteLine('TARGET_JOB_DEADLINE_EXHAUSTED')
            $finalExitCode = 1
        } else {
            $targetCode = [HermesTerminateJob]::TerminateAndWait($targetJob, $drainWaitMs)
            if ($targetCode -ne 0) {
                [Console]::Out.Write($childStdout)
                [Console]::Error.Write($childStderr)
                [Console]::Error.WriteLine('TARGET_JOB_TERMINATE_FAILED win32=' + (-$targetCode))
                $finalExitCode = 1
            } else {
                $targetTerminationComplete = $true
                Write-WrapperPhase 'target-terminated'
                [Console]::Out.Write($childStdout)
                [Console]::Error.Write($childStderr)
                $finalExitCode = $child.ExitCode
            }
        }
    }
} catch {
    if ($null -ne $child) {
        try { if (!$child.HasExited) { $child.Kill() } } catch {}
    }
    $failure = [string]$_.Exception.Message
    if ($targetJob -ne [IntPtr]::Zero -and -not $targetTerminationComplete) {
        try {
            $drainWaitMs = [int](Get-TargetWaitMs)
            if ($drainWaitMs -lt 0) {
                $failure += ' TARGET_JOB_DEADLINE_EXHAUSTED'
            } else {
                $targetCode = [HermesTerminateJob]::TerminateAndWait($targetJob, $drainWaitMs)
                if ($targetCode -ne 0) {
                    $failure += ' TARGET_JOB_TERMINATE_FAILED win32=' + (-$targetCode)
                }
            }
        } catch {
            $failure += ' TARGET_JOB_TERMINATE_FAILED ' + [string]$_.Exception.Message
        }
    }
    [Console]::Error.WriteLine($failure)
    $finalExitCode = 1
} finally {
    Write-WrapperPhase 'finally-close'
    if ($null -ne $child) { $child.Dispose() }
    [HermesTerminateJob]::Close($targetJob)
    [HermesTerminateJob]::Close($helperJob)
    Remove-Item -LiteralPath $helperGatePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
    Write-WrapperPhase 'handles-closed'
}
[Environment]::Exit($finalExitCode)
`.trim()

/**
 * Direct sibling termination of the persistent target job. This is invoked
 * before killing the wrapper so cancellation does not depend on observing the
 * wrapper's final handle teardown or on taskkill's process-tree traversal.
 */
export const TERMINATE_NAMED_JOB_COMMAND = String.raw`
$ErrorActionPreference = 'Stop'
$targetJobName = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_JOB_NAME')
$targetWaitText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_WAIT_MS')
$diagnosticLog = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_NAMED_JOB_LOG')
function Write-Diagnostic([string]$message) {
    if ([string]::IsNullOrWhiteSpace($diagnosticLog)) { return }
    try { [IO.File]::AppendAllText($diagnosticLog, ($message + [Environment]::NewLine)) } catch {}
}
if ([string]::IsNullOrWhiteSpace($targetJobName)) { Write-Diagnostic 'invalid-input'; exit 87 }
Write-Diagnostic ('started job=' + $targetJobName)
$targetWaitMs = 500
$parsedTargetWaitMs = 0
if ([int]::TryParse($targetWaitText, [ref]$parsedTargetWaitMs)) {
    $targetWaitMs = [Math]::Max(0, $parsedTargetWaitMs)
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HermesTerminateNamedJob {
    private const uint JOB_OBJECT_TERMINATE = 0x0008;
    private const uint JOB_OBJECT_QUERY = 0x0004;
    private const uint SYNCHRONIZE = 0x00100000;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenJobObject(uint desiredAccess, bool inheritHandle, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    public static int OpenAndTerminate(string name, int waitMs) {
        IntPtr job = OpenJobObject(JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY | SYNCHRONIZE, false, name);
        if (job == IntPtr.Zero) {
            int openError = Marshal.GetLastWin32Error();
            return openError == 2 || openError == 6 ? 0 : -openError;
        }
        try {
            if (!TerminateJobObject(job, 1)) {
                int terminateError = Marshal.GetLastWin32Error();
                return terminateError == 0 ? -1 : -terminateError;
            }
            uint result = WaitForSingleObject(job, (uint)Math.Max(0, waitMs));
            if (result == WAIT_OBJECT_0) return 0;
            if (result == WAIT_TIMEOUT) return -258;
            return -(int)result;
        } finally {
            CloseHandle(job);
        }
    }
}
'@ -ErrorAction Stop
$exitCode = 1
try {
    $result = [HermesTerminateNamedJob]::OpenAndTerminate($targetJobName, $targetWaitMs)
    Write-Diagnostic ('result=' + $result)
    if ($result -eq 0) {
        $exitCode = 0
    } else {
        [Console]::Error.WriteLine('TARGET_JOB_TERMINATE_FAILED win32=' + (-$result))
    }
} catch {
    Write-Diagnostic ('exception=' + [string]$_.Exception.Message)
    [Console]::Error.WriteLine('TARGET_JOB_TERMINATE_FAILED ' + [string]$_.Exception.Message)
}
exit $exitCode
`.trim()

/**
 * Detached sibling watcher for the named target job. It opens an authenticated
 * process handle for the wrapper, waits for that exact process object to exit,
 * then terminates the target job. This covers the narrow window where a direct
 * supervisor kill can finish before the wrapper's last job handle is observed
 * closed by the terminating caller.
 */
export const TERMINATE_JOB_WATCHER_COMMAND = String.raw`
$ErrorActionPreference = 'Stop'
$ownerPidText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_OWNER_PID')
$ownerCreatedAtText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_OWNER_CREATED_AT')
$targetJobName = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_JOB_NAME')
$watcherLog = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_LOG')
$watcherReadyPath = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_READY_PATH')
$watcherReadyNonce = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_READY_NONCE')
$watcherDeadlineAtText = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_DEADLINE_AT')
function Write-WatcherLog([string]$message) {
    if ([string]::IsNullOrWhiteSpace($watcherLog)) { return }
    try { [IO.File]::AppendAllText($watcherLog, ($message + [Environment]::NewLine)) } catch {}
}
function Write-WatcherReady([string]$value) {
    if ([string]::IsNullOrWhiteSpace($watcherReadyPath) -or [string]::IsNullOrWhiteSpace($watcherReadyNonce)) { return }
    $tempPath = $watcherReadyPath + '.' + $PID + '.tmp'
    try {
        [IO.File]::WriteAllText($tempPath, $value)
        [IO.File]::Move($tempPath, $watcherReadyPath)
    } catch {
        try { Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue } catch {}
    }
}
$ownerPid = 0
$ownerCreatedAt = 0.0
$watcherDeadlineAt = 0L
[long]::TryParse($watcherDeadlineAtText, [ref]$watcherDeadlineAt) | Out-Null
[double]::TryParse($ownerCreatedAtText, [ref]$ownerCreatedAt) | Out-Null
if (
    -not [int]::TryParse($ownerPidText, [ref]$ownerPid) -or
    $ownerCreatedAt -le 0 -or
    [string]::IsNullOrWhiteSpace($targetJobName) -or
    [string]::IsNullOrWhiteSpace($watcherReadyPath) -or
    [string]::IsNullOrWhiteSpace($watcherReadyNonce) -or
    $watcherDeadlineAt -le 0
) {
    Write-WatcherLog 'invalid-input'
    exit 87
}
Write-WatcherLog ('started owner=' + $ownerPid + ' job=' + $targetJobName)
Write-WatcherLog ('waiting owner=' + $ownerPid)
try {
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
public static class HermesTerminateWatch {
    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    private const uint SYNCHRONIZE = 0x00100000;
    private const uint JOB_OBJECT_TERMINATE = 0x0008;
    private const uint JOB_OBJECT_QUERY = 0x0004;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private const uint WAIT_FAILED = 0xFFFFFFFF;
    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime { public uint Low; public uint High; }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(IntPtr process, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenJobObject(uint desiredAccess, bool inheritHandle, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    private static long NowUnixMilliseconds() {
        return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    }
    private static uint Remaining(long deadlineAt) {
        long remaining = deadlineAt - NowUnixMilliseconds();
        if (remaining <= 0) return 0;
        return (uint)Math.Min(remaining, 0xFFFFFFFEL);
    }
    private static double ToUnixSeconds(FileTime time) {
        long fileTicks = ((long)time.High << 32) | time.Low;
        return (fileTicks - 116444736000000000L) / 10000000.0;
    }
    public static int ReadCreatedAt(IntPtr process, out double createdAt) {
        createdAt = 0;
        FileTime creation, exit, kernel, user;
        if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) {
            int error = Marshal.GetLastWin32Error();
            return error == 0 ? -1 : -error;
        }
        createdAt = ToUnixSeconds(creation);
        return 0;
    }
    private static IntPtr OpenTargetJobUntilDeadline(string jobName, long deadlineAt, out int error) {
        error = 0;
        while (true) {
            IntPtr job = OpenJobObject(JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY | SYNCHRONIZE, false, jobName);
            if (job != IntPtr.Zero) return job;
            error = Marshal.GetLastWin32Error();
            if (error != 2 && error != 6) return IntPtr.Zero;
            uint remaining = Remaining(deadlineAt);
            if (remaining == 0) {
                error = (int)WAIT_TIMEOUT;
                return IntPtr.Zero;
            }
            Thread.Sleep((int)Math.Min(10U, remaining));
        }
    }
    private static int Failure(string readyPath, string readyNonce, string stage, int error) {
        TryWriteReady(readyPath, "FAILED:" + readyNonce + " " + stage + " win32=" + error);
        return error == 0 ? -1 : -error;
    }
    private static bool TryWriteReady(string path, string value) {
        if (String.IsNullOrWhiteSpace(path)) return false;
        string tempPath = path + "." + System.Diagnostics.Process.GetCurrentProcess().Id + ".tmp";
        try {
            using (var stream = new FileStream(tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            using (var writer = new StreamWriter(stream)) {
                writer.Write(value);
                writer.Flush();
            }
            File.Move(tempPath, path);
            return true;
        } catch {
            try { File.Delete(tempPath); } catch {}
            return false;
        }
    }
    public static int WaitForOwnerThenTerminate(int ownerPid, double expectedOwnerCreatedAt, string jobName, string readyPath, string readyNonce, long deadlineAt) {
        IntPtr owner = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, ownerPid);
        if (owner == IntPtr.Zero) {
            return Failure(readyPath, readyNonce, "owner-open", Marshal.GetLastWin32Error());
        }
        double actualOwnerCreatedAt = 0;
        int ownerGenerationCode = ReadCreatedAt(owner, out actualOwnerCreatedAt);
        if (ownerGenerationCode != 0) {
            CloseHandle(owner);
            return Failure(readyPath, readyNonce, "owner-generation", -ownerGenerationCode);
        }
        if (Math.Abs(actualOwnerCreatedAt - expectedOwnerCreatedAt) > 1.5) {
            CloseHandle(owner);
            return Failure(readyPath, readyNonce, "owner-generation", 0x10001);
        }
        int openJobError = 0;
        IntPtr job = OpenTargetJobUntilDeadline(jobName, deadlineAt, out openJobError);
        if (job == IntPtr.Zero) {
            CloseHandle(owner);
            return Failure(readyPath, readyNonce, "job-open", openJobError);
        }
        try {
            if (!TryWriteReady(readyPath, "ARMED:" + readyNonce)) return Failure(readyPath, readyNonce, "ready-write", 1);
            uint ownerRemaining = Remaining(deadlineAt);
            if (ownerRemaining == 0) return Failure(readyPath, readyNonce, "owner-deadline", (int)WAIT_TIMEOUT);
            uint ownerWait = WaitForSingleObject(owner, ownerRemaining);
            if (ownerWait == WAIT_TIMEOUT) return Failure(readyPath, readyNonce, "owner-wait", (int)WAIT_TIMEOUT);
            if (ownerWait == WAIT_FAILED) return Failure(readyPath, readyNonce, "owner-wait", Marshal.GetLastWin32Error());
            if (!TerminateJobObject(job, 1)) {
                return Failure(readyPath, readyNonce, "job-terminate", Marshal.GetLastWin32Error());
            }
            uint jobRemaining = Remaining(deadlineAt);
            if (jobRemaining == 0) return Failure(readyPath, readyNonce, "job-deadline", (int)WAIT_TIMEOUT);
            uint result = WaitForSingleObject(job, Math.Min(1500U, jobRemaining));
            if (result == WAIT_OBJECT_0) return 0;
            return Failure(readyPath, readyNonce, "job-wait", result == WAIT_FAILED ? Marshal.GetLastWin32Error() : (int)result);
        } finally {
            CloseHandle(job);
            CloseHandle(owner);
        }
    }
}
'@ -ErrorAction Stop
  $watchResult = [HermesTerminateWatch]::WaitForOwnerThenTerminate($ownerPid, $ownerCreatedAt, $targetJobName, $watcherReadyPath, $watcherReadyNonce, $watcherDeadlineAt)
  Write-WatcherLog ('completed result=' + $watchResult)
  if ($watchResult -ne 0) {
    Write-WatcherReady ('FAILED:' + $watcherReadyNonce + ' watcher-result=' + $watchResult)
  }
  exit $watchResult
} catch {
  Write-WatcherLog ('exception=' + [string]$_.Exception.Message)
  Write-WatcherReady ('FAILED:' + $watcherReadyNonce + ' exception=' + [string]$_.Exception.Message)
  exit 1
}
`.trim()

const TERMINATE_JOB_WATCHER_BOOTSTRAP = String.raw`
$watcherScript = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_WATCHER_SCRIPT')
if ([string]::IsNullOrWhiteSpace($watcherScript)) { exit 87 }
& ([ScriptBlock]::Create($watcherScript))
`.trim()

export const TERMINATE_JOB_WATCHER_BRIDGE = String.raw`
const { spawnSync } = require('node:child_process')
const executable = process.env.HERMES_TERMINATE_WATCHER_POWERSHELL
const encodedCommand = process.env.HERMES_TERMINATE_WATCHER_ENCODED_COMMAND
if (!executable || !encodedCommand) process.exit(87)
const result = spawnSync(
  executable,
  ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encodedCommand],
  { detached: false, windowsHide: true, stdio: 'ignore', env: process.env }
)
process.exit(Number.isInteger(result.status) ? result.status : 1)
`.trim()

export type ProcessIdentity = { pid: number; createdAt?: number }

export function parseWrapperProcessMarker(value: string, expectedNonce: string): ProcessIdentity | null {
  const match = /^PID:(\d+);CREATED_AT_MS:(\d+);NONCE:([A-Za-z0-9_-]+)$/.exec(String(value ?? '').trim())
  if (!match || match[3] !== expectedNonce) return null
  const pid = Number(match[1])
  const createdAtMs = Number(match[2])
  if (!Number.isSafeInteger(pid) || pid <= 0 || !Number.isSafeInteger(createdAtMs) || createdAtMs <= 0) return null
  return { pid, createdAt: createdAtMs / 1_000 }
}

async function waitForWrapperProcessMarker(
  markerPath: string,
  markerNonce: string,
  deadlineAt: number
): Promise<ProcessIdentity | null> {
  while (Date.now() < deadlineAt) {
    try {
      if (fs.existsSync(markerPath)) {
        const identity = parseWrapperProcessMarker(fs.readFileSync(markerPath, 'utf8'), markerNonce)
        if (identity && identity.createdAt != null) return identity
      }
    } catch {
      // A partial or stale marker is not an authenticated wrapper identity.
    }
    const remaining = Math.max(0, deadlineAt - Date.now())
    if (remaining <= 0) break
    await new Promise(resolve => setTimeout(resolve, Math.min(WRAPPER_MARKER_POLL_MS, remaining)))
  }
  return null
}

function readWrapperPhaseDiagnostics(phasePath: string, phaseNonce: string): string {
  const summary = WRAPPER_PHASE_NAMES.map(phase => {
    const markerPath = `${phasePath}.${phase}.marker`
    const expectedPrefix = `PHASE:${phaseNonce};NAME:${phase};TICKS:`
    try {
      if (!fs.existsSync(markerPath)) return `${phase}=missing`
      const value = fs.readFileSync(markerPath, 'utf8').trim().replace(/[\r\n]+/g, ' ')
      return value.startsWith(expectedPrefix) ? `${phase}=${value.slice(0, 512)}` : `${phase}=invalid`
    } catch {
      return `${phase}=unreadable`
    }
  })
  return `wrapper-phases ${summary.join(' ')}`.slice(0, 4_096)
}

function hasSuccessfulWrapperReceipt(phasePath: string, phaseNonce: string): boolean {
  const readPhase = (phase: (typeof WRAPPER_PHASE_NAMES)[number]): string | undefined => {
    try {
      const value = fs.readFileSync(`${phasePath}.${phase}.marker`, 'utf8').trim()
      const expectedPrefix = `PHASE:${phaseNonce};NAME:${phase};TICKS:`
      return value.startsWith(expectedPrefix) ? value : undefined
    } catch {
      return undefined
    }
  }
  const innerExited = readPhase('inner-exited')
  return Boolean(
    innerExited?.endsWith(';DETAIL:code=0') &&
      readPhase('target-terminated') &&
      readPhase('handles-closed')
  )
}

function readExactWatcherReadyValue(watcherReadyPath: string): string | undefined {
  try {
    if (!fs.existsSync(watcherReadyPath)) return undefined
    return fs.readFileSync(watcherReadyPath, 'utf8').trim().replace(/[\r\n]+/g, ' ').slice(0, 512)
  } catch {
    return undefined
  }
}

async function readProcessCreatedAt(
  pid: number,
  timeoutMs = 2_000
): Promise<number | null> {
  if (!Number.isInteger(pid) || pid <= 0) return null
  const budget = Math.trunc(timeoutMs)
  if (budget <= 0) return null
  if (process.platform !== 'win32') return null
  try {
    const { stdout } = await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `$p = Get-Process -Id ${Math.trunc(pid)} -ErrorAction Stop; [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()`
      ],
      { encoding: 'utf8', windowsHide: true, timeout: budget }
    )
    const value = Number(String(stdout).trim())
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

export type ProcessLiveness = 'live' | 'absent' | 'unknown'

/**
 * Discriminated liveness probe outcome.
 * Only authenticated child-process exits may prove live/absent.
 * Error metadata (timeout/access/spawn) is never treated as an exit code.
 */
export type LivenessProbeResult =
  | { kind: 'exit'; code: number }
  | { kind: 'error'; code?: string | number; message?: string }

/** Injectable runner for process liveness probes (tests inject fakes). */
export type LivenessProbeRunner = (
  pid: number,
  timeoutMs: number
) => Promise<LivenessProbeResult>

type DeadlineProbeResult<T> =
  | { completed: true; value: T }
  | { completed: false }

/**
 * Resolve a read-only probe only while the shared deadline remains. The
 * underlying native operation is deliberately consumed after a timeout so a
 * late rejection cannot become an unhandled promise, but its value is never
 * allowed back into the mutation boundary.
 */
async function resolveBeforeDeadline<T>(
  operation: () => Promise<T>,
  deadlineAt: number
): Promise<DeadlineProbeResult<T>> {
  const remaining = Math.max(0, deadlineAt - Date.now())
  if (remaining <= 0) return { completed: false }

  const pending = Promise.resolve().then(operation)
  let timer: NodeJS.Timeout | undefined
  const timeout = new Promise<DeadlineProbeResult<T>>(resolve => {
    timer = setTimeout(() => resolve({ completed: false }), Math.max(1, remaining - 1))
  })

  try {
    const outcome = await Promise.race([
      pending.then(value => ({ completed: true as const, value })),
      timeout
    ])
    if (!outcome.completed || Date.now() > deadlineAt) return { completed: false }
    return outcome
  } catch {
    return { completed: false }
  } finally {
    if (timer) clearTimeout(timer)
    void pending.catch(() => undefined)
  }
}

/**
 * Pure classification of a liveness probe outcome.
 * live only for kind=exit/code=0; absent only for kind=exit/code=3.
 * Every kind=error is unknown regardless of embedded code metadata.
 */
export function classifyLivenessProbeResult(result: LivenessProbeResult): ProcessLiveness {
  if (result.kind === 'exit') {
    if (result.code === 0) return 'live'
    if (result.code === 3) return 'absent'
    return 'unknown'
  }
  // kind=error: timeout/access/spawn/malformed — never prove absence from error metadata.
  return 'unknown'
}

function execFileFailureToLivenessResult(error: any): LivenessProbeResult {
  // Timeout/killed probes are errors even if a numeric code is present.
  if (error?.killed === true || error?.signal) {
    return {
      kind: 'error',
      code: typeof error?.code === 'string' || typeof error?.code === 'number' ? error.code : undefined,
      message: String(error?.message ?? error)
    }
  }
  // Node execFile puts authenticated child exit status on error.code as a number.
  if (typeof error?.code === 'number') {
    return { kind: 'exit', code: error.code }
  }
  // Some paths expose exit status on .status while .code is a string errno.
  if (typeof error?.status === 'number') {
    return { kind: 'exit', code: error.status }
  }
  return {
    kind: 'error',
    code: typeof error?.code === 'string' || typeof error?.code === 'number' ? error.code : undefined,
    message: String(error?.message ?? error)
  }
}

async function defaultLivenessProbeRunner(
  pid: number,
  timeoutMs: number
): Promise<LivenessProbeResult> {
  if (process.platform !== 'win32') {
    try {
      process.kill(pid, 0)
      return { kind: 'exit', code: 0 }
    } catch (error: any) {
      // ESRCH is an authenticated "no such process" from the kill(2) probe.
      if (error?.code === 'ESRCH') return { kind: 'exit', code: 3 }
      return {
        kind: 'error',
        code: error?.code,
        message: String(error?.message ?? error)
      }
    }
  }
  try {
    await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        // Exit 0 = live, 3 = explicitly not found, anything else = unknown.
        `$p = Get-Process -Id ${Math.trunc(pid)} -ErrorAction SilentlyContinue; if ($null -ne $p) { exit 0 } else { exit 3 }`
      ],
      { windowsHide: true, timeout: timeoutMs }
    )
    return { kind: 'exit', code: 0 }
  } catch (error: any) {
    return execFileFailureToLivenessResult(error)
  }
}

export async function probeProcessLiveness(
  pid: number,
  timeoutMs = 2_000,
  runner: LivenessProbeRunner = defaultLivenessProbeRunner
): Promise<ProcessLiveness> {
  if (!Number.isInteger(pid) || pid <= 0) return 'absent'
  const budget = Math.trunc(timeoutMs)
  if (budget <= 0) return 'unknown'
  const result = await runner(pid, budget)
  return classifyLivenessProbeResult(result)
}

export async function identitiesStillPresent(
  identities: readonly ProcessIdentity[],
  {
    deadlineAt,
    livenessRunner,
    readCreatedAt
  }: {
    deadlineAt?: number
    livenessRunner?: LivenessProbeRunner
    readCreatedAt?: (pid: number, timeoutMs: number) => Promise<number | null>
  } = {}
): Promise<ProcessIdentity[]> {
  const survivors: ProcessIdentity[] = []
  const absoluteDeadline =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt) ? deadlineAt : Date.now() + 2_000
  const remaining = () => Math.max(0, absoluteDeadline - Date.now())
  const runner = livenessRunner ?? defaultLivenessProbeRunner
  const createdAtReader = readCreatedAt ?? readProcessCreatedAt

  for (const identity of identities) {
    const left = remaining()
    if (left <= 0) {
      // No time for an absence probe: keep known/unknown identities as survivors.
      survivors.push(identity)
      continue
    }
    const slice = Math.max(1, Math.floor(left / Math.max(1, identities.length - survivors.length)))
    const createdAtResult = await resolveBeforeDeadline(
      () => createdAtReader(identity.pid, slice),
      absoluteDeadline
    )
    if (!createdAtResult.completed) {
      survivors.push(identity)
      continue
    }
    const createdAt = createdAtResult.value
    if (createdAt == null) {
      // Do not infer absence from a null create-time read. Probe liveness with
      // remaining budget; only explicit not-found proves absence.
      const liveLeft = remaining()
      if (liveLeft <= 0) {
        survivors.push(identity)
        continue
      }
      const livenessResult = await resolveBeforeDeadline(
        () => probeProcessLiveness(identity.pid, liveLeft, runner),
        absoluteDeadline
      )
      if (!livenessResult.completed || livenessResult.value !== 'absent') {
        // live, unknown, or an expired probe => survivor
        survivors.push(identity)
      }
      continue
    }
    // Unknown generation: any completed create-time read is still only
    // identity evidence; a matching live generation remains a survivor.
    if (identity.createdAt == null || !Number.isFinite(identity.createdAt)) {
      survivors.push({ pid: identity.pid, createdAt })
      continue
    }
    if (Math.abs(createdAt - identity.createdAt) <= 1.5) {
      survivors.push(identity)
    }
  }
  return survivors
}

/**
 * Snapshot a Windows process tree (root + descendants) as PID + create-time
 * identities so post-kill verification can detect PID reuse.
 */
export async function snapshotProcessTreeIdentities(
  rootPid: number,
  {
    timeoutMs = 1_500,
    deadlineAt
  }: { timeoutMs?: number; deadlineAt?: number } = {}
): Promise<ProcessIdentity[]> {
  if (!Number.isInteger(rootPid) || rootPid <= 0) return []
  const requestedBudget = Math.max(0, Math.trunc(timeoutMs))
  const remainingBudget =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? Math.max(0, Math.trunc(deadlineAt - Date.now()))
      : requestedBudget
  const budget = Math.min(requestedBudget, remainingBudget)
  // A zero/negative caller budget is a hard no-probe condition on every
  // platform. In particular, do not fall through to a fresh default-timeout
  // probe after the shared deadline is exhausted.
  if (budget <= 0) {
    return [{ pid: rootPid }]
  }
  if (process.platform !== 'win32') {
    const createdAt = await readProcessCreatedAt(rootPid, budget)
    return createdAt == null ? [] : [{ pid: rootPid, createdAt }]
  }

  const startedAt = Date.now()
  try {
    const { stdout } = await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `
$ErrorActionPreference = 'Stop'
$root = ${Math.trunc(rootPid)}
$ids = New-Object 'System.Collections.Generic.List[int]'
function Add-Tree([int]$pidVal) {
  if ($pidVal -le 0 -or $ids.Contains($pidVal)) { return }
  $ids.Add($pidVal) | Out-Null
  Get-CimInstance Win32_Process -Filter ("ParentProcessId = $pidVal") -ErrorAction SilentlyContinue | ForEach-Object {
    Add-Tree ([int]$_.ProcessId)
  }
}
Add-Tree $root
$rows = @()
foreach ($id in $ids) {
  try {
    $p = Get-Process -Id $id -ErrorAction Stop
    $created = [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
    $rows += ("{0}|{1}" -f $id, $created)
  } catch {}
}
$rows -join ';'
`.trim()
      ],
      { encoding: 'utf8', windowsHide: true, timeout: budget }
    )
    const identities: ProcessIdentity[] = []
    for (const part of String(stdout || '')
      .trim()
      .split(';')
      .filter(Boolean)) {
      const [pidText, createdText] = part.split('|')
      const pid = Number(pidText)
      const createdAt = Number(createdText)
      if (Number.isInteger(pid) && pid > 0 && Number.isFinite(createdAt) && createdAt > 0) {
        identities.push({ pid, createdAt })
      }
    }
    return identities
  } catch {
    // If the tree query failed, spend only the caller's remaining slice on an
    // exact root-generation read; never fall back to readProcessCreatedAt's
    // independent two-second default and overrun the absolute deadline.
    const elapsed = Date.now() - startedAt
    const fallbackBudget = Math.max(0, Math.min(budget, elapsed >= budget ? 0 : budget - elapsed))
    if (fallbackBudget <= 0) return [{ pid: rootPid }]
    const createdAt = await readProcessCreatedAt(rootPid, fallbackBudget)
    return createdAt == null ? [{ pid: rootPid }] : [{ pid: rootPid, createdAt }]
  }
}

async function runTaskkillWithinDeadline(
  pid: number,
  deadlineAt: number
): Promise<{ completed: boolean; succeeded: boolean }> {
  const remaining = Math.max(0, deadlineAt - Date.now())
  if (remaining <= 0) return { completed: false, succeeded: false }

  return await new Promise(resolve => {
    let settled = false
    let timer: NodeJS.Timeout | undefined
    const finish = (result: { completed: boolean; succeeded: boolean }) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(result)
    }
    const child = execFile(
      'taskkill',
      ['/PID', String(pid), '/T', '/F'],
      {
        windowsHide: true,
        timeout: Math.max(1, remaining - 1),
        killSignal: 'SIGKILL'
      },
      (error: any) => finish({ completed: true, succeeded: !error })
    )
    child.once('error', () => finish({ completed: true, succeeded: false }))
    timer = setTimeout(() => {
      // This command only supervises the already-contained mutation child.
      // If taskkill itself cannot settle in time, stop waiting and fail closed;
      // never report a confirmed tree after an unbounded native command.
      try {
        child.kill('SIGKILL')
      } catch {
        void 0
      }
      finish({ completed: false, succeeded: false })
    }, Math.max(1, remaining - 1))
  })
}

/**
 * Kill a Windows process tree and wait until every pre-captured identity is gone
 * or reused. Uses one absolute deadline shared by taskkill + identity polling.
 * Returns confirmed=false when any identity remains — callers must treat that as
 * a hard terminal-boundary failure, not a settled success.
 */
export async function killProcessTreeAndAwaitGone(
  pid: number,
  {
    confirmMs = TERMINATE_KILL_CONFIRM_MS,
    pollMs = 50,
    preSnapshot,
    deadlineAt,
    snapshotProcessTree,
    readCreatedAt
  }: {
    confirmMs?: number
    pollMs?: number
    preSnapshot?: readonly ProcessIdentity[]
    /** Absolute Date.now() deadline; overrides confirmMs window when provided. */
    deadlineAt?: number
    /** Injectable only to force snapshot failure in the real boundary canary. */
    snapshotProcessTree?: typeof snapshotProcessTreeIdentities
    /** Injectable create-time reader for deadline regressions. */
    readCreatedAt?: (pid: number, timeoutMs: number) => Promise<number | null>
  } = {}
): Promise<{ confirmed: boolean; identities: ProcessIdentity[]; survivors: ProcessIdentity[] }> {
  if (!Number.isInteger(pid) || pid <= 0) {
    return { confirmed: true, identities: [], survivors: [] }
  }

  const absoluteDeadline =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? deadlineAt
      : Date.now() + Math.max(0, Math.trunc(confirmMs))

  const remaining = () => Math.max(0, absoluteDeadline - Date.now())
  if (remaining() <= 0) {
    // No time for probes: fail closed with known/unknown identities as survivors.
    const identities =
      preSnapshot && preSnapshot.length > 0 ? [...preSnapshot] : [{ pid }]
    return { confirmed: false, identities, survivors: identities }
  }

  const snapshot = snapshotProcessTree ?? snapshotProcessTreeIdentities
  let captured: ProcessIdentity[] = []
  let snapshotTimedOut = false
  if (preSnapshot && preSnapshot.length > 0) {
    captured = [...preSnapshot]
  } else {
    const snapshotBudget = remaining()
    if (snapshotBudget <= 0) {
      return { confirmed: false, identities: [{ pid }], survivors: [{ pid }] }
    }
    const snapshotPromise = Promise.resolve().then(() =>
      snapshot(pid, {
        timeoutMs: Math.min(500, snapshotBudget),
        deadlineAt: absoluteDeadline
      })
    )
    let snapshotTimer: NodeJS.Timeout | undefined
    try {
      // Native execFile has its own timeout, but keep the boundary safe even
      // if an injected/native adapter ignores that option. Snapshotting is
      // read-only, so abandoning its late result cannot leave mutation work.
      captured = await Promise.race([
        snapshotPromise,
        new Promise<ProcessIdentity[]>(resolve => {
          snapshotTimer = setTimeout(() => {
            snapshotTimedOut = true
            resolve([])
          }, Math.max(1, snapshotBudget - 1))
        })
      ])
    } catch {
      // Snapshot failure is intentionally represented by the unknown root.
      // The caller's Job Object boundary, when present, is the hard fallback.
      captured = []
    } finally {
      if (snapshotTimer) clearTimeout(snapshotTimer)
    }
    // A timed-out adapter may settle later; consume its rejection without
    // extending this boundary or creating an unhandled-rejection side effect.
    void snapshotPromise.catch(() => undefined)
    if (snapshotTimedOut) {
      // The shared deadline was consumed by snapshotting. Do not launch a
      // second native probe for a fabricated/fresh root generation.
      captured = []
    }
  }
  const identities = captured
  const createdAtReader = readCreatedAt ?? readProcessCreatedAt

  // Always include the root identity. Prefer a real create-time; if unavailable,
  // keep an unknown-generation identity (createdAt omitted) so liveness alone
  // keeps confirmation false while the PID lives.
  if (!identities.some(entry => entry.pid === pid)) {
    const left = remaining()
    let createdAt: number | null = null
    if (!snapshotTimedOut && left > 0) {
      const rootResult = await resolveBeforeDeadline(
        () => createdAtReader(pid, left),
        absoluteDeadline
      )
      if (rootResult.completed) createdAt = rootResult.value
    }
    identities.push(createdAt != null ? { pid, createdAt } : { pid })
  }

  const taskkillBudget = Math.min(remaining(), Math.max(0, Math.trunc(confirmMs)))
  if (taskkillBudget > 0) {
    const taskkillResult = await runTaskkillWithinDeadline(pid, absoluteDeadline)
    if (!taskkillResult.completed) {
      return { confirmed: false, identities, survivors: identities }
    }
    if (!taskkillResult.succeeded) {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        void 0
      }
    }
  }

  // Also hard-kill every known identity in case /T missed a detached Start-Process child.
  for (const identity of identities) {
    const left = remaining()
    if (left <= 0) break
    if (identity.pid === pid) continue
    const taskkillResult = await runTaskkillWithinDeadline(identity.pid, absoluteDeadline)
    if (!taskkillResult.completed) {
      return { confirmed: false, identities, survivors: identities }
    }
    if (!taskkillResult.succeeded) {
      try {
        process.kill(identity.pid, 'SIGKILL')
      } catch {
        void 0
      }
    }
  }

  if (remaining() <= 0) {
    return { confirmed: false, identities, survivors: identities }
  }

  let survivors = await identitiesStillPresent(identities, { deadlineAt: absoluteDeadline })
  while (survivors.length > 0 && remaining() > 0) {
    await new Promise(resolve => setTimeout(resolve, Math.max(1, Math.min(pollMs, remaining()))))
    if (remaining() <= 0) break
    survivors = await identitiesStillPresent(identities, { deadlineAt: absoluteDeadline })
  }

  if (remaining() <= 0 && survivors.length > 0) {
    return { confirmed: false, identities, survivors }
  }

  return {
    confirmed: survivors.length === 0,
    identities,
    survivors
  }
}

function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode != null || child.signalCode != null) {
    return Promise.resolve(true)
  }
  return new Promise(resolve => {
    let settled = false
    const done = (ok: boolean) => {
      if (settled) return
      settled = true
      resolve(ok)
    }
    const timer = setTimeout(() => done(false), Math.max(1, timeoutMs))
    child.once('exit', () => {
      clearTimeout(timer)
      done(true)
    })
    child.once('error', () => {
      clearTimeout(timer)
      done(false)
    })
  })
}

async function terminateNamedTargetJobWithinDeadline(
  targetJobName: string,
  deadlineAt: number,
  maxBudgetMs = 1_000
): Promise<boolean> {
  if (process.platform !== 'win32' || !targetJobName) return false
  const remaining = Math.max(0, deadlineAt - Date.now())
  const budget = Math.min(Math.max(0, Math.trunc(maxBudgetMs)), remaining)
  if (budget <= 0) return false
  try {
    await execFileAsync(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        TERMINATE_NAMED_JOB_COMMAND
      ],
      {
        windowsHide: true,
        timeout: Math.max(1, budget - 1),
        env: {
          ...process.env,
          HERMES_TERMINATE_TARGET_JOB_NAME: targetJobName,
          HERMES_TERMINATE_TARGET_WAIT_MS: String(Math.max(0, budget - 100))
        }
      }
    )
    return true
  } catch {
    return false
  }
}

function startTargetJobWatcher(
  ownerPid: number,
  ownerCreatedAt: number,
  targetJobName: string,
  watcherReadyPath: string,
  watcherReadyNonce: string,
  deadlineAt: number
): ReturnType<StartTargetJobWatcher> {
  if (process.platform !== 'win32' || !Number.isInteger(ownerPid) || ownerPid <= 0) return undefined
  const encodedWatcherCommand = Buffer.from(TERMINATE_JOB_WATCHER_BOOTSTRAP, 'utf16le').toString('base64')
  try {
    const watcher = spawn(
      process.execPath,
      ['-e', TERMINATE_JOB_WATCHER_BRIDGE],
      {
        windowsHide: true,
        detached: true,
        stdio: 'ignore',
        env: {
          ...process.env,
          ELECTRON_RUN_AS_NODE: '1',
          HERMES_TERMINATE_WATCHER_POWERSHELL: powershellExecutable(),
          HERMES_TERMINATE_WATCHER_ENCODED_COMMAND: encodedWatcherCommand,
          HERMES_TERMINATE_OWNER_PID: String(ownerPid),
          HERMES_TERMINATE_OWNER_CREATED_AT: String(ownerCreatedAt),
          HERMES_TERMINATE_TARGET_JOB_NAME: targetJobName,
          HERMES_TERMINATE_WATCHER_READY_PATH: watcherReadyPath,
          HERMES_TERMINATE_WATCHER_READY_NONCE: watcherReadyNonce,
          // The watcher body was originally authored for direct Windows argv
          // transport, where escaped C# quotes are consumed before PowerShell
          // parses the here-string. ScriptBlock/environment transport preserves
          // those backslashes, so normalize only escaped double quotes here.
          HERMES_TERMINATE_WATCHER_SCRIPT: TERMINATE_JOB_WATCHER_COMMAND.replace(/\\\"/g, '"'),
          HERMES_TERMINATE_WATCHER_DEADLINE_AT: String(Math.trunc(deadlineAt))
        }
      }
    )
    return watcher
  } catch {
    return undefined
  }
}

function boundedWatcherErrorMessage(message: unknown): string {
  return String(message ?? '')
    .replace(/[\r\n]+/g, ' ')
    .slice(0, 256)
}

function observeTargetJobWatcher(child: ChildProcess, spawnedAt: number): TargetJobWatcherDiagnostics {
  const diagnostics: TargetJobWatcherDiagnostics = {
    spawnedAt,
    pid: child.pid
  }
  child.on('error', error => {
    if (diagnostics.errorAt == null) diagnostics.errorAt = Date.now()
    if (diagnostics.errorCode == null) {
      const errorCode = (error as NodeJS.ErrnoException).code
      if (typeof errorCode === 'string' || typeof errorCode === 'number') diagnostics.errorCode = errorCode
    }
    if (!diagnostics.errorMessage) diagnostics.errorMessage = boundedWatcherErrorMessage(error?.message)
  })
  child.on('exit', (code, signal) => {
    if (diagnostics.exitAt == null) diagnostics.exitAt = Date.now()
    diagnostics.exitCode = code
    diagnostics.signalCode = signal
  })
  if (child.exitCode != null || child.signalCode != null) {
    diagnostics.exitAt = Date.now()
    diagnostics.exitCode = child.exitCode
    diagnostics.signalCode = child.signalCode
  }
  return diagnostics
}

function formatTargetJobWatcherDiagnostics(diagnostics?: TargetJobWatcherDiagnostics): string {
  if (!diagnostics) return 'watcher-diagnostics=none'
  const now = Date.now()
  const errorElapsedMs = diagnostics.errorAt == null ? 'none' : Math.max(0, diagnostics.errorAt - diagnostics.spawnedAt)
  const exitElapsedMs = diagnostics.exitAt == null ? 'none' : Math.max(0, diagnostics.exitAt - diagnostics.spawnedAt)
  const elapsedMs = diagnostics.exitAt != null
    ? exitElapsedMs
    : diagnostics.errorAt != null
      ? errorElapsedMs
      : Math.max(0, now - diagnostics.spawnedAt)
  return [
    `watcher-spawned-at=${diagnostics.spawnedAt}`,
    `watcher-pid=${diagnostics.pid ?? 'none'}`,
    `watcher-error-code=${diagnostics.errorCode ?? 'none'}`,
    `watcher-error-message=${JSON.stringify(diagnostics.errorMessage ?? '')}`,
    `watcher-exit-code=${diagnostics.exitCode ?? 'none'}`,
    `watcher-signal=${diagnostics.signalCode ?? 'none'}`,
    `watcher-error-elapsed-ms=${errorElapsedMs}`,
    `watcher-exit-elapsed-ms=${exitElapsedMs}`,
    `watcher-elapsed-ms=${elapsedMs}`
  ].join(' ')
}

function sanitizeBoundaryDiagnostics(value: unknown): string {
  const safeLines: string[] = []
  for (const rawLine of String(value ?? '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || /Command failed:/i.test(line)) continue
    if (/watcher-(?:spawned|exited|stalled|error|exit|signal|elapsed)/i.test(line)) {
      safeLines.push(line.slice(0, 1_200))
      continue
    }
    if (/^(?:TARGET_[A-Z0-9_ -]+|unconfirmed-tree-survivors:[0-9,]+|exit -?\d+|timeout|killed|aborted|deadline-exhausted)\b/i.test(line)) {
      safeLines.push(line.slice(0, 512))
    }
  }
  return [...new Set(safeLines)].slice(0, 8).join('\n')
}

function deadlineFailureDetail(stderr: unknown): string {
  const diagnostics = sanitizeBoundaryDiagnostics(stderr)
  return diagnostics ? `deadline-exhausted ${diagnostics}` : 'deadline-exhausted'
}

function sanitizeRunnerStderr(value: unknown): string {
  const stderr = String(value ?? '').replace(/\0/g, '')
  if (/Command failed:/i.test(stderr)) return sanitizeBoundaryDiagnostics(stderr)
  return stderr.slice(0, 4_096)
}

const TARGET_JOB_WATCHER_GRACE_MS = 400

type TargetJobWatcherResult = {
  terminal: boolean
  healthy: boolean
  targetBoundaryConfirmed: boolean
  detail: string
}

/**
 * Wait for the detached target-job watcher without allowing it to become an
 * unobserved mutator. A watcher that exits unsuccessfully or misses its short
 * settle window is killed and the named target job is terminated within the
 * same absolute deadline. The caller must not remove watcher artifacts until
 * this function reports terminal=true.
 */
async function waitForTargetJobWatcher(
  watcher: ChildProcess | undefined,
  targetJobName: string,
  deadlineAt: number,
  startupFailure?: string,
  diagnostics?: TargetJobWatcherDiagnostics,
  skipGrace = false
): Promise<TargetJobWatcherResult> {
  if (!watcher || !Number.isInteger(watcher.pid) || (watcher.pid as number) <= 0) {
    const remaining = Math.max(0, deadlineAt - Date.now())
    const targetJobAttempt =
      remaining > 0
        ? await terminateNamedTargetJobWithinDeadline(targetJobName, deadlineAt, Math.min(1_000, remaining))
        : false
    return {
      terminal: true,
      healthy: false,
      targetBoundaryConfirmed: targetJobAttempt,
      detail: `${startupFailure ?? 'watcher-missing'} ${formatTargetJobWatcherDiagnostics(diagnostics)} target-job-termination=${targetJobAttempt ? 'confirmed' : 'not-confirmed'}`
    }
  }

  const watcherPid = watcher.pid
  const settleBudget = skipGrace ? 0 : Math.min(TARGET_JOB_WATCHER_GRACE_MS, Math.max(0, deadlineAt - Date.now()))
  if (settleBudget > 0 && (await waitForChildExit(watcher, settleBudget))) {
    if (watcher.exitCode === 0 && watcher.signalCode == null) {
      return {
        terminal: true,
        healthy: true,
        targetBoundaryConfirmed: true,
        detail: `watcher-exited-cleanly ${formatTargetJobWatcherDiagnostics(diagnostics)}`
      }
    }
    const remaining = Math.max(0, deadlineAt - Date.now())
    const targetJobAttempt =
      remaining > 0
        ? await terminateNamedTargetJobWithinDeadline(targetJobName, deadlineAt, Math.min(1_000, remaining))
        : false
    return {
      terminal: true,
      healthy: false,
      targetBoundaryConfirmed: targetJobAttempt,
      detail: `watcher-exited-with-failure code=${String(watcher.exitCode)} signal=${String(watcher.signalCode)} ${formatTargetJobWatcherDiagnostics(diagnostics)} target-job-termination=${targetJobAttempt ? 'confirmed' : 'not-confirmed'}`
    }
  }

  // The watcher is stalled. Kill its exact process tree and close the named
  // target job in parallel, then observe the watcher exit before returning.
  const remaining = Math.max(0, deadlineAt - Date.now())
  const watcherTreeSnapshot =
    remaining > 0
      ? await snapshotProcessTreeIdentities(watcherPid, {
          timeoutMs: Math.min(300, remaining),
          deadlineAt
        })
      : []
  const targetJobTermination =
    remaining > 0
      ? terminateNamedTargetJobWithinDeadline(targetJobName, deadlineAt, Math.min(1_000, remaining))
      : Promise.resolve(false)
  try {
    watcher.kill('SIGKILL')
  } catch {
    void 0
  }
  const watcherTreeTermination = killProcessTreeAndAwaitGone(watcherPid, {
    confirmMs: Math.min(500, remaining),
    preSnapshot: watcherTreeSnapshot.length > 0 ? watcherTreeSnapshot : [{ pid: watcherPid }],
    deadlineAt
  })
  const [targetJobAttempt, watcherTreeResult] = await Promise.all([targetJobTermination, watcherTreeTermination])
  const remainingAfterKill = Math.max(0, deadlineAt - Date.now())
  const watcherExited =
    remainingAfterKill > 0
      ? await waitForChildExit(watcher, remainingAfterKill)
      : watcher.exitCode != null || watcher.signalCode != null

  return {
    // Native exact-generation absence is terminal proof even when Node has not
    // delivered the child `exit` event yet (for example while inherited stdio
    // handles finish closing).
    terminal: watcherExited || watcherTreeResult.confirmed,
    healthy: false,
    targetBoundaryConfirmed: targetJobAttempt,
    detail: `watcher-stalled tree-confirmed=${watcherTreeResult.confirmed} ${formatTargetJobWatcherDiagnostics(diagnostics)} target-job-termination=${targetJobAttempt ? 'confirmed' : 'not-confirmed'}`
  }
}

/**
 * Run PowerShell under a hard budget. On abort/timeout the child tree is killed
 * and the promise resolves only after every pre-captured tree identity is
 * confirmed gone. Confirmation failure is a hard boundary failure.
 */
async function defaultRunPowerShell(
  script: string,
  timeoutMs = 4_000,
  signal?: AbortSignal,
  deadlineAt?: number,
  dependencies?: HardBoundaryDependencies
): Promise<{ stdout: string; stderr: string; code: number; pid?: number }> {
  const requestedBudget = Math.max(0, Math.trunc(timeoutMs))
  const startedAt = Date.now()
  // Leave a small scheduler/IPC margin so the public return stays inside the
  // caller's hard wall-clock budget even when native callbacks settle late.
  const deadlineSafetyMarginMs = Math.min(100, Math.floor(requestedBudget / 10))
  const requestedDeadline =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? Math.min(Math.trunc(deadlineAt), startedAt + requestedBudget)
      : startedAt + requestedBudget
  const absoluteDeadline = Math.max(startedAt, requestedDeadline - deadlineSafetyMarginMs)
  const budget = Math.max(0, absoluteDeadline - startedAt)
  if (budget <= 0) {
    return { stdout: '', stderr: 'aborted', code: 1 }
  }
  if (signal?.aborted) {
    return { stdout: '', stderr: 'aborted', code: 1 }
  }

  // Reserve part of the budget for kill + confirmed absence of the whole tree.
  const killReserveMs = terminateKillReserveMs(budget)
  const wrapperDeadline = absoluteDeadline - killReserveMs
  // Keep a bounded outer tail for one final PID-not-found proof when Node's exit
  // event lags behind a successfully delivered watcher kill.
  const watcherSettleReserveMs = Math.min(250, Math.floor(killReserveMs / 4))
  const watcherDeadline = absoluteDeadline - watcherSettleReserveMs
  const runMs = Math.max(0, wrapperDeadline - startedAt)
  if (runMs <= 0) {
    return { stdout: '', stderr: 'deadline-exhausted', code: 1 }
  }
  const jobName = `HermesTerminateHelper-${randomBytes(16).toString('hex')}`
  const targetJobName = `HermesTerminateTarget-${randomBytes(16).toString('hex')}`
  const targetWaitMs = Math.max(0, Math.min(1_500, runMs - 100))
  const watcherStarter = dependencies?.startWatcher ?? startTargetJobWatcher
  const watcherReadyDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-terminate-watcher-'))
  const watcherReadyPath = path.join(watcherReadyDirectory, 'ready')
  const watcherReadyNonce = randomBytes(16).toString('hex')
  const wrapperPidMarkerPath = path.join(watcherReadyDirectory, 'wrapper.pid')
  const wrapperPidMarkerNonce = randomBytes(16).toString('hex')
  const wrapperPhasePath = path.join(watcherReadyDirectory, 'wrapper.phase')
  const wrapperPhaseNonce = randomBytes(16).toString('hex')
  const helperScriptPath = path.join(watcherReadyDirectory, 'helper.ps1')
  const helperGatePath = path.join(watcherReadyDirectory, 'helper.go')

  return await new Promise(resolve => {
    let settled = false
    let childPid: number | undefined
    let treeSnapshot: ProcessIdentity[] = []
    let killing = false
    let terminalizing: Promise<void> | null = null
    let childProcess: ChildProcess | undefined
    let wrapperProcessIdentity: ProcessIdentity | undefined
    let wrapperMarkerSetup: Promise<void> | undefined
    let watcherProcess: ChildProcess | undefined
    let watcherDiagnostics: TargetJobWatcherDiagnostics | undefined
    let watcherStartupFailure: string | undefined

    const watcherTempPath = () =>
      Number.isInteger(watcherProcess?.pid) && (watcherProcess?.pid as number) > 0
        ? `${watcherReadyPath}.${watcherProcess?.pid}.tmp`
        : undefined
    const cleanupWatcherArtifacts = () => {
      // The watcher writes only these exact paths. Never recursively remove the
      // temporary directory, because a late or unrelated file must not be swept.
      const exactPaths = [
        watcherReadyPath,
        watcherTempPath(),
        wrapperPidMarkerPath,
        `${wrapperPidMarkerPath}.tmp`,
        helperScriptPath,
        helperGatePath,
        ...WRAPPER_PHASE_NAMES.flatMap(phase => [
          `${wrapperPhasePath}.${phase}.marker`,
          `${wrapperPhasePath}.${phase}.marker.tmp`
        ])
      ].filter(
        (entry): entry is string => typeof entry === 'string'
      )
      for (const exactPath of exactPaths) {
        try {
          fs.rmSync(exactPath, { force: true })
        } catch {
          void 0
        }
      }
      try {
        fs.rmdirSync(watcherReadyDirectory)
      } catch {
        // A non-empty directory is intentionally retained rather than removed
        // recursively. The caller still receives the terminal failure.
        void 0
      }
    }

    const finish = async (result: { stdout: string; stderr: string; code: number; pid?: number }) => {
      if (settled || terminalizing) return
      terminalizing = (async () => {
        let finalResult = { ...result, pid: childPid }
        let watcherTerminal = false
        const mustKill = killing || signal?.aborted === true

        if (wrapperMarkerSetup) await wrapperMarkerSetup

        const watcherReadyAtFinish = readExactWatcherReadyValue(watcherReadyPath)
        const watcherUnauthenticated = watcherReadyAtFinish !== `ARMED:${watcherReadyNonce}`
        if (watcherUnauthenticated && watcherProcess?.exitCode == null && watcherProcess?.signalCode == null) {
          try {
            watcherProcess?.kill('SIGKILL')
          } catch {
            void 0
          }
        }
        const watcherPromise = waitForTargetJobWatcher(
          watcherProcess,
          targetJobName,
          absoluteDeadline,
          watcherStartupFailure,
          watcherDiagnostics,
          watcherReadyAtFinish !== `ARMED:${watcherReadyNonce}`
        )
        const killPromise =
          mustKill && typeof childPid === 'number'
            ? killProcessTreeAndAwaitGone(childPid, {
                confirmMs: killReserveMs,
                preSnapshot: treeSnapshot,
                deadlineAt: absoluteDeadline
              })
            : Promise.resolve(undefined)
        const watcherResult = await watcherPromise
        let killResult = await killPromise
        watcherTerminal = watcherResult.terminal
        if (
          !watcherTerminal &&
          Number.isInteger(watcherProcess?.pid) &&
          (watcherProcess?.pid as number) > 0 &&
          Date.now() < absoluteDeadline
        ) {
          const watcherSurvivors = await identitiesStillPresent([{ pid: watcherProcess?.pid as number }], {
            deadlineAt: absoluteDeadline
          })
          watcherTerminal = watcherSurvivors.length === 0
        }
        // The bounded native probe can consume the outer tail on Windows. An
        // exit/signal event delivered while that probe was in flight is also
        // terminal evidence; do not confuse kill() delivery (`killed`) with this
        // completed child-process event.
        if (!watcherTerminal && watcherProcess) {
          watcherTerminal = watcherProcess.exitCode != null || watcherProcess.signalCode != null
        }
        let wrapperBoundaryRequired = mustKill
        let targetBoundaryConfirmed = watcherResult.targetBoundaryConfirmed

        // A healthy watcher opened and generation-checked the exact wrapper
        // process handle, then observed it signaled. That is authoritative
        // absence proof for that one captured generation; every other captured
        // descendant still requires the normal liveness proof.
        if (watcherResult.healthy && killResult && wrapperProcessIdentity) {
          const survivors = killResult.survivors.filter(identity => {
            if (identity.pid !== wrapperProcessIdentity?.pid) return true
            if (identity.createdAt == null || wrapperProcessIdentity.createdAt == null) return false
            return Math.abs(identity.createdAt - wrapperProcessIdentity.createdAt) > 1.5
          })
          killResult = { ...killResult, survivors, confirmed: survivors.length === 0 }
        }

        // execFile completion can lag exact process death while inherited stdio
        // drains. Accept only the nonce-bound success receipt written after
        // target termination and handle closure, corroborated by the healthy
        // watcher and its authenticated READY value.
        const watcherReadyValue = readExactWatcherReadyValue(watcherReadyPath)
        if (
          finalResult.code !== 0 &&
          watcherResult.healthy &&
          targetBoundaryConfirmed &&
          watcherReadyValue === `ARMED:${watcherReadyNonce}` &&
          hasSuccessfulWrapperReceipt(wrapperPhasePath, wrapperPhaseNonce)
        ) {
          finalResult = { stdout: 'TERMINATED\n', stderr: '', code: 0, pid: childPid }
        }

        // A watcher failure is not itself a target-tree boundary. Even when the
        // wrapper callback reports a normal exit, force the authenticated wrapper
        // identity through the same kill/absence confirmation and require the
        // named target-job drain to have returned a proven result.
        if (!watcherResult.healthy || !targetBoundaryConfirmed) {
          wrapperBoundaryRequired = true
          killing = true
          const remaining = Math.max(0, absoluteDeadline - Date.now())
          const forcedKillPromise =
            typeof childPid === 'number' && (!killResult || !killResult.confirmed)
              ? killProcessTreeAndAwaitGone(childPid, {
                  confirmMs: Math.min(killReserveMs, remaining),
                  preSnapshot: treeSnapshot,
                  deadlineAt: absoluteDeadline
                })
              : Promise.resolve(killResult)
          const targetJobFallback =
            !targetBoundaryConfirmed && remaining > 0
              ? terminateNamedTargetJobWithinDeadline(targetJobName, absoluteDeadline, Math.min(1_000, remaining))
              : Promise.resolve(targetBoundaryConfirmed)
          const [forcedKillResult, targetJobResult] = await Promise.all([forcedKillPromise, targetJobFallback])
          killResult = forcedKillResult
          targetBoundaryConfirmed = targetBoundaryConfirmed || targetJobResult
        }

        if (!watcherResult.healthy) {
          finalResult = {
            ...finalResult,
            stderr: [watcherStartupFailure, watcherResult.detail, finalResult.stderr]
              .filter(Boolean)
              .join('\\n'),
            code: 1,
            pid: childPid
          }
        }
        if (watcherDiagnostics && finalResult.code !== 0) {
          finalResult = {
            ...finalResult,
            stderr: [formatTargetJobWatcherDiagnostics(watcherDiagnostics), finalResult.stderr]
              .filter(Boolean)
              .join('\\n'),
            pid: childPid
          }
        }
        if (!targetBoundaryConfirmed) {
          finalResult = {
            ...finalResult,
            stderr: [finalResult.stderr, 'target-boundary-unconfirmed'].filter(Boolean).join('\\n'),
            code: 1,
            pid: childPid
          }
        }
        if (killResult && !killResult.confirmed) {
          finalResult = {
            ...finalResult,
            stderr: [
              finalResult.stderr,
              `unconfirmed-tree-survivors:${killResult.survivors.map(s => s.pid).join(',')}`
            ]
              .filter(Boolean)
              .join('\\n'),
            code: 1,
            pid: childPid
          }
        }
        if (!watcherTerminal) {
          finalResult = {
            ...finalResult,
            stderr: [finalResult.stderr, 'watcher-terminal-state-unconfirmed'].filter(Boolean).join('\\n'),
            code: 1,
            pid: childPid
          }
        }

        // Native confirmation can observe the process object gone before Node
        // delivers its exit event. Drain that event under the same deadline.
        if (wrapperBoundaryRequired && childProcess && absoluteDeadline > Date.now()) {
          await waitForChildExit(childProcess, absoluteDeadline - Date.now())
        }

        // Capture the exact authenticated READY value and the ordered,
        // nonce-validated wrapper phases before any artifact cleanup. These
        // bounded diagnostics identify which side of the boundary stalled
        // without making cleanup depend on a wildcard directory scan.
        const wrapperPhaseDiagnostics = readWrapperPhaseDiagnostics(wrapperPhasePath, wrapperPhaseNonce)
        if (finalResult.code !== 0) {
          finalResult = {
            ...finalResult,
            stderr: [
              watcherReadyValue ? `watcher-ready-value=${JSON.stringify(watcherReadyValue)}` : 'watcher-ready-value=missing',
              wrapperPhaseDiagnostics,
              finalResult.stderr
            ]
              .filter(Boolean)
              .join('\\n'),
            pid: childPid
          }
        }

        // Do not remove READY or its PID-qualified temp file while the watcher
        // can still publish FAILED or terminate the target job.
        if (watcherTerminal) cleanupWatcherArtifacts()
        settled = true
        signal?.removeEventListener('abort', onAbort)
        resolve(finalResult)
      })().catch(error => {
        // Preserve the exact artifacts if watcher terminalization itself failed;
        // recursive cleanup here could permit a late watcher mutation.
        settled = true
        signal?.removeEventListener('abort', onAbort)
        resolve({
          stdout: result.stdout,
          stderr: `termination-boundary-error:${String(error?.message ?? error)}`,
          code: 1,
          pid: childPid
        })
      })
    }

    childProcess = execFile(
      powershellExecutable(),
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        TERMINATE_JOB_WRAPPER_COMMAND
      ],
      {
        encoding: 'utf8',
        timeout: runMs,
        windowsHide: true,
        maxBuffer: 1024 * 1024,
        killSignal: 'SIGTERM',
        env: {
          ...process.env,
          HERMES_TERMINATE_SCRIPT: script,
          HERMES_TERMINATE_JOB_NAME: jobName,
          HERMES_TERMINATE_TARGET_JOB_NAME: targetJobName,
          HERMES_TERMINATE_TARGET_WAIT_MS: String(targetWaitMs),
          HERMES_TERMINATE_DEADLINE_AT: String(Math.trunc(wrapperDeadline)),
          HERMES_TERMINATE_WATCHER_READY_PATH: watcherReadyPath,
          HERMES_TERMINATE_WATCHER_READY_NONCE: watcherReadyNonce,
          HERMES_TERMINATE_WRAPPER_PID_MARKER_PATH: wrapperPidMarkerPath,
          HERMES_TERMINATE_WRAPPER_PID_MARKER_NONCE: wrapperPidMarkerNonce,
          HERMES_TERMINATE_WRAPPER_PHASE_PATH: wrapperPhasePath,
          HERMES_TERMINATE_WRAPPER_PHASE_NONCE: wrapperPhaseNonce,
          HERMES_TERMINATE_HELPER_SCRIPT_PATH: helperScriptPath,
          HERMES_TERMINATE_HELPER_GATE_PATH: helperGatePath
        }
      },
      (error: any, stdout, stderr) => {
        void (async () => {
          const errorStdout = String(stdout ?? error?.stdout ?? '')
          const capturedStderr = sanitizeRunnerStderr(stderr ?? error?.stderr ?? '')
          const errorMessage = String(error?.message ?? '')
          const lifecycleFact =
            error?.killed === true || /ETIMEDOUT|timeout|killed/i.test(errorMessage) ? 'timeout' : ''
          const errorStderr = [
            capturedStderr,
            typeof error?.code === 'number' ? `exit ${error.code}` : '',
            lifecycleFact
          ]
            .filter(Boolean)
            .join('\n')
          if (!error) {
            await finish({ stdout: String(stdout ?? ''), stderr: String(stderr ?? ''), code: 0 })
            return
          }
          if (
            typeof childPid === 'number' &&
            (error?.killed || /ETIMEDOUT|timeout/i.test(String(error?.message ?? '')))
          ) {
            killing = true
            await finish({
              stdout: errorStdout,
              stderr: errorStderr || 'timeout',
              code: typeof error?.code === 'number' ? error.code : 1
            })
            return
          }
          await finish({
            stdout: errorStdout,
            stderr: errorStderr,
            code: typeof error?.code === 'number' ? error.code : 1
          })
        })()
      }
    )

    if (typeof childProcess.pid === 'number' && childProcess.pid > 0) {
      childPid = childProcess.pid
      const launchedPid = childPid
      // The execFile PID is only a launch hint. The wrapper writes an exact,
      // nonce-bound self marker before opening the target job; authenticate that
      // generation before starting the detached watcher.
      wrapperMarkerSetup = (async () => {
        wrapperProcessIdentity = await waitForWrapperProcessMarker(
          wrapperPidMarkerPath,
          wrapperPidMarkerNonce,
          wrapperDeadline
        )
        if (!wrapperProcessIdentity) {
          watcherStartupFailure = `wrapper-marker-unavailable launch-pid=${launchedPid}`
          killing = true
          treeSnapshot = [{ pid: launchedPid }]
          try {
            childProcess?.kill('SIGKILL')
          } catch {
            void 0
          }
          return
        }
        const watcherSpawnedAt = Date.now()
        watcherProcess = watcherStarter(
          wrapperProcessIdentity.pid,
          wrapperProcessIdentity.createdAt ?? 0,
          targetJobName,
          watcherReadyPath,
          watcherReadyNonce,
          watcherDeadline
        )
        if (watcherProcess) watcherDiagnostics = observeTargetJobWatcher(watcherProcess, watcherSpawnedAt)
        if (!watcherProcess) {
          watcherStartupFailure = `watcher-spawn-failed wrapper-pid=${wrapperProcessIdentity.pid} launch-pid=${launchedPid}`
          killing = true
          try {
            childProcess?.kill('SIGKILL')
          } catch {
            void 0
          }
        }
        // Capture identities promptly after spawn with a bounded slice; abort must
        // not wait for a fresh snapshot before killing. Keep the authenticated
        // wrapper generation in the same confirmation set even when the launch
        // PID is a short-lived proxy.
        const snapshot = await snapshotProcessTreeIdentities(launchedPid, {
          timeoutMs: Math.min(400, killReserveMs)
        })
        // Never let the launch hint or an unknown-generation snapshot entry
        // replace the exact nonce-authenticated wrapper generation.
        treeSnapshot = [
          ...snapshot.filter(identity => identity.pid !== wrapperProcessIdentity?.pid),
          { pid: wrapperProcessIdentity.pid, createdAt: wrapperProcessIdentity.createdAt }
        ]
      })()
      // A marker failure kills the launch child; make sure the public promise
      // still enters terminalization even if execFile reports no callback.
      void wrapperMarkerSetup.then(() => {
        if (watcherStartupFailure && !settled && !terminalizing) {
          void finish({ stdout: '', stderr: watcherStartupFailure, code: 1, pid: childPid })
        }
      })
    }

    const onAbort = () => {
      if (settled || terminalizing) return
      killing = true
      if (typeof childPid === 'number') {
        // Kill immediately with already-captured identities (+ root). Never
        // delay kill for a fresh unbounded snapshot, and never synthesize
        // create-time. Unknown generation keeps confirmation fail-safe.
        if (treeSnapshot.length === 0) {
          treeSnapshot = [{ pid: childPid }]
        }
      }
      try {
        childProcess?.kill('SIGKILL')
      } catch {
        void 0
      }
      // finish() concurrently awaits the watcher and the authenticated tree
      // drain. If the watcher stalls it performs the bounded named-job fallback.
      void finish({ stdout: '', stderr: 'aborted', code: 1, pid: childPid })
    }

    if (signal) {
      if (signal.aborted) {
        onAbort()
      } else {
        signal.addEventListener('abort', onAbort, { once: true })
      }
    }
  })
}

/** Testable production runner; callers should use terminateWindowsHolderExact. */
export async function runPowerShellWithHardBoundary(
  script: string,
  timeoutMs = 4_000,
  signal?: AbortSignal,
  deadlineAt?: number,
  dependencies?: HardBoundaryDependencies
): Promise<{ stdout: string; stderr: string; code: number; pid?: number }> {
  return defaultRunPowerShell(script, timeoutMs, signal, deadlineAt, dependencies)
}

/**
 * Build a self-contained PowerShell script that terminates one PID only when
 * its create-time ticks still match the expected generation.
 */
export function buildExactTerminateScript(pid: number, createdAtUnixSeconds: number, waitMs = 1_500): string {
  // createdAt from psutil is epoch seconds (float). Compare at second resolution.
  const expected = Number(createdAtUnixSeconds)
  return `
$ErrorActionPreference = 'Stop'
$pidTarget = ${Math.trunc(pid)}
$expectedUnix = [double]${expected}
$waitMs = ${Math.max(0, Math.trunc(waitMs))}
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class HermesForceReleaseNative {
  public const uint PROCESS_TERMINATE = 0x0001;
  public const uint PROCESS_SET_QUOTA = 0x0100;
  public const uint PROCESS_SUSPEND_RESUME = 0x0800;
  public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
  public const uint SYNCHRONIZE = 0x00100000;
  public const uint JOB_OBJECT_ASSIGN_PROCESS = 0x0001;
  public const uint JOB_OBJECT_TERMINATE = 0x0008;
  public const uint JOB_OBJECT_QUERY = 0x0004;
  public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
  public const int JobObjectExtendedLimitInformation = 9;
  public const uint WAIT_OBJECT_0 = 0;
  public const uint WAIT_TIMEOUT = 258;
  [StructLayout(LayoutKind.Sequential)]
  public struct BasicLimits {
    public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
    public uint LimitFlags;
    public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
    public uint ActiveProcessLimit;
    public UIntPtr Affinity;
    public uint PriorityClass, SchedulingClass;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct IoCounters {
    public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
    public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct ExtendedLimits {
    public BasicLimits BasicLimitInformation;
    public IoCounters IoInfo;
    public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct FileTime { public uint Low; public uint High; }
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern IntPtr OpenJobObject(uint desiredAccess, bool inheritHandle, string name);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool TerminateJobObject(IntPtr job, uint exitCode);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool GetProcessTimes(IntPtr process, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern bool CloseHandle(IntPtr handle);
  [DllImport("ntdll.dll")]
  public static extern int NtSuspendProcess(IntPtr process);
  [DllImport("ntdll.dll")]
  public static extern int NtResumeProcess(IntPtr process);
  public static double ToUnixSeconds(FileTime time) {
    long fileTicks = ((long)time.High << 32) | time.Low;
    return (fileTicks - 116444736000000000L) / 10000000.0;
  }
  public static IntPtr OpenAuthenticatedProcess(int pid, double expectedUnix, out double actualUnix, out int error) {
    actualUnix = 0;
    error = 0;
    IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_SUSPEND_RESUME | PROCESS_TERMINATE | SYNCHRONIZE, false, pid);
    if (process == IntPtr.Zero) {
      error = Marshal.GetLastWin32Error();
      return IntPtr.Zero;
    }
    FileTime creation, exit, kernel, user;
    if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) {
      error = Marshal.GetLastWin32Error();
      CloseHandle(process);
      return IntPtr.Zero;
    }
    actualUnix = ToUnixSeconds(creation);
    if (Math.Abs(actualUnix - expectedUnix) > 1.5) {
      error = 0x10001;
      CloseHandle(process);
      return IntPtr.Zero;
    }
    return process;
  }
  public static int SuspendProcess(IntPtr process) {
    int status = NtSuspendProcess(process);
    return status == 0 ? 0 : status;
  }
  public static int ResumeProcess(IntPtr process) {
    int status = NtResumeProcess(process);
    return status == 0 ? 0 : status;
  }
  public static int ReadCreatedAt(IntPtr process, out double createdUnix) {
    createdUnix = 0;
    FileTime creation, exit, kernel, user;
    if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) {
      int error = Marshal.GetLastWin32Error();
      return error == 0 ? -1 : -error;
    }
    createdUnix = ToUnixSeconds(creation);
    return 0;
  }
  public static IntPtr CreateKillOnCloseJob() {
    return CreateNamedKillOnCloseJob(null);
  }
  public static IntPtr CreateNamedKillOnCloseJob(string name) {
    IntPtr job = CreateJobObject(IntPtr.Zero, name);
    if (job == IntPtr.Zero) return IntPtr.Zero;
    var limits = new ExtendedLimits();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits)))) {
      CloseHandle(job);
      return IntPtr.Zero;
    }
    return job;
  }
  public static int AssignProcessHandle(IntPtr job, IntPtr process) {
    if (AssignProcessToJobObject(job, process)) return 0;
    int error = Marshal.GetLastWin32Error();
    return error == 0 ? -1 : -error;
  }
  public static int ProcessIsInJob(IntPtr job, IntPtr process) {
    bool contained = false;
    if (!IsProcessInJob(process, job, out contained)) {
      int error = Marshal.GetLastWin32Error();
      return error == 0 ? -1 : -error;
    }
    return contained ? 1 : 0;
  }
  public static IntPtr OpenNamedTargetJob(string name, out int error) {
    IntPtr job = OpenJobObject(JOB_OBJECT_ASSIGN_PROCESS | JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY | SYNCHRONIZE, false, name);
    error = job == IntPtr.Zero ? Marshal.GetLastWin32Error() : 0;
    return job;
  }
  public static int TerminateJobAndWait(IntPtr job, int waitMs) {
    if (!TerminateJobObject(job, 1)) {
      int error = Marshal.GetLastWin32Error();
      return error == 0 ? -1 : -error;
    }
    uint result = WaitForSingleObject(job, (uint)Math.Max(0, waitMs));
    if (result == WAIT_OBJECT_0) return 0;
    if (result == WAIT_TIMEOUT) return -258;
    int waitError = Marshal.GetLastWin32Error();
    return waitError == 0 ? -1 : -waitError;
  }
}
"@

function Get-IdentityUnix([int]$targetPid) {
  $p = Get-Process -Id $targetPid -ErrorAction Stop
  return [DateTimeOffset]::new($p.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
}

try {
  $actualUnix = Get-IdentityUnix $pidTarget
} catch {
  $msg = [string]$_.Exception.Message
  if ($msg -match 'Access is denied|AccessDenied|denied') {
    Write-Output ('ACCESS_DENIED ' + $msg)
    exit 5
  }
  Write-Output 'ALREADY_GONE'
  exit 0
}
if ([math]::Abs($actualUnix - $expectedUnix) -gt 1.5) {
  Write-Output ("CREATE_TIME_MISMATCH actual=" + $actualUnix + " expected=" + $expectedUnix)
  exit 3
}

$job = [IntPtr]::Zero
$targetJobName = [Environment]::GetEnvironmentVariable('HERMES_TERMINATE_TARGET_JOB_NAME')
$externalTargetJob = -not [string]::IsNullOrWhiteSpace($targetJobName)
$handles = @{}
$suspended = New-Object 'System.Collections.Generic.List[int]'
$contained = New-Object 'System.Collections.Generic.HashSet[int]'
$success = $false
$exitCode = 1
$fallbackSnapshotUsed = $false
$treeRows = $null

function Get-TreeChildren([int]$parentPid) {
  if ($null -eq $script:treeRows) {
    try {
      if ([Environment]::GetEnvironmentVariable('HERMES_FORCE_RELEASE_FORCE_SNAPSHOT_FAILURE') -eq '1' -and -not $script:fallbackSnapshotUsed) {
        $script:fallbackSnapshotUsed = $true
        throw 'forced primary snapshot failure'
      }
      # Capture one coherent process snapshot. Re-querying WMI for every
      # generation consumes the mutation deadline and widens PID-reuse races.
      $script:treeRows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
      # A primary CIM snapshot failure still has one bounded provider fallback.
      # If this fallback also fails, the caller's finally kills only the
      # already-contained root and reports failure; it never claims clearance.
      $script:treeRows = @(Get-WmiObject Win32_Process -ErrorAction Stop)
    }
  }
  return @($script:treeRows | Where-Object { [int]$_.ParentProcessId -eq $parentPid })
}

function Get-TreeRowCreationUnix($row) {
  $raw = $row.CreationDate
  if ($null -eq $raw -or [string]::IsNullOrWhiteSpace([string]$raw)) {
    throw 'TREE_SNAPSHOT_MISSING_CREATE_TIME'
  }
  try {
    if ($raw -is [DateTime]) {
      return [DateTimeOffset]::new(([DateTime]$raw).ToUniversalTime()).ToUnixTimeSeconds()
    }
    return [DateTimeOffset]::new(
      [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$raw).ToUniversalTime()
    ).ToUnixTimeSeconds()
  } catch {
    throw 'TREE_SNAPSHOT_INVALID_CREATE_TIME'
  }
}

function Assert-NativeSuccess([int]$code, [string]$operation) {
  if ($code -ne 0) { throw ($operation + ' win32=' + (-$code)) }
}

function Assign-ContainedProcess([IntPtr]$processHandle, [int]$currentPid) {
  $assignCode = [HermesForceReleaseNative]::AssignProcessHandle($job, $processHandle)
  if ($assignCode -ne 0) { throw ('TREE_ASSIGN_FAILED pid=' + $currentPid + ' win32=' + (-$assignCode)) }
  $membershipCode = [HermesForceReleaseNative]::ProcessIsInJob($job, $processHandle)
  if ($membershipCode -ne 1) {
    Assert-NativeSuccess $membershipCode ('TREE_ASSIGN_MEMBERSHIP_FAILED pid=' + $currentPid)
    throw ('TREE_ASSIGN_MEMBERSHIP_FAILED pid=' + $currentPid + ' win32=0')
  }
}

function Pause-BoundaryTest([string]$phase, [int]$currentPid) {
  $wanted = [Environment]::GetEnvironmentVariable('HERMES_FORCE_RELEASE_TEST_PAUSE_PHASE')
  if ($wanted -ne $phase) { return }
  $wantedPidText = [Environment]::GetEnvironmentVariable('HERMES_FORCE_RELEASE_TEST_PAUSE_PID')
  if (-not [string]::IsNullOrWhiteSpace($wantedPidText)) {
    $wantedPid = 0
    if (-not [int]::TryParse($wantedPidText, [ref]$wantedPid) -or $wantedPid -ne $currentPid) { return }
  }
  $marker = [Environment]::GetEnvironmentVariable('HERMES_FORCE_RELEASE_TEST_PHASE_MARKER')
  if (-not [string]::IsNullOrWhiteSpace($marker)) {
    $markerTemp = $marker + '.tmp'
    [IO.File]::WriteAllText(
      $markerTemp,
      ($phase + ':' + $currentPid + [Environment]::NewLine),
      [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $markerTemp -Destination $marker -Force
  }
  Start-Sleep -Seconds 30
}

try {
  if ($externalTargetJob) {
    $targetJobOpenError = 0
    $job = [HermesForceReleaseNative]::OpenNamedTargetJob($targetJobName, [ref]$targetJobOpenError)
    if ($job -eq [IntPtr]::Zero) { throw ('TREE_JOB_OPEN_FAILED win32=' + $targetJobOpenError) }
  } else {
    $job = [HermesForceReleaseNative]::CreateKillOnCloseJob()
    if ($job -eq [IntPtr]::Zero) { throw 'TREE_JOB_CREATE_FAILED win32=5' }
  }

  # Authenticate and assign each generation from one handle before suspending
  # it. A helper death after assignment closes this job and kills the member;
  # no suspended process can remain outside the terminal boundary.
  $rootActual = 0.0
  $rootOpenError = 0
  $rootHandle = [HermesForceReleaseNative]::OpenAuthenticatedProcess($pidTarget, $expectedUnix, [ref]$rootActual, [ref]$rootOpenError)
  if ($rootHandle -eq [IntPtr]::Zero) {
    if ($rootOpenError -eq 0x10001) { throw ("CREATE_TIME_MISMATCH root=" + $pidTarget) }
    throw ('TREE_OPEN_FAILED win32=' + $rootOpenError)
  }
  $handles[[string]$pidTarget] = $rootHandle
  Assign-ContainedProcess $rootHandle $pidTarget
  [void]$contained.Add($pidTarget)
  Pause-BoundaryTest 'after-root-assignment' $pidTarget
  $suspendRoot = [HermesForceReleaseNative]::SuspendProcess($rootHandle)
  Assert-NativeSuccess $suspendRoot 'TREE_SUSPEND_FAILED'
  [void]$suspended.Add($pidTarget)
  Pause-BoundaryTest 'after-root-suspension' $pidTarget
  $rootRevalidated = 0.0
  Assert-NativeSuccess ([HermesForceReleaseNative]::ReadCreatedAt($rootHandle, [ref]$rootRevalidated)) 'TREE_REVALIDATE_FAILED'
  if ([math]::Abs($rootRevalidated - $expectedUnix) -gt 1.5) {
    throw ("CREATE_TIME_MISMATCH root=" + $pidTarget)
  }

  $rows = New-Object 'System.Collections.Generic.List[object]'
  $seen = @{}
  $queue = New-Object 'System.Collections.Generic.Queue[int]'
  $seen[[string]$pidTarget] = $true
  [void]$queue.Enqueue($pidTarget)
  while ($queue.Count -gt 0) {
    $parentPid = $queue.Dequeue()
    foreach ($child in @(Get-TreeChildren $parentPid)) {
      $childPid = [int]$child.ProcessId
      if ($childPid -le 0 -or $seen.ContainsKey([string]$childPid)) { continue }
      $seen[[string]$childPid] = $true
      # Use the creation timestamp from the same process-tree row that yielded
      # this PID. Never turn a stale/reused PID into a fresh authenticated
      # generation by probing it again before opening its boundary handle.
      $childExpected = Get-TreeRowCreationUnix $child
      $childActual = 0.0
      $childOpenError = 0
      $childHandle = [HermesForceReleaseNative]::OpenAuthenticatedProcess($childPid, $childExpected, [ref]$childActual, [ref]$childOpenError)
      if ($childHandle -eq [IntPtr]::Zero) {
        if ($childOpenError -eq 0x10001) { throw ("CREATE_TIME_MISMATCH child=" + $childPid) }
        throw ('TREE_OPEN_FAILED win32=' + $childOpenError)
      }
      $handles[[string]$childPid] = $childHandle
      Assign-ContainedProcess $childHandle $childPid
      [void]$contained.Add($childPid)
      Pause-BoundaryTest 'after-child-assignment' $childPid
      $childSuspend = [HermesForceReleaseNative]::SuspendProcess($childHandle)
      Assert-NativeSuccess $childSuspend 'TREE_SUSPEND_FAILED'
      [void]$suspended.Add($childPid)
      Pause-BoundaryTest 'after-child-suspension' $childPid
      $childRevalidated = 0.0
      Assert-NativeSuccess ([HermesForceReleaseNative]::ReadCreatedAt($childHandle, [ref]$childRevalidated)) 'TREE_REVALIDATE_FAILED'
      if ([math]::Abs($childRevalidated - $childExpected) -gt 1.5) {
        throw ("CREATE_TIME_MISMATCH child=" + $childPid)
      }
      [void]$rows.Add([pscustomobject]@{ pid = $childPid; created = $childRevalidated })
      [void]$queue.Enqueue($childPid)
    }
  }

  $terminateCode = [HermesForceReleaseNative]::TerminateJobAndWait($job, $waitMs)
  Assert-NativeSuccess $terminateCode 'TREE_TERMINATE_FAILED'
  $allRows = @([pscustomobject]@{ pid = $pidTarget; created = $rootRevalidated }) + @($rows.ToArray())
  if (-not $externalTargetJob) {
    foreach ($row in $allRows) {
      $live = Get-Process -Id ([int]$row.pid) -ErrorAction SilentlyContinue
      if ($null -ne $live) {
        $liveCreated = Get-IdentityUnix ([int]$row.pid)
        if ([math]::Abs($liveCreated - [double]$row.created) -le 1.5) {
          throw ("TREE_SURVIVOR pid=" + [int]$row.pid)
        }
      }
    }
  }
  $success = $true
  $exitCode = 0
  Write-Output 'TERMINATED'
} catch {
  $message = [string]$_.Exception.Message
  $win32 = [regex]::Match($message, 'win32=(\d+)').Groups[1].Value
  if ($message -match 'Access is denied|AccessDenied|denied' -or $win32 -eq '5') {
    Write-Output ('ACCESS_DENIED ' + $message)
    $exitCode = 5
  } elseif ($message -match 'CREATE_TIME_MISMATCH') {
    Write-Output $message
    $exitCode = 3
  } else {
    Write-Output ('BOUNDARY_FAILED ' + $message)
    $exitCode = 1
  }
} finally {
  if ($job -ne [IntPtr]::Zero) {
    if (-not $success) { [HermesForceReleaseNative]::TerminateJobAndWait($job, $waitMs) | Out-Null }
    [HermesForceReleaseNative]::CloseHandle($job) | Out-Null
  }
  for ($index = $suspended.Count - 1; $index -ge 0; $index--) {
    $suspendedPid = [int]$suspended[$index]
    if (-not $contained.Contains($suspendedPid)) {
      $handle = $handles[[string]$suspendedPid]
      if ($null -ne $handle) { [HermesForceReleaseNative]::ResumeProcess($handle) | Out-Null }
    }
  }
  foreach ($entry in $handles.GetEnumerator()) {
    [HermesForceReleaseNative]::CloseHandle([IntPtr]$entry.Value) | Out-Null
  }
}
exit $exitCode
`.trim()
}

export function parseTerminateScriptOutput(stdout: string, code: number): ForceReleaseTerminateResult {
  const text = String(stdout || '').trim()
  if (/PROTECTED/i.test(text)) {
    const win32 = text.match(/win32=(\d+)/i)
    return { kind: 'protected', win32Error: win32 ? Number(win32[1]) : 5 }
  }
  if (/ALREADY_GONE/i.test(text) || (code === 0 && /TERMINATED/i.test(text))) {
    if (/TERMINATED/i.test(text)) return { kind: 'terminated' }
    if (/ALREADY_GONE/i.test(text)) return { kind: 'already-gone' }
  }
  if (/CREATE_TIME_MISMATCH/i.test(text) || code === 3) {
    return { kind: 'create-time-mismatch' }
  }
  if (/ACCESS_DENIED/i.test(text) || code === 5) {
    const marker = text.match(/ACCESS_DENIED(?:\s+([\s\S]*))?/i)
    const detail = marker?.[1]?.trim()
    return detail ? { kind: 'access-denied', win32Error: 5, detail } : { kind: 'access-denied', win32Error: 5 }
  }
  const win32 = text.match(/win32=(\d+)/i)
  if (win32) {
    const err = Number(win32[1])
    if (err === 5) return { kind: 'access-denied', win32Error: 5, detail: text }
    // Do not treat 6/87 as already-gone: the process was observed live above.
    return { kind: 'failed', detail: text || `win32=${err}`, win32Error: err }
  }
  if (code === 0 && /TERMINATED/i.test(text)) return { kind: 'terminated' }
  return { kind: 'failed', detail: text || `exit ${code}` }
}

export async function terminateWindowsHolderExact(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunPowerShell,
    waitMs = 1_500,
    timeoutMs,
    signal,
    deadlineAt
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    waitMs?: number
    /** Hard wall-clock budget for the PowerShell child including kill/confirm. */
    timeoutMs?: number
    /** When aborted, kill the child tree, await confirmed absence, then return. */
    signal?: AbortSignal
    /** Absolute deadline shared with the caller's orchestration budget. */
    deadlineAt?: number
  } = {}
): Promise<ForceReleaseTerminateResult> {
  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }
  if (signal?.aborted) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }
  if (!Number.isInteger(target.pid) || target.pid <= 0) {
    return { kind: 'failed', detail: 'invalid pid' }
  }
  if (!Number.isFinite(target.createdAt) || target.createdAt <= 0) {
    return { kind: 'failed', detail: 'invalid createdAt' }
  }

  const requestedBudget = Math.max(0, Math.trunc(timeoutMs ?? Math.max(2_000, waitMs + 1_000)))
  const remainingBudget =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? Math.max(0, Math.trunc(deadlineAt - Date.now()))
      : requestedBudget
  const budget = Math.min(requestedBudget, remainingBudget)
  // Keep TerminateProcess wait short enough that kill-reserve still fits.
  const killReserveMs = terminateKillReserveMs(budget)
  const runBudget = Math.max(1, budget - killReserveMs)
  const effectiveWait = Math.max(0, Math.min(Math.trunc(waitMs), Math.max(0, runBudget - 250)))
  if (budget <= 50) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }

  const script = buildExactTerminateScript(target.pid, target.createdAt, effectiveWait)
  const result = await run(script, budget, signal, deadlineAt)
  if (signal?.aborted) {
    // Child tree must already be confirmed gone by run(); never claim mutation.
    return { kind: 'failed', detail: deadlineFailureDetail(result.stderr) }
  }
  if (/unconfirmed-tree-survivors/i.test(result.stderr || '')) {
    return { kind: 'failed', detail: sanitizeBoundaryDiagnostics(result.stderr) || 'unconfirmed-tree-survivors' }
  }
  // Timed-out/killed child: do not parse a partial TerminateProcess success.
  if (/aborted|ETIMEDOUT|timeout/i.test(result.stderr || '') && !/TERMINATED|ACCESS_DENIED|PROTECTED|CREATE_TIME/i.test(result.stdout || '')) {
    return { kind: 'failed', detail: deadlineFailureDetail(result.stderr) }
  }
  return parseTerminateScriptOutput(result.stdout + '\n' + result.stderr, result.code)
}

/**
 * Execute exact termination using the caller's remaining absolute deadline.
 * This is the production adapter used by the updater path; keeping the
 * deadline calculation here prevents a stale per-holder timeout from
 * extending the overall force-release contract.
 */
export async function terminateWindowsHolderWithinDeadline(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunPowerShell,
    budgetMs,
    deadlineAt,
    signal
  }: {
    platform?: NodeJS.Platform
    run?: RunPowerShell
    budgetMs: number
    deadlineAt: number
    signal?: AbortSignal
  }
): Promise<ForceReleaseTerminateResult> {
  const requestedBudget = Math.max(0, Math.trunc(budgetMs))
  const absoluteDeadline = Number.isFinite(deadlineAt) ? Math.trunc(deadlineAt) : Date.now() + requestedBudget
  const remainingBudget = Math.max(0, absoluteDeadline - Date.now())
  const budget = Math.min(requestedBudget, remainingBudget)
  if (budget <= 50 || signal?.aborted) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }

  return terminateWindowsHolderExact(target, {
    platform,
    run,
    timeoutMs: budget,
    waitMs: Math.max(0, Math.min(1_500, budget - 250)),
    signal,
    deadlineAt: absoluteDeadline
  })
}
