/**
 * Exact Windows holder termination.
 *
 * One directly tracked hidden PowerShell process hosts one constant C# native
 * boundary. It never spawns a mutating child, assigns a target Job Object, or
 * terminates descendants. The exact target process handle is revalidated for
 * generation, canonical image scope, canonical resource scope, and fresh
 * Restart Manager ownership immediately before TerminateProcess.
 */

import { type ChildProcessWithoutNullStreams, execFile, spawn } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder, ForceReleaseTerminateResult } from './windows-update-force-release'

const FILETIME_PATTERN = /^\d{15,20}$/
const MAX_RESOURCES = 32
const MAX_PATH_CHARS = 32_767
const MAX_DIAGNOSTIC_BYTES = 4_096
const MUTATION_RESERVE_MS = 750
const execFileAsync = promisify(execFile)

export interface DirectBoundaryRequest {
  deadlineAt: number
  installRoot: string
  pid: number
  creationFileTime: string
  resources: string[]
  signal?: AbortSignal
}

export type DirectBoundaryRunResult = {
  stdout: string
  stderr: string
  code: number
}

export type RunDirectBoundary = (request: DirectBoundaryRequest) => Promise<DirectBoundaryRunResult>

function powershellExecutable(): string {
  return path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

function boundedAppend(current: string, chunk: unknown): string {
  if (Buffer.byteLength(current, 'utf8') >= MAX_DIAGNOSTIC_BYTES) {
    return current
  }

  return (current + String(chunk ?? '')).slice(0, MAX_DIAGNOSTIC_BYTES)
}

function fixedOsEnvironment(): NodeJS.ProcessEnv {
  const allowed = new Set(['COMSPEC', 'PATHEXT', 'PSMODULEPATH', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'WINDIR'])

  const result: NodeJS.ProcessEnv = {}

  for (const [name, value] of Object.entries(process.env)) {
    if (value !== undefined && allowed.has(name.toUpperCase())) {
      result[name] = value
    }
  }

  return result
}

export const DIRECT_NATIVE_BOUNDARY_COMMAND = String.raw`
$ErrorActionPreference = 'Stop'
$pidText = [Environment]::GetEnvironmentVariable('HERMES_NATIVE_TARGET_PID', 'Process')
$fileTime = [Environment]::GetEnvironmentVariable('HERMES_NATIVE_TARGET_FILETIME', 'Process')
$installRoot = [Environment]::GetEnvironmentVariable('HERMES_NATIVE_INSTALL_ROOT', 'Process')
$resourcePayload = [Environment]::GetEnvironmentVariable('HERMES_NATIVE_RESOURCES_B64', 'Process')
$deadlineText = [Environment]::GetEnvironmentVariable('HERMES_NATIVE_DEADLINE_AT', 'Process')

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;

public static class HermesExactTerminate {
    private const uint PROCESS_TERMINATE = 0x0001;
    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    private const uint SYNCHRONIZE = 0x00100000;
    private const uint FILE_SHARE_READ = 0x00000001;
    private const uint FILE_SHARE_WRITE = 0x00000002;
    private const uint FILE_SHARE_DELETE = 0x00000004;
    private const uint OPEN_EXISTING = 3;
    private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private const int ERROR_ACCESS_DENIED = 5;
    private const int ERROR_INVALID_PARAMETER = 87;
    private const int ERROR_MORE_DATA = 234;
    private const int CCH_RM_SESSION_KEY = 32;
    private const int CCH_RM_MAX_APP_NAME = 255;
    private const int CCH_RM_MAX_SVC_NAME = 63;
    private const int MIN_MUTATION_DRAIN_MS = 250;

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME {
        public uint Low;
        public uint High;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RM_UNIQUE_PROCESS {
        public int ProcessId;
        public FILETIME ProcessStartTime;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_APP_NAME + 1)]
        public string AppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_SVC_NAME + 1)]
        public string ServiceName;
        public uint ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool Restartable;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr process,
        out FILETIME creation,
        out FILETIME exit,
        out FILETIME kernel,
        out FILETIME user
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(
        IntPtr process,
        uint flags,
        StringBuilder imagePath,
        ref uint size
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(
        IntPtr file,
        StringBuilder path,
        uint pathLength,
        uint flags
    );

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(
        out uint sessionHandle,
        int sessionFlags,
        [Out] StringBuilder sessionKey
    );

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint sessionHandle);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(
        uint sessionHandle,
        uint fileCount,
        [MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.LPWStr)] string[] fileNames,
        uint applicationCount,
        IntPtr applications,
        uint serviceCount,
        [MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.LPWStr)] string[] serviceNames
    );

    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(
        uint sessionHandle,
        out uint processInfoNeeded,
        ref uint processInfoCount,
        [In, Out] RM_PROCESS_INFO[] affectedApplications,
        ref uint rebootReasons
    );

    private static ulong FileTimeValue(FILETIME value) {
        return ((ulong)value.High << 32) | value.Low;
    }

    private static ulong ProcessFileTime(IntPtr process) {
        FILETIME creation;
        FILETIME exit;
        FILETIME kernel;
        FILETIME user;
        if (!GetProcessTimes(process, out creation, out exit, out kernel, out user)) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        return FileTimeValue(creation);
    }

    private static string StripExtendedPrefix(string value) {
        if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) {
            return @"\\" + value.Substring(8);
        }
        if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) {
            return value.Substring(4);
        }
        return value;
    }

    private static string CanonicalPath(string value, bool directory) {
        IntPtr handle = CreateFile(
            value,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            IntPtr.Zero,
            OPEN_EXISTING,
            directory ? FILE_FLAG_BACKUP_SEMANTICS : 0,
            IntPtr.Zero
        );
        if (handle == new IntPtr(-1)) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        try {
            StringBuilder result = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(handle, result, (uint)result.Capacity, 0);
            if (length == 0 || length >= result.Capacity) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return Path.GetFullPath(StripExtendedPrefix(result.ToString()))
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }
        finally {
            CloseHandle(handle);
        }
    }

    private static bool IsWithin(string root, string candidate) {
        if (string.Equals(root, candidate, StringComparison.OrdinalIgnoreCase)) {
            return false;
        }
        string prefix = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
            + Path.DirectorySeparatorChar;
        return candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    private static string ProcessImage(IntPtr process) {
        StringBuilder image = new StringBuilder(32768);
        uint length = (uint)image.Capacity;
        if (!QueryFullProcessImageName(process, 0, image, ref length)) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        return CanonicalPath(image.ToString(), false);
    }

    private static int ResourceOwnership(string resource, int pid, ulong expectedFileTime) {
        uint session;
        StringBuilder key = new StringBuilder(CCH_RM_SESSION_KEY + 1);
        int rc = RmStartSession(out session, 0, key);
        if (rc != 0) return -1;
        try {
            rc = RmRegisterResources(session, 1, new string[] { resource }, 0, IntPtr.Zero, 0, null);
            if (rc != 0) return -1;

            RM_PROCESS_INFO[] rows = null;
            uint finalCount = 0;
            for (int attempt = 0; attempt < 8; attempt++) {
                uint needed = 0;
                uint count = 0;
                uint reboot = 0;
                rc = RmGetList(session, out needed, ref count, null, ref reboot);
                if (rc == 0 && needed == 0) return 0;
                if ((rc != ERROR_MORE_DATA && rc != 0) || needed == 0 || needed > 4096) return -1;

                count = needed;
                RM_PROCESS_INFO[] candidate = new RM_PROCESS_INFO[count];
                rc = RmGetList(session, out needed, ref count, candidate, ref reboot);
                if (rc == ERROR_MORE_DATA) continue;
                if (rc != 0 || count > candidate.Length) return -1;
                rows = candidate;
                finalCount = count;
                break;
            }
            if (rows == null) return -1;

            for (int index = 0; index < finalCount; index++) {
                RM_UNIQUE_PROCESS owner = rows[index].Process;
                if (
                    owner.ProcessId == pid &&
                    FileTimeValue(owner.ProcessStartTime) == expectedFileTime
                ) {
                    return 1;
                }
            }
            return 0;
        }
        finally {
            RmEndSession(session);
        }
    }

    private static string ValidateHandle(
        IntPtr process,
        ulong expectedFileTime,
        string canonicalRoot,
        string[] canonicalResources,
        int pid,
        long deadlineAt
    ) {
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() >= deadlineAt) return "TIMEOUT";
        uint state = WaitForSingleObject(process, 0);
        if (state == WAIT_OBJECT_0) return "ALREADY_GONE";
        if (state != WAIT_TIMEOUT) return "IDENTITY_UNREADABLE";
        if (ProcessFileTime(process) != expectedFileTime) return "GENERATION_MISMATCH";

        string image;
        try {
            image = ProcessImage(process);
        }
        catch {
            return "IDENTITY_UNREADABLE";
        }
        if (!IsWithin(canonicalRoot, image)) return "IMAGE_OUT_OF_SCOPE";

        foreach (string resource in canonicalResources) {
            long remaining = deadlineAt - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            if (remaining <= 0 || remaining > Int32.MaxValue) return "TIMEOUT";
            Task<int> ownershipTask = Task.Run(() => ResourceOwnership(resource, pid, expectedFileTime));
            if (!ownershipTask.Wait((int)remaining)) return "TIMEOUT";
            int ownership = ownershipTask.Result;
            if (ownership < 0) return "OWNERSHIP_UNKNOWN";
            if (ownership == 0) return "OWNERSHIP_STALE";
        }
        return "VALID";
    }

    public static string Run(
        int pid,
        string expectedFileTimeText,
        string installRoot,
        string[] resources,
        long deadlineAt
    ) {
        if (pid <= 0 || resources == null || resources.Length == 0 || resources.Length > 32) {
            return "INVALID_CLAIM";
        }
        ulong expectedFileTime;
        if (!UInt64.TryParse(expectedFileTimeText, out expectedFileTime) || expectedFileTime == 0) {
            return "INVALID_CLAIM";
        }
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() >= deadlineAt) return "TIMEOUT";

        string canonicalRoot;
        var canonicalResources = new List<string>();
        try {
            canonicalRoot = CanonicalPath(installRoot, true);
            foreach (string resource in resources) {
                string canonicalResource = CanonicalPath(resource, false);
                if (!IsWithin(canonicalRoot, canonicalResource)) return "RESOURCE_OUT_OF_SCOPE";
                canonicalResources.Add(canonicalResource);
            }
        }
        catch {
            return "SCOPE_UNREADABLE";
        }

        IntPtr processHandle = OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
            false,
            pid
        );
        if (processHandle == IntPtr.Zero) {
            int error = Marshal.GetLastWin32Error();
            if (error == ERROR_INVALID_PARAMETER) return "ALREADY_GONE";
            if (error != ERROR_ACCESS_DENIED) return "IDENTITY_UNREADABLE";

            // A combined open can prove only that one requested right was
            // denied. Open a read-only retained generation and revalidate all
            // scope and current-ownership claims before classifying the
            // difference as a terminate-right permission boundary.
            IntPtr permissionProbe = OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                false,
                pid
            );
            if (permissionProbe == IntPtr.Zero) {
                int probeError = Marshal.GetLastWin32Error();
                return probeError == ERROR_INVALID_PARAMETER ? "ALREADY_GONE" : "IDENTITY_UNREADABLE";
            }
            try {
                string permissionValidation = ValidateHandle(
                    permissionProbe,
                    expectedFileTime,
                    canonicalRoot,
                    canonicalResources.ToArray(),
                    pid,
                    deadlineAt
                );
                return permissionValidation == "VALID" ? "PERMISSION_REQUIRED" : permissionValidation;
            }
            finally {
                CloseHandle(permissionProbe);
            }
        }

        try {
            string finalValidation = ValidateHandle(
                processHandle,
                expectedFileTime,
                canonicalRoot,
                canonicalResources.ToArray(),
                pid,
                deadlineAt
            );
            if (finalValidation != "VALID") return finalValidation;

            long remaining = deadlineAt - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            if (remaining < MIN_MUTATION_DRAIN_MS || remaining > Int32.MaxValue) return "TIMEOUT";

            if (!TerminateProcess(processHandle, 1)) {
                int error = Marshal.GetLastWin32Error();
                if (WaitForSingleObject(processHandle, 0) == WAIT_OBJECT_0) return "ALREADY_GONE";
                return error == ERROR_ACCESS_DENIED ? "PERMISSION_REQUIRED" : "TERMINATE_FAILED";
            }

            remaining = deadlineAt - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            if (remaining > 0 && remaining <= Int32.MaxValue) {
                uint waited = WaitForSingleObject(processHandle, (uint)remaining);
                if (waited == WAIT_OBJECT_0) return "TERMINATED";
                if (waited != WAIT_TIMEOUT) return "DRAIN_FAILED";
            }

            return "DRAIN_FAILED";
        }
        catch {
            return "FAILED";
        }
        finally {
            CloseHandle(processHandle);
        }
    }
}
'@ -Language CSharp -ErrorAction Stop

$pidValue = 0
$deadlineAt = 0L
if (-not [int]::TryParse($pidText, [ref]$pidValue) -or $pidValue -le 0) { throw 'invalid claim' }
if (-not [long]::TryParse($deadlineText, [ref]$deadlineAt) -or $deadlineAt -le 0) { throw 'invalid claim' }
if ($fileTime -notmatch '^\d{15,20}$') { throw 'invalid claim' }
$resourceJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($resourcePayload))
$resources = @($resourceJson | ConvertFrom-Json)
if ($resources.Count -lt 1 -or $resources.Count -gt 32) { throw 'invalid claim' }

Write-Output 'BOUNDARY_READY'
[Console]::Out.Flush()
$authorization = [Console]::In.ReadLine()
if ($authorization -ne 'GO') {
    Write-Output 'RESULT:FAILED'
    exit 1
}

try {
    $result = [HermesExactTerminate]::Run(
        $pidValue,
        $fileTime,
        $installRoot,
        [string[]]$resources,
        $deadlineAt
    )
    Write-Output ('RESULT:' + $result)
    if ($result -eq 'TERMINATED' -or $result -eq 'ALREADY_GONE') { exit 0 }
    if ($result -eq 'PERMISSION_REQUIRED') { exit 5 }
    if ($result -eq 'GENERATION_MISMATCH') { exit 3 }
    exit 1
} catch {
    Write-Output 'RESULT:FAILED'
    exit 1
}
`.trim()

export function buildExactTerminateScript(): string {
  return DIRECT_NATIVE_BOUNDARY_COMMAND
}

function killBeforeAuthorization(child: ChildProcessWithoutNullStreams): void {
  try {
    child.stdin.end()
  } catch {
    void 0
  }

  try {
    child.kill()
  } catch {
    void 0
  }
}

async function defaultRunDirectBoundary(request: DirectBoundaryRequest): Promise<DirectBoundaryRunResult> {
  const remaining = Math.max(0, request.deadlineAt - Date.now())

  if (remaining <= MUTATION_RESERVE_MS || request.signal?.aborted) {
    return { stdout: '', stderr: 'deadline-exhausted', code: 1 }
  }

  const resourcesPayload = Buffer.from(JSON.stringify(request.resources), 'utf8').toString('base64')

  const child = spawn(
    powershellExecutable(),
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-Command',
      DIRECT_NATIVE_BOUNDARY_COMMAND
    ],
    {
      detached: false,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...fixedOsEnvironment(),
        HERMES_NATIVE_DEADLINE_AT: String(Math.trunc(request.deadlineAt)),
        HERMES_NATIVE_INSTALL_ROOT: request.installRoot,
        HERMES_NATIVE_RESOURCES_B64: resourcesPayload,
        HERMES_NATIVE_TARGET_FILETIME: request.creationFileTime,
        HERMES_NATIVE_TARGET_PID: String(request.pid)
      }
    }
  )

  return await new Promise(resolve => {
    let stdout = ''
    let stderr = ''
    let settled = false
    let authorized = false
    let spawnFailed = false
    let readyBuffer = ''

    const finish = (result: DirectBoundaryRunResult) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(readyTimer)
      clearTimeout(deadlineTimer)
      request.signal?.removeEventListener('abort', onAbort)
      resolve(result)
    }

    const authorize = () => {
      if (authorized || settled) {
        return
      }

      if (request.signal?.aborted || Date.now() >= request.deadlineAt - MUTATION_RESERVE_MS) {
        killBeforeAuthorization(child)

        return
      }

      authorized = true
      clearTimeout(readyTimer)
      child.stdin.end('GO\n')
    }

    const onAbort = () => killBeforeAuthorization(child)

    const readyTimer = setTimeout(() => killBeforeAuthorization(child), Math.max(1, remaining - MUTATION_RESERVE_MS))

    const deadlineTimer = setTimeout(() => killBeforeAuthorization(child), Math.max(1, remaining))

    child.stdout.on('data', chunk => {
      stdout = boundedAppend(stdout, chunk)
      readyBuffer = boundedAppend(readyBuffer, chunk)

      if (/(?:^|\r?\n)BOUNDARY_READY(?:\r?\n|$)/.test(readyBuffer)) {
        authorize()
      }
    })
    child.stderr.on('data', chunk => {
      stderr = boundedAppend(stderr, chunk)
    })
    child.once('error', () => {
      spawnFailed = true
      finish({ stdout: '', stderr: 'spawn-failed', code: 1 })
    })
    child.once('close', code => {
      if (spawnFailed) {
        return
      }

      finish({
        stdout,
        stderr: authorized ? '' : 'boundary-not-authorized',
        code: typeof code === 'number' ? code : 1
      })
    })

    if (request.signal) {
      if (request.signal.aborted) {
        onAbort()
      } else {
        request.signal.addEventListener('abort', onAbort, { once: true })
      }
    }
  })
}

