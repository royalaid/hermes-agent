import { spawn, type SpawnOptions } from 'node:child_process'
import { statSync } from 'node:fs'
import path from 'node:path'
import type { Readable } from 'node:stream'

import { hiddenWindowsChildOptions } from './windows-child-options'
import { queryWindowsProcessCreatedAt, queryWindowsProcessCreationFileTime } from './windows-process-identity'

export const STAGED_UPDATER_BRIDGE_LEASE_ENV = 'HERMES_UPDATE_BRIDGE_LEASE_ID'

const WINDOWS_HANDOFF_ENV = {
  branch: 'HERMES_UPDATE_HANDOFF_BRANCH',
  desktopPid: 'HERMES_UPDATE_HANDOFF_DESKTOP_PID',
  installRoot: 'HERMES_UPDATE_HANDOFF_INSTALL_ROOT',
  relaunchAppPath: 'HERMES_UPDATE_HANDOFF_RELAUNCH_APP_PATH',
  relaunchExe: 'HERMES_UPDATE_HANDOFF_RELAUNCH_EXE',
  script: 'HERMES_UPDATE_HANDOFF_SCRIPT'
} as const

const BRIDGE_LEASE_ID_PATTERN = /^[A-Za-z0-9._-]{16,128}$/

const INHERITED_UPDATE_CONTROL_ENV = [
  /^HERMES_DESKTOP_UPDATE_TEST$/i,
  /^HERMES_INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS$/i,
  /^HERMES_SELFTEST_/i,
  /^HERMES_TEST_/i,
  /^HERMES_FORCE_RELEASE_(?:FORCE_|TEST_)/i,
  /^HERMES_TERMINATE_/i,
  /^HERMES_DEFERRED_GATEWAY_STARTUP_GATE$/i,
  /^HERMES_UPDATE_HANDOFF_/i,
  /^HERMES_UPDATE_BRIDGE_LEASE_ID$/i
]

/** Prevent ambient controls from changing a production handoff. */
function sanitizeWindowsUpdateEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(
    Object.entries(env).filter(([name]) => !INHERITED_UPDATE_CONTROL_ENV.some(pattern => pattern.test(name)))
  )
}

