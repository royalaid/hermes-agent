import { spawn, type SpawnOptions } from 'node:child_process'
import { statSync } from 'node:fs'
import path from 'node:path'

import { hiddenWindowsChildOptions } from './windows-child-options'
import { queryWindowsProcessCreatedAt } from './windows-process-identity'

export const STAGED_UPDATER_BRIDGE_LEASE_ENV = 'HERMES_UPDATE_BRIDGE_LEASE_ID'

export interface UpdaterChild {
  pid?: number
  kill: (signal?: NodeJS.Signals | number) => boolean
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
 * Returns the spawn recipe when the script exists in the checkout, or null
 * (caller falls back to the staged binary — old checkouts that predate the
 * script keep working unchanged). Windows-only by the same policy as
 * resolveStagedUpdaterBinary: POSIX updates in place via
 * applyUpdatesPosixInApp and needs no hand-off at all.
 */
export function resolveUpdateScriptHandoff(
  updateRoot: string,
  deps: ResolveUpdateScriptHandoffDeps = {}
): UpdateScriptHandoff | null {
  const isWindows = deps.isWindows ?? process.platform === 'win32'

  if (!isWindows) {
    return null
  }

  const scriptPath = path.join(updateRoot, 'scripts', 'desktop-update.ps1')
  const exists = deps.fileExists ?? stagedFileExists

  if (!exists(scriptPath)) {
    return null
  }

  return {
    command: 'powershell',
    args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath],
    scriptPath
  }
}

/**
 * Wrap a PowerShell hand-off invocation so it survives a detached, hidden
 * spawn from Electron.
 *
 * Verified empirically (2026-08-09, Windows 11): `spawn('powershell', [...,
 * '-File', script], { detached: true, stdio: 'ignore', windowsHide: true })`
 * exits 0 WITHOUT executing a single line of the script. powershell.exe is a
 * console-subsystem binary; detached+windowsHide gives it no console to
 * attach to, and Windows PowerShell 5.1 dies during console init before
 * -File processing (the same class of failure as #54220's conhost work, on
 * the launch side). The same spawn with a visible console, or non-detached,
 * runs fine — so unit tests and foreground use hide the bug.
 *
 * `cmd /c start "" /min powershell ...` was the variant that survived the
 * full detached+hidden production shape in testing: `start` allocates the
 * child its own (minimized) console and fully detaches it from cmd.exe,
 * which exits immediately. The spawned pid is therefore the WRAPPER's —
 * callers must not use it as a marker owner (the script claims the marker
 * itself with its own $PID).
 */
export function wrapHandoffForDetachedConsole(
  handoff: UpdateScriptHandoff,
  extraArgs: string[]
): {
  command: string
  args: string[]
} {
  return {
    command: 'cmd.exe',
    args: ['/d', '/s', '/c', 'start', '', '/min', handoff.command, ...handoff.args, ...extraArgs]
  }
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
 * running desktop from rewriting its own bits; macOS and Linux have no such
 * lock and update in place through applyUpdatesPosixInApp(). Off Windows the
 * hand-off therefore buys nothing and costs a great deal: a staged binary older
 * than the hand-off protocol holds the update marker, spawns `hermes update`,
 * and that child refuses its own parent — wedging the in-app Update button for
 * good, with no route (update, re-download, reinstall) to a newer binary
 * (#74836). Returning null off Windows is what routes those platforms to the
 * in-app updater.
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
}

export function stagedUpdaterEnvironment(baseEnv: NodeJS.ProcessEnv, bridgeLeaseId: string): NodeJS.ProcessEnv {
  return { ...baseEnv, [STAGED_UPDATER_BRIDGE_LEASE_ENV]: bridgeLeaseId }
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

/** Capture the OS creation identity immediately after an updater spawn. */
export async function captureSpawnedUpdaterCreatedAt(
  pid: number,
  { queryCreatedAt = queryWindowsProcessCreatedAt }: ExactUpdaterProcessDeps = {}
): Promise<number | null> {
  if (!Number.isSafeInteger(pid) || pid <= 0) {return null}
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

  if (currentCreatedAt !== expectedCreatedAt) {return false}

  try {
    // ChildProcess.kill routes through libuv's retained process handle on
    // Windows. Unlike process.kill(pid), it cannot reopen a replacement that
    // reused the updater's numeric PID after the identity proof above.
    return child.kill()
  } catch {
    return false
  }
}