export function parseTerminateScriptOutput(stdout: string, code: number): ForceReleaseTerminateResult {
  const matches = String(stdout || '')
    .split(/\r?\n/)
    .map(line => line.match(/^RESULT:([A-Z_]+)$/)?.[1])
    .filter((value): value is string => Boolean(value))

  const result = matches.length === 1 ? matches[0] : undefined

  switch (result) {
    case 'TERMINATED':
      return code === 0 ? { kind: 'terminated' } : { kind: 'failed', detail: 'protocol-failed' }

    case 'ALREADY_GONE':
      return code === 0 ? { kind: 'already-gone' } : { kind: 'failed', detail: 'protocol-failed' }

    case 'GENERATION_MISMATCH':
      return { kind: 'create-time-mismatch' }

    case 'PERMISSION_REQUIRED':
      return code === 5 ? { kind: 'permission-required', win32Error: 5 } : { kind: 'failed', detail: 'protocol-failed' }

    case 'IMAGE_OUT_OF_SCOPE':

    case 'RESOURCE_OUT_OF_SCOPE':

    case 'SCOPE_UNREADABLE':
      return { kind: 'failed', detail: 'scope-mismatch' }

    case 'OWNERSHIP_STALE':
      return { kind: 'failed', detail: 'ownership-stale' }

    case 'OWNERSHIP_UNKNOWN':
      return { kind: 'failed', detail: 'ownership-unknown' }

    case 'IDENTITY_UNREADABLE':
      return { kind: 'failed', detail: 'identity-unreadable' }

    case 'TIMEOUT':
      return { kind: 'failed', detail: 'deadline-exhausted' }

    case 'DRAIN_FAILED':
      return { kind: 'failed', detail: 'target-drain-failed' }

    default:
      return { kind: 'failed', detail: 'boundary-failed' }
  }
}