function windowsPowerShellExecutable(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

// Keep the native argument surface byte-stable: all dynamic values travel in
// the private child environment and this fixed PowerShell program reads them
// back as values, never source text. Windows PowerShell expects UTF-16LE for
// -EncodedCommand.
const WINDOWS_HANDOFF_RUNNER = String.raw`
$ErrorActionPreference = 'Stop'
$required = @(
  'HERMES_UPDATE_HANDOFF_SCRIPT',
  'HERMES_UPDATE_HANDOFF_INSTALL_ROOT',
  'HERMES_UPDATE_HANDOFF_BRANCH',
  'HERMES_UPDATE_HANDOFF_DESKTOP_PID',
  'HERMES_UPDATE_HANDOFF_RELAUNCH_EXE',
  'HERMES_UPDATE_BRIDGE_LEASE_ID'
)
foreach ($name in $required) {
  $value = [Environment]::GetEnvironmentVariable($name, 'Process')
  if ([string]::IsNullOrWhiteSpace($value)) {
    throw "Missing required update handoff value: $name"
  }
}
$scriptPath = $env:HERMES_UPDATE_HANDOFF_SCRIPT
$desktopPid = 0
if (-not [int]::TryParse($env:HERMES_UPDATE_HANDOFF_DESKTOP_PID, [ref]$desktopPid) -or $desktopPid -le 0) {
  throw 'Invalid update handoff desktop PID'
}
if ($env:HERMES_UPDATE_BRIDGE_LEASE_ID -notmatch '^[A-Za-z0-9._-]{16,128}$') {
  throw 'Invalid update handoff bridge lease ID'
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
  throw 'Update handoff script is missing'
}
$scriptArgs = @{
  InstallRoot = $env:HERMES_UPDATE_HANDOFF_INSTALL_ROOT
  Branch = $env:HERMES_UPDATE_HANDOFF_BRANCH
  DesktopPid = $desktopPid
  RelaunchExe = $env:HERMES_UPDATE_HANDOFF_RELAUNCH_EXE
  BridgeLeaseId = $env:HERMES_UPDATE_BRIDGE_LEASE_ID
}
if (-not [string]::IsNullOrWhiteSpace($env:HERMES_UPDATE_HANDOFF_RELAUNCH_APP_PATH)) {
  $scriptArgs.RelaunchAppPath = $env:HERMES_UPDATE_HANDOFF_RELAUNCH_APP_PATH
}
& $scriptPath @scriptArgs
if ($null -eq $LASTEXITCODE) { exit 1 }
exit $LASTEXITCODE
`.trim()

const WINDOWS_HANDOFF_RUNNER_ENCODED_COMMAND = Buffer.from(WINDOWS_HANDOFF_RUNNER, 'utf16le').toString('base64')

// The directly tracked bridge is short-lived. It starts the actual writer with
// an explicit hidden window style, publishes only that process's exact PID and
// creation FILETIME, then exits. Neither the bridge PID nor untrusted output is
// allowed to become update ownership authority.
const WINDOWS_HANDOFF_LAUNCHER = String.raw`
$ErrorActionPreference = 'Stop'
try {
  $powerShell = Join-Path $PSHOME 'powershell.exe'
  if (-not (Test-Path -LiteralPath $powerShell -PathType Leaf)) { exit 20 }
  $child = Start-Process -FilePath $powerShell -ArgumentList @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-EncodedCommand',
    '${WINDOWS_HANDOFF_RUNNER_ENCODED_COMMAND}'
  ) -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
  if ($null -eq $child -or $child.Id -le 0) { exit 21 }
  $created = $child.StartTime.ToUniversalTime().ToFileTimeUtc()
  [Console]::Out.WriteLine("HERMES_UPDATE_HANDOFF_PID=$($child.Id);FILETIME=$created")
  [Console]::Out.Flush()
  exit 0
} catch {
  exit 22
}
`.trim()

const WINDOWS_HANDOFF_ENCODED_COMMAND = Buffer.from(WINDOWS_HANDOFF_LAUNCHER, 'utf16le').toString('base64')

export interface UpdaterChild {
  pid?: number
  kill: (signal?: NodeJS.Signals | number) => boolean
  stdout?: Readable | null
  once?: (event: string, listener: (...args: any[]) => void) => unknown
  removeListener?: (event: string, listener: (...args: any[]) => void) => unknown
  unref: () => void
}

export interface ResolveUpdateScriptHandoffDeps {
  isWindows?: boolean
  fileExists?: (candidate: string) => boolean
}

export interface UpdateScriptHandoff {
  command: string
  args: string[]
  scriptPath: string
}

export type WindowsUpdateTransport = { kind: 'script'; handoff: UpdateScriptHandoff } | { kind: 'manual' }

export interface WindowsUpdateHandoffValues {
  bridgeLeaseId: string
  branch: string
  desktopPid: number
  installRoot: string
  relaunchAppPath?: string
  relaunchExe: string
}

export interface DetachedWindowsHandoff {
  command: string
  args: string[]
  env: Record<string, string>
}

export type WindowsUpdateLaunchResult =
  { kind: 'manual' } | { kind: 'spawned'; child: UpdaterChild; handoff: UpdateScriptHandoff }

/**
 * Repo-owned Windows update hand-off (frozen-binary escape hatch).
 *
 * The staged Tauri `hermes-setup.exe` has no self-update path, so every
 * updater-side fix only reaches users when a new binary is built, signed and
 * published — which historically lags main by months and strands users on
 * long-fixed bugs (cache resolver #67369, marker self-adopt #74782; the
 * 2026-08-09 incident chain). `scripts/desktop-update.ps1` lives in the repo
 * checkout instead: every `hermes update` refreshes the code that drives the
 * NEXT update, and only PowerShell itself is frozen.
 *
 * Returns the spawn recipe when the script exists in the checkout, or null.
 * Ordinary Windows updates fail closed to a manual command when it is absent;
 * a staged installer is reserved for bootstrap recovery and is not a protocol
 * fallback. POSIX uses its own detached hand-off resolver below.
 */
export function resolveUpdateScriptHandoff(
  updateRoot: string,
  deps: ResolveUpdateScriptHandoffDeps = {}
): UpdateScriptHandoff | null {
  const isWindows = deps.isWindows ?? process.platform === 'win32'

  if (!isWindows) {
    return null
  }

  const exists = deps.fileExists ?? stagedFileExists

  // The transactional Desktop protocol is implemented only by the hardened
  // flat script on this branch. The upstream nested Edge handoff uses another
  // wire contract, so treating it as a fallback would launch an incompatible
  // updater after a partial/skewed checkout. Fail closed when the flat script
  // is absent.
  const scriptPath = path.join(updateRoot, 'scripts', 'desktop-update.ps1')

  if (exists(scriptPath)) {
    return {
      command: 'powershell',
      args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath],
      scriptPath
    }
  }

  return null
}

/** Resolve the only transport allowed for an ordinary Windows update. */
export function resolveWindowsUpdateTransport(
  updateRoot: string,
  deps: ResolveUpdateScriptHandoffDeps = {}
): WindowsUpdateTransport {
  const handoff = resolveUpdateScriptHandoff(updateRoot, deps)

  return handoff ? { kind: 'script', handoff } : { kind: 'manual' }
}

/**
 * Repo-owned POSIX update hand-off (the mac/linux twin of the above).
 *
 * Replaces the in-app posix updater: the Desktop spawns the script detached
 * and QUITS, the script waits it out, runs `hermes update`, swaps/relaunches
 * the app, and writes .hermes-update-result.json. With the app gone before
 * the update starts, the HERMES_DESKTOP_CHILD_PID reaper-exclusion dance is
 * unnecessary — there are no live desktop backends to spare.
 *
 * Null when the checkout predates the script (caller surfaces the manual
 * `hermes update` card — old checkouts pull the script on their next update).
 */
export function resolvePosixScriptHandoff(
  updateRoot: string,
  deps: ResolveUpdateScriptHandoffDeps = {}
): UpdateScriptHandoff | null {
  const isWindows = deps.isWindows ?? process.platform === 'win32'

  if (isWindows) {
    return null
  }

  const scriptPath = path.join(updateRoot, 'scripts', 'desktop-update', 'posix.sh')
  const exists = deps.fileExists ?? stagedFileExists

  if (!exists(scriptPath)) {
    return null
  }

  return {
    command: '/bin/bash',
    args: [scriptPath],
    scriptPath
  }
}

/**
 * Compose a fixed hidden PowerShell bridge. It starts the actual hidden writer
 * and emits only that process's PID plus exact creation FILETIME. Dynamic
 * values remain outside argv and windowsHide prevents console allocation.
 */
export function wrapHandoffForDetachedConsole(
  handoff: UpdateScriptHandoff,
  values: WindowsUpdateHandoffValues
): DetachedWindowsHandoff {
  if (!Number.isSafeInteger(values.desktopPid) || values.desktopPid <= 0) {
    throw new Error('Windows update handoff requires a positive desktop PID')
  }

  const requiredValues = [
    handoff.scriptPath,
    values.installRoot,
    values.branch,
    values.relaunchExe,
    values.bridgeLeaseId
  ]

  if (requiredValues.some(value => typeof value !== 'string' || value.trim().length === 0)) {
    throw new Error('Windows update handoff requires every environment value')
  }

  if (!BRIDGE_LEASE_ID_PATTERN.test(values.bridgeLeaseId)) {
    throw new Error('Windows update handoff requires a valid bridge lease ID')
  }

  return {
    command: windowsPowerShellExecutable(),
    args: [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-EncodedCommand',
      WINDOWS_HANDOFF_ENCODED_COMMAND
    ],
    env: {
      [STAGED_UPDATER_BRIDGE_LEASE_ENV]: values.bridgeLeaseId,
      [WINDOWS_HANDOFF_ENV.branch]: values.branch,
      [WINDOWS_HANDOFF_ENV.desktopPid]: String(values.desktopPid),
      [WINDOWS_HANDOFF_ENV.installRoot]: values.installRoot,
      [WINDOWS_HANDOFF_ENV.relaunchAppPath]: values.relaunchAppPath ?? '',
      [WINDOWS_HANDOFF_ENV.relaunchExe]: values.relaunchExe,
      [WINDOWS_HANDOFF_ENV.script]: handoff.scriptPath
    }
  }
}

/** Dev Electron starts `electron.exe <app-entry>`; preserve that entry for a
 * detached updater relaunch. Packaged Hermes.exe takes no app argument. */