function exactResources(target: ForceReleaseHolder): string[] {
  return Array.from(
    new Set(
      [...(target.resources ?? []), ...(target.resource ? [target.resource] : [])].filter(
        resource => typeof resource === 'string' && resource.length > 0
      )
    )
  )
}

export async function terminateWindowsHolderExact(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunDirectBoundary,
    timeoutMs = 4_000,
    signal,
    deadlineAt,
    installRoot
  }: {
    platform?: NodeJS.Platform
    run?: RunDirectBoundary
    timeoutMs?: number
    signal?: AbortSignal
    deadlineAt?: number
    installRoot: string
  }
): Promise<ForceReleaseTerminateResult> {
  const resources = exactResources(target)
  const requestedDeadline = Date.now() + Math.max(0, Math.trunc(timeoutMs))

  const absoluteDeadline =
    typeof deadlineAt === 'number' && Number.isFinite(deadlineAt)
      ? Math.min(requestedDeadline, Math.trunc(deadlineAt))
      : requestedDeadline

  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }

  if (signal?.aborted || absoluteDeadline - Date.now() <= MUTATION_RESERVE_MS) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }

  if (!Number.isSafeInteger(target.pid) || target.pid <= 0) {
    return { kind: 'failed', detail: 'invalid-claim' }
  }

  if (
    !target.creationFileTime ||
    !FILETIME_PATTERN.test(target.creationFileTime) ||
    BigInt(target.creationFileTime) <= 0n
  ) {
    return { kind: 'failed', detail: 'identity-unavailable' }
  }

  if (
    typeof installRoot !== 'string' ||
    installRoot.length === 0 ||
    installRoot.length > MAX_PATH_CHARS ||
    resources.length === 0 ||
    resources.length > MAX_RESOURCES ||
    resources.some(resource => resource.length > MAX_PATH_CHARS)
  ) {
    return { kind: 'failed', detail: 'invalid-claim' }
  }

  const controller = new AbortController()
  let expired = false
  const onCallerAbort = () => controller.abort()
  signal?.addEventListener('abort', onCallerAbort, { once: true })

  const deadlineTimer = setTimeout(
    () => {
      expired = true
      controller.abort()
    },
    Math.max(1, absoluteDeadline - Date.now())
  )

  try {
    const result = await run({
      deadlineAt: absoluteDeadline,
      installRoot,
      pid: target.pid,
      creationFileTime: target.creationFileTime,
      resources,
      signal: controller.signal
    })

    if (expired || (controller.signal.aborted && Date.now() >= absoluteDeadline)) {
      return { kind: 'failed', detail: 'deadline-exhausted' }
    }

    return parseTerminateScriptOutput(result.stdout, result.code)
  } finally {
    clearTimeout(deadlineTimer)
    signal?.removeEventListener('abort', onCallerAbort)
  }
}