export function resolveWindowsDevRelaunchAppPath(defaultApp: boolean, argv: readonly string[]): string | undefined {
  const appEntry = argv[1]

  if (!defaultApp || !appEntry || appEntry.startsWith('-')) {
    return undefined
  }

  return path.win32.resolve(appEntry)
}

/** Render argv for a PowerShell-facing manual instruction without creating
 * executable source from unquoted values. Single quotes are literal in
 * PowerShell; an embedded quote is represented by two single quotes. */
export function formatPowerShellArgvForDisplay(argv: string[]): string {
  return argv.map(value => (/^[A-Za-z0-9._/:-]+$/.test(value) ? value : `'${value.replaceAll("'", "''")}'`)).join(' ')
}

/** Apply the resolved ordinary-update policy and compose the exact production
 * spawn. A manual transport never calls spawn; a script transport merges its
 * private payload into the child environment at the final boundary. */
export function launchWindowsUpdateTransport(
  transport: WindowsUpdateTransport,
  values: WindowsUpdateHandoffValues,
  options: SpawnOptions,
  deps: SpawnUpdaterProcessDeps = {}
): WindowsUpdateLaunchResult {
  if (transport.kind === 'manual') {
    return transport
  }

  const wrapped = wrapHandoffForDetachedConsole(transport.handoff, values)

  const child = spawnUpdaterProcess(
    wrapped.command,
    wrapped.args,
    {
      ...options,
      detached: false,
      env: { ...sanitizeWindowsUpdateEnvironment(options.env ?? {}), ...wrapped.env },
      stdio: ['ignore', 'pipe', 'ignore']
    },
    deps
  )

  return { kind: 'spawned', child, handoff: transport.handoff }
}

/**
 * Electron/Chromium internal switches that must NOT be replayed on re-exec:
 * runtime artifacts of THIS launch, not user intent (ported from the deleted
 * update-relaunch.ts; #45205). `--no-sandbox` is deliberately kept — it is
 * the user's sandbox opt-out and the signal that makes a relaunch safe when
 * chrome-sandbox isn't setuid.
 */
export const INTERNAL_ARG_PREFIXES = [
  '--type=',
  '--user-data-dir=',
  '--enable-features=',
  '--disable-features=',
  '--field-trial-handle=',
  '--enable-logging',
  '--log-file=',
  '--disable-gpu-sandbox',
  '--lang=',
  '--inspect',
  '--remote-debugging-port='
]

/** Filter Electron internals from process.argv.slice(1) so the relaunched
 * app replays only user/launcher intent (deep links, app flags). */
export function collectRelaunchArgs(argv: unknown): string[] {
  if (!Array.isArray(argv)) {
    return []
  }

  return argv.filter((arg): arg is string => {
    if (typeof arg !== 'string' || arg.length === 0) {
      return false
    }

    return !INTERNAL_ARG_PREFIXES.some(prefix =>
      prefix.endsWith('=') ? arg.startsWith(prefix) : arg === prefix || arg.startsWith(prefix + '=')
    )
  })
}

/** True when the user has opted out of the SUID sandbox — the relaunch is
 * safe even if chrome-sandbox fails preflight (ported from update-relaunch.ts). */
export function sandboxFallbackFromEnv(env: Record<string, string | undefined>, launchArgs: string[]): boolean {
  const disable = String(env?.ELECTRON_DISABLE_SANDBOX || '').trim()

  if (disable === '1' || disable.toLowerCase() === 'true') {
    return true
  }

  return Array.isArray(launchArgs) && launchArgs.includes('--no-sandbox')
}

export interface ResolveStagedUpdaterBinaryDeps {
  isWindows?: boolean
  fileExists?: (candidate: string) => boolean
}

function stagedFileExists(candidate: string): boolean {
  try {
    return statSync(candidate).isFile()
  } catch {
    return false
  }
}

/**
 * Decide which staged installer binary — if any — may be handed an update.
 *
 * The Tauri installer self-copies into HERMES_HOME on *every* platform
 * (`hermes-setup.exe` on Windows, `hermes-setup` elsewhere — see
 * apps/bootstrap-installer `paths::installer_dest` and
 * `bootstrap::copy_self_to_hermes_home`), so finding that binary on macOS or
 * Linux is expected, not leftover junk.
 *
 * Handing an update to it is nonetheless a Windows-only policy. Windows needs
 * the quit -> hand-off -> rebuild dance because a venv shim file lock keeps the
 * running desktop from rewriting its own bits; macOS and Linux use the
 * repo-owned detached POSIX hand-off. Off Windows the staged-binary hand-off
 * therefore buys nothing and costs a great deal: a staged binary older
 * than the hand-off protocol holds the update marker, spawns `hermes update`,
 * and that child refuses its own parent — wedging the in-app Update button for
 * good, with no route (update, re-download, reinstall) to a newer binary
 * (#74836). Returning null off Windows is what routes those platforms to the
 * POSIX script hand-off.
 *
 * Null on Windows too when nothing is staged (a dev/source run, or a CLI
 * install that never went through the installer); callers degrade gracefully.
 */
export function resolveStagedUpdaterBinary(
  hermesHome: string,
  deps: ResolveStagedUpdaterBinaryDeps = {}
): string | null {
  const isWindows = deps.isWindows ?? process.platform === 'win32'

  if (!isWindows) {
    return null
  }

  const fileExists = deps.fileExists ?? stagedFileExists
  const candidate = path.join(hermesHome, 'hermes-setup.exe')

  return fileExists(candidate) ? candidate : null
}

export interface SpawnUpdaterProcessDeps {
  isWindows?: boolean
  spawnProcess?: (command: string, args: string[], options: SpawnOptions) => UpdaterChild
}

interface ExactUpdaterProcessDeps {
  queryCreatedAt?: (pid: number) => Promise<number | null>
  queryCreationFileTime?: (pid: number) => Promise<string | null>
}

interface WindowsHandoffOwnerIdentityDeps extends ExactUpdaterProcessDeps {
  clearTimeoutFn?: (timer: unknown) => void
  setTimeoutFn?: (callback: () => void, ms: number) => unknown
  timeoutMs?: number
}

export interface WindowsHandoffOwnerIdentity {
  pid: number
  createdAt: number
  creationFileTime: string
}

export function stagedUpdaterEnvironment(baseEnv: NodeJS.ProcessEnv, bridgeLeaseId: string): NodeJS.ProcessEnv {
  return {
    ...sanitizeWindowsUpdateEnvironment(baseEnv),
    [STAGED_UPDATER_BRIDGE_LEASE_ENV]: bridgeLeaseId
  }
}

/**
 * Resolve the actual writer started by the short-lived hidden bridge. The
 * bridge receipt is bounded and the exact creation generation is queried
 * independently from Windows before the PID can authorize adoption.
 */
export async function waitForWindowsHandoffOwnerIdentity(
  child: UpdaterChild,
  deps: WindowsHandoffOwnerIdentityDeps = {}
): Promise<WindowsHandoffOwnerIdentity | null> {
  const stdout = child.stdout

  if (!stdout || typeof stdout.on !== 'function') {
    return null
  }

  const setTimeoutFn = deps.setTimeoutFn ?? setTimeout

  const clearTimeoutFn =
    deps.clearTimeoutFn ?? ((timer: unknown) => clearTimeout(timer as ReturnType<typeof setTimeout>))

  const timeoutMs = deps.timeoutMs ?? 5_000

  const receipt = await new Promise<{ pid: number; creationFileTime: string } | null>(resolve => {
    let settled = false
    let buffer = ''

    const finish = (value: { pid: number; creationFileTime: string } | null) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeoutFn(timer)
      stdout.removeListener('data', onData)
      stdout.removeListener('end', onEnd)
      stdout.removeListener('error', onStreamError)
      child.removeListener?.('error', onChildError)
      child.removeListener?.('exit', onChildExit)
      resolve(value)
    }

    const parse = (): { pid: number; creationFileTime: string } | null | undefined => {
      const match = /^HERMES_UPDATE_HANDOFF_PID=([1-9][0-9]{0,9});FILETIME=([1-9][0-9]{0,19})\r?\n?$/.exec(buffer)

      if (!match) {
        return undefined
      }

      const pid = Number(match[1])

      return Number.isSafeInteger(pid) && pid > 0 ? { pid, creationFileTime: match[2] } : null
    }

    const onData = (chunk: Buffer | string) => {
      buffer += String(chunk)

      if (buffer.length > 128) {
        finish(null)

        return
      }

      const parsed = parse()

      if (parsed !== undefined) {
        finish(parsed)
      }
    }

    const onEnd = () => finish(parse() ?? null)
    const onStreamError = () => finish(null)
    const onChildError = () => finish(null)

    const onChildExit = (code: number | null, signal: string | null) => {
      if (signal || (typeof code === 'number' && code !== 0)) {
        finish(null)
      }
    }

    const timer = setTimeoutFn(() => finish(null), timeoutMs)

    stdout.on('data', onData)
    stdout.once('end', onEnd)
    stdout.once('error', onStreamError)
    child.once?.('error', onChildError)
    child.once?.('exit', onChildExit)
  })

  if (!receipt) {
    return null
  }

  const [createdAt, observedFileTime] = await Promise.all([
    captureSpawnedUpdaterCreatedAt(receipt.pid, deps),
    (deps.queryCreationFileTime ?? queryWindowsProcessCreationFileTime)(receipt.pid)
  ])

  return createdAt && observedFileTime === receipt.creationFileTime
    ? { pid: receipt.pid, createdAt, creationFileTime: receipt.creationFileTime }
    : null
}