export async function terminateWindowsHolderWithinDeadline(
  target: ForceReleaseHolder,
  {
    platform = process.platform,
    run = defaultRunDirectBoundary,
    budgetMs,
    deadlineAt,
    signal,
    installRoot
  }: {
    platform?: NodeJS.Platform
    run?: RunDirectBoundary
    budgetMs: number
    deadlineAt: number
    signal?: AbortSignal
    installRoot: string
  }
): Promise<ForceReleaseTerminateResult> {
  const budget = Math.min(Math.max(0, Math.trunc(budgetMs)), Math.max(0, Math.trunc(deadlineAt - Date.now())))

  if (budget <= MUTATION_RESERVE_MS || signal?.aborted) {
    return { kind: 'failed', detail: 'deadline-exhausted' }
  }

  return terminateWindowsHolderExact(target, {
    platform,
    run,
    timeoutMs: budget,
    signal,
    deadlineAt,
    installRoot
  })
}

export type ProcessIdentity = { pid: number; createdAt?: number }

const IDENTITY_PROBE_COMMAND = String.raw`
$ErrorActionPreference = 'Stop'
$payload = [Environment]::GetEnvironmentVariable('HERMES_IDENTITY_PROBE_B64', 'Process')
$rows = @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json)
$live = @()
foreach ($row in $rows) {
  $targetPid = [int]$row.pid
  if ($targetPid -le 0) { $live += $row; continue }
  try {
    $process = Get-Process -Id $targetPid -ErrorAction Stop
  } catch {
    if ($_.CategoryInfo.Category -eq [Management.Automation.ErrorCategory]::ObjectNotFound) { continue }
    $live += $row
    continue
  }
  if ($null -eq $row.createdAt) { $live += $row; continue }
  try {
    $actual = [DateTimeOffset]::new($process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
    if ([Math]::Abs($actual - [double]$row.createdAt) -le 1.5) { $live += $row }
  } catch {
    $live += $row
  }
}
@{ live = $live } | ConvertTo-Json -Compress -Depth 3
`.trim()