/**
 * Spawn the detached installer used for update and bootstrap-recovery handoffs.
 * The helper owns both hidden-console selection and unref semantics so every
 * updater handoff follows the same behavior and can be tested without Electron.
 */
export function spawnUpdaterProcess(
  updater: string,
  updaterArgs: string[],
  options: SpawnOptions,
  deps: SpawnUpdaterProcessDeps = {}
): UpdaterChild {
  const isWindows = deps.isWindows ?? process.platform === 'win32'
  const spawnOptions = hiddenWindowsChildOptions(options, isWindows) as SpawnOptions

  const child = deps.spawnProcess
    ? deps.spawnProcess(updater, updaterArgs, spawnOptions)
    : spawn(updater, updaterArgs, spawnOptions)

  child.unref()

  return child
}

export interface UpdaterHandoffOutcome {
  ok: boolean
  /** Set when ok is false. */
  reason?: 'spawn-error' | 'early-exit'
  /** Human-readable detail for logs (never contains argv secrets). */
  message?: string
  /** Exit code when the child exited inside the settle window. */
  code?: number | null
  /** Signal when the child was killed inside the settle window. */
  signal?: string | null
}

export interface ObserveUpdaterHandoffDeps {
  setTimeoutFn?: (callback: () => void, ms: number) => unknown
  clearTimeoutFn?: (timer: unknown) => void
}

/**
 * Watch a just-spawned detached updater for the duration of the quit dwell
 * and report whether the hand-off actually became viable (#66753).
 *
 * Before this, the Desktop called `unref()` and quit after a fixed dwell
 * without ever observing the child's async `error` event (ENOENT/EACCES —
 * Node reports exec failures asynchronously) or an early `exit`. A failed
 * spawn therefore looked identical to a successful one: the app vanished, no
 * updater appeared, and nothing relaunched. Worse, an unhandled `'error'`
 * event on the detached child would crash the Electron main process outright.
 *
 * Success is: no `error` event AND either the child survives the settle
 * window or it exits 0 inside it. Failure is a spawn `error`, a non-zero exit,
 * or a signal death inside the window. For the Windows bridge, this observes
 * only bridge health; callers must adopt and then observe the exact writer
 * generation with waitForWindowsHandoffOwnerIdentity/observeUpdaterGeneration.
 *
 * Children that expose no event interface (bare test doubles) settle as ok
 * after the window — the observation is a best-effort hardening, never a new
 * way to wedge an update.
 */
export function observeUpdaterHandoff(
  child: UpdaterChild,
  settleMs: number,
  deps: ObserveUpdaterHandoffDeps = {}
): Promise<UpdaterHandoffOutcome> {
  const setTimeoutFn = deps.setTimeoutFn ?? setTimeout

  const clearTimeoutFn =
    deps.clearTimeoutFn ?? ((timer: unknown) => clearTimeout(timer as ReturnType<typeof setTimeout>))

  const observable = child as UpdaterChild & {
    once?: (event: string, listener: (...args: unknown[]) => void) => unknown
    removeListener?: (event: string, listener: (...args: unknown[]) => void) => unknown
  }

  if (typeof observable.once !== 'function') {
    return new Promise(resolve => {
      setTimeoutFn(() => resolve({ ok: true }), settleMs)
    })
  }

  return new Promise(resolve => {
    let settled = false

    const finish = (outcome: UpdaterHandoffOutcome) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeoutFn(timer)
      observable.removeListener?.('error', onError)
      observable.removeListener?.('exit', onExit)
      resolve(outcome)
    }

    const onError = (...args: unknown[]) => {
      const error = args[0] as (Error & { code?: string }) | undefined

      finish({
        ok: false,
        reason: 'spawn-error',
        message: `updater spawn failed: ${error?.code || error?.message || 'unknown error'}`
      })
    }

    const onExit = (...args: unknown[]) => {
      const code = args[0] as number | null
      const signal = args[1] as string | null

      if (signal || (typeof code === 'number' && code !== 0)) {
        finish({
          ok: false,
          reason: 'early-exit',
          message: signal
            ? `updater died from signal ${signal} before the settle window elapsed`
            : `updater exited ${code} before the settle window elapsed`,
          code: code ?? null,
          signal: signal ?? null
        })

        return
      }

      // A fixed launcher may exit cleanly after publishing its child receipt.
      // The child receipt still requires independent generation observation.
      finish({ ok: true, code: code ?? 0, signal: null })
    }

    const timer = setTimeoutFn(() => finish({ ok: true }), settleMs)

    observable.once('error', onError)
    observable.once('exit', onExit)
  })
}

interface ObserveUpdaterGenerationDeps extends ExactUpdaterProcessDeps {
  wait?: (delayMs: number) => Promise<void>
}

/** Hold the quit boundary until the exact adopted writer generation survives. */
export async function observeUpdaterGeneration(
  pid: number,
  expectedCreationFileTime: string,
  settleMs: number,
  deps: ObserveUpdaterGenerationDeps = {}
): Promise<boolean> {
  if (
    !Number.isSafeInteger(pid) ||
    pid <= 0 ||
    !/^[1-9][0-9]{0,19}$/.test(expectedCreationFileTime) ||
    !Number.isFinite(settleMs) ||
    settleMs < 0
  ) {
    return false
  }

  const wait = deps.wait ?? (delayMs => new Promise(resolve => setTimeout(resolve, delayMs)))

  if (settleMs > 0) {
    await wait(settleMs)
  }

  try {
    return (await (deps.queryCreationFileTime ?? queryWindowsProcessCreationFileTime)(pid)) === expectedCreationFileTime
  } catch {
    return false
  }
}

/** Capture the OS creation identity immediately after an updater spawn. */
export async function captureSpawnedUpdaterCreatedAt(
  pid: number,
  { queryCreatedAt = queryWindowsProcessCreatedAt }: ExactUpdaterProcessDeps = {}
): Promise<number | null> {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return null
  }

  let createdAt: number | null

  try {
    createdAt = await queryCreatedAt(pid)
  } catch {
    return null
  }

  return Number.isSafeInteger(createdAt) && Number(createdAt) > 0 ? Number(createdAt) : null
}

/** Probe the retained spawned generation without reopening its numeric PID. */
export function isSpawnedUpdaterGenerationActive(child: UpdaterChild): boolean {
  try {
    return child.kill(0)
  } catch {
    return false
  }
}

/**
 * Stop only the exact spawned updater generation. A missing, denied, exited,
 * or PID-reused process is retained rather than risking an unrelated target.
 */
export async function terminateSpawnedUpdaterIfExact(
  child: UpdaterChild,
  expectedCreatedAt: number,
  { queryCreatedAt = queryWindowsProcessCreatedAt }: ExactUpdaterProcessDeps = {}
): Promise<boolean> {
  const pid = child.pid

  if (
    !Number.isSafeInteger(pid) ||
    Number(pid) <= 0 ||
    !Number.isSafeInteger(expectedCreatedAt) ||
    expectedCreatedAt <= 0
  ) {
    return false
  }

  const currentCreatedAt = await captureSpawnedUpdaterCreatedAt(Number(pid), { queryCreatedAt })

  if (currentCreatedAt !== expectedCreatedAt) {
    return false
  }

  try {
    // ChildProcess.kill routes through libuv's retained process handle on
    // Windows. Unlike process.kill(pid), it cannot reopen a replacement that
    // reused the updater's numeric PID after the identity proof above.
    return child.kill()
  } catch {
    return false
  }
}