/**
 * Compatibility liveness probe for the elevated-helper transport. Unknown or
 * unreadable state stays present; only explicit PID absence or generation
 * mismatch proves the retained identity is gone.
 */
export async function identitiesStillPresent(identities: readonly ProcessIdentity[]): Promise<ProcessIdentity[]> {
  const valid = identities.filter(identity => Number.isSafeInteger(identity.pid) && identity.pid > 0)

  if (valid.length === 0) {
    return []
  }

  const payload = Buffer.from(JSON.stringify(valid), 'utf8').toString('base64')

  try {
    const { stdout } = await execFileAsync(
      powershellExecutable(),
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', IDENTITY_PROBE_COMMAND],
      {
        encoding: 'utf8',
        timeout: 2_000,
        windowsHide: true,
        maxBuffer: 64 * 1024,
        env: { ...fixedOsEnvironment(), HERMES_IDENTITY_PROBE_B64: payload }
      }
    )

    const parsed = JSON.parse(String(stdout || ''))

    if (!parsed || !Array.isArray(parsed.live)) {
      return [...valid]
    }

    const requested = new Map(valid.map(identity => [identity.pid, identity] as const))
    const result: ProcessIdentity[] = []

    for (const row of parsed.live) {
      const pid = Number(row?.pid)
      const identity = requested.get(pid)

      if (identity) {
        result.push(identity)
      }
    }

    return result
  } catch {
    return [...valid]
  }
}
