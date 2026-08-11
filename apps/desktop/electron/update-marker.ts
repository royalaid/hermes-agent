/**
 * In-app update mutual-exclusion marker (#50238).
 *
 * The Tauri updater writes HERMES_HOME/.hermes-update-in-progress for the whole
 * duration of an `--update` run (see apps/bootstrap-installer/src-tauri/src/
 * update.rs `UpdateMarkerGuard`). The marker body is two lines: the updater's
 * pid and the unix-seconds it started.
 *
 * Why: if the user relaunches the desktop mid-update — the window vanished with
 * no progress and looks crashed — a fresh instance must NOT spawn its own local
 * backend. That backend re-locks the venv shim and makes the updater refuse or
 * fail mutation, leaving the launch stuck behind a half-finished update. The
 * desktop gates local backend startup on this marker and parks until the update
 * finishes.
 *
 * This module holds the PURE, side-effect-light logic (path, pid liveness,
 * parse + staleness) so it is unit-testable without booting Electron. The
 * polling/boot-progress wrapper lives in main.ts where the boot-progress and
 * log sinks are.
 */

import fs from 'fs'
import { randomUUID } from 'node:crypto'
import path from 'path'

import { getCachedWindowsProcessCreatedAt } from './windows-process-identity'

// Expected update duration, retained as a diagnostic threshold. This is NOT a
// lease expiry: a process proven live continues to own the marker past this
// age because dependency installation or a Desktop rebuild can legitimately
// run longer. Only proven owner death or PID reuse releases the gate; invalid
// or unreadable marker data remains blocking.
export const UPDATE_MARKER_MAX_AGE_MS = 20 * 60 * 1000
export const UPDATE_MARKER_CLOCK_SKEW_MS = 5 * 1000

export type ProcessCreateTimeProbe = (pid: number) => number | null | undefined
export type PidIdentityStatus = 'matching' | 'stale' | 'unknown'
export type UpdateMarkerBlockedReason = 'unreadable' | 'malformed' | 'future' | 'cleanup-race'

export interface LiveUpdateMarker {
  kind: 'live'
  pid: number
  startedAt: number
  ageMs: number
  overdue: boolean
}

export interface UnreadableUpdateMarker {
  kind: 'unreadable'
  reason: UpdateMarkerBlockedReason
  pid: null
  ageMs: null
  overdue: null
}

export type UpdateMarkerState = LiveUpdateMarker | UnreadableUpdateMarker

interface PidIdentityProbeOptions {
  kill?: typeof process.kill
  getProcessCreatedAt?: ProcessCreateTimeProbe
}

interface UpdateMarkerReadOptions extends PidIdentityProbeOptions {
  now?: () => number
  maxAgeMs?: number
}

export function markerPath(hermesHome) {
  return path.join(hermesHome, '.hermes-update-in-progress')
}

// Signal 0 does not deliver a signal — it probes existence/permission. Only
// ESRCH proves that no process exists. EPERM/EACCES and unknown host errors are
// inconclusive and therefore stay "alive" for this safety gate; otherwise a
// transient Windows probe failure could delete an active updater's marker.
// Injectable `kill` keeps it unit-testable.
export function isPidAlive(pid, kill: typeof process.kill = process.kill.bind(process)) {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return false
  }

  try {
    kill(pid, 0)

    return true
  } catch (err: any) {
    return err?.code !== 'ESRCH'
  }
}

/**
 * Classify whether a PID is still the process that wrote a whole-second claim.
 *
 * Only ESRCH or a process created at least one full second after the marker is
 * definitive staleness. Permission errors and unavailable creation-time probes
 * are unknown so safety gates can block while adoption paths require a proven
 * match. No subprocess is launched here; callers may inject a cached or native
 * creation-time probe without blocking Electron's main event loop.
 */
export function probePidIdentity(
  pid: number,
  startedAtSeconds: number,
  {
    kill = process.kill.bind(process),
    getProcessCreatedAt
  }: PidIdentityProbeOptions = {}
): PidIdentityStatus {
  if (
    !Number.isSafeInteger(pid) ||
    pid <= 0 ||
    !Number.isSafeInteger(startedAtSeconds) ||
    startedAtSeconds <= 0
  ) {
    return 'stale'
  }

  try {
    kill(pid, 0)
  } catch (error: any) {
    if (error?.code === 'ESRCH') {
      return 'stale'
    }
    // Access and host-probe errors do not prove death. A creation-time probe
    // may still establish the exact identity; otherwise the result is unknown.
  }

  if (!getProcessCreatedAt) {
    return 'unknown'
  }

  let createdAt: number | null | undefined

  try {
    createdAt = getProcessCreatedAt(pid)
  } catch {
    return 'unknown'
  }

  if (!Number.isFinite(createdAt) || Number(createdAt) <= 0) {
    return 'unknown'
  }

  return Number(createdAt) < startedAtSeconds + 1 ? 'matching' : 'stale'
}

/** Fail-closed ownership check used by readers that only need block/release. */
export function pidMatchesUpdateOwner(
  pid: number,
  startedAtSeconds: number,
  options: PidIdentityProbeOptions = {}
): boolean {
  return probePidIdentity(pid, startedAtSeconds, options) !== 'stale'
}

function parsePositiveInteger(line: string): number | null {
  const normalized = line.trim()

  if (!/^[0-9]+$/.test(normalized)) {
    return null
  }

  const value = Number(normalized)

  return Number.isSafeInteger(value) && value > 0 ? value : null
}

function parseMarker(raw: string): { pid: number; startedAt: number } | null {
  const lines = raw.split(/\r?\n/)

  if (lines.at(-1) === '') {
    lines.pop()
  }

  if (lines.length !== 2) {
    return null
  }

  const pid = parsePositiveInteger(lines[0])
  const startedAt = parsePositiveInteger(lines[1])

  return pid === null || startedAt === null ? null : { pid, startedAt }
}

type RestoreResult = 'restored' | 'superseded' | 'unresolved'

function isAlreadyExists(error: unknown): boolean {
  return (error as NodeJS.ErrnoException | undefined)?.code === 'EEXIST'
}

function retireTombstone(tombstone: string): void {
  try {
    fs.unlinkSync(tombstone)
  } catch {
    void 0
  }
}

function restoreIsolatedMarker(file: string, tombstone: string): RestoreResult {
  // linkSync is an atomic publish-if-absent. Unlike rename, it cannot replace
  // a newer claimant that appears while cleanup is restoring moved bytes.
  try {
    fs.linkSync(tombstone, file)
    retireTombstone(tombstone)

    return 'restored'
  } catch (error) {
    if (isAlreadyExists(error)) {
      retireTombstone(tombstone)

      return 'superseded'
    }
  }

  // Some filesystems cannot hard-link. Copy through O_EXCL as a fail-closed
  // fallback: a foreign replacement wins without ever being overwritten.
  let payload: Buffer

  try {
    payload = fs.readFileSync(tombstone)
  } catch {
    return 'unresolved'
  }

  let descriptor: number | null = null
  let openedDestination = false

  try {
    descriptor = fs.openSync(file, 'wx', 0o600)
    openedDestination = true
    fs.writeFileSync(descriptor, payload)
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null
    retireTombstone(tombstone)

    return 'restored'
  } catch (error) {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor)
      } catch {
        void 0
      }
    }

    if (!openedDestination && isAlreadyExists(error)) {
      retireTombstone(tombstone)

      return 'superseded'
    }

    return 'unresolved'
  }
}

type CleanupResult = 'retry' | 'unresolved'

function recoveryArtifacts(file: string): string[] | null {
  const directory = path.dirname(file)
  const prefix = `${path.basename(file)}.cas-`

  try {
    return fs
      .readdirSync(directory)
      .filter(name => name.startsWith(prefix))
      .map(name => path.join(directory, name))
  } catch (error: any) {
    return error?.code === 'ENOENT' ? [] : null
  }
}

function removeMarkerIfExact(file: string, expectedRaw: string): CleanupResult {
  const tombstone = `${file}.cas-release-${process.pid}-${randomUUID()}`

  try {
    fs.renameSync(file, tombstone)
  } catch {
    // The marker may have disappeared or changed before rename. Reread the
    // authoritative path; repeated permission failures eventually fail closed.
    return 'retry'
  }

  let isolated: string | null = null

  try {
    isolated = fs.readFileSync(tombstone, 'utf8')
  } catch {
    void 0
  }

  if (isolated !== expectedRaw) {
    return restoreIsolatedMarker(file, tombstone) === 'unresolved' ? 'unresolved' : 'retry'
  }

  try {
    fs.unlinkSync(tombstone)

    // Reread even after exact removal: a newer claimant may already occupy the
    // path and must be returned by this call rather than hidden until a poll.
    return 'retry'
  } catch {
    return restoreIsolatedMarker(file, tombstone) === 'unresolved' ? 'unresolved' : 'retry'
  }
}

function blockedMarker(reason: UpdateMarkerBlockedReason): UnreadableUpdateMarker {
  return { kind: 'unreadable', reason, pid: null, ageMs: null, overdue: null }
}

/**
 * Read + interpret the marker.
 *
 * Returns a live owner while its PID remains alive, regardless of age. A
 * non-ENOENT read failure returns a truthy `unreadable` state so callers park
 * instead of starting a backend while marker ownership is unprovable.
 * Malformed, future-dated, or unreadable markers remain truthy and block.
 * Proven-dead or PID-reused markers self-heal through exact-byte tombstone
 * cleanup; an absent marker returns `null`.
 *
 * Pure-ish: file I/O against the given path, plus an injectable pid probe and
 * clock for tests.
 */
export function readLiveUpdateMarker(
  hermesHome,
  {
    kill,
    getProcessCreatedAt = kill ? undefined : getCachedWindowsProcessCreatedAt,
    now = Date.now,
    maxAgeMs = UPDATE_MARKER_MAX_AGE_MS
  }: UpdateMarkerReadOptions = {}
): UpdateMarkerState | null {
  const file = markerPath(hermesHome)

  // A bounded reread closes cleanup races without letting a continuously
  // replaced marker spin Electron's main event loop.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    let raw: string

    try {
      raw = fs.readFileSync(file, 'utf8')
    } catch (error: any) {
      if (error?.code === 'ENOENT') {
        const artifacts = recoveryArtifacts(file)

        if (artifacts === null) {
          return blockedMarker('cleanup-race')
        }

        if (artifacts.length === 0) {
          return null
        }

        const releasePrefix = `${path.basename(file)}.cas-release-`

        if (artifacts.length === 1 && path.basename(artifacts[0]).startsWith(releasePrefix)) {
          if (restoreIsolatedMarker(file, artifacts[0]) === 'unresolved') {
            return blockedMarker('cleanup-race')
          }

          continue
        }

        return blockedMarker('cleanup-race')
      }

      return blockedMarker('unreadable')
    }

    const artifacts = recoveryArtifacts(file)

    if (artifacts === null || artifacts.length > 0) {
      return blockedMarker('cleanup-race')
    }

    const parsed = parseMarker(String(raw))

    if (!parsed) {
      return blockedMarker('malformed')
    }

    const { pid, startedAt } = parsed
    const ageMs = now() - startedAt * 1000

    if (!Number.isFinite(ageMs) || ageMs < -UPDATE_MARKER_CLOCK_SKEW_MS) {
      return blockedMarker('future')
    }

    const identity = probePidIdentity(pid, startedAt, { kill, getProcessCreatedAt })

    if (identity === 'stale') {
      if (removeMarkerIfExact(file, String(raw)) === 'unresolved') {
        return blockedMarker('cleanup-race')
      }

      continue
    }

    return { kind: 'live', pid, startedAt, ageMs, overdue: ageMs > maxAgeMs }
  }

  return blockedMarker('cleanup-race')
}

/**
 * Whether a NEW updater hand-off must be refused because a different,
 * already-alive updater currently owns the marker (#75778).
 *
 * A user who retries while a prior updater is parked mid-run must not launch
 * a second updater over the same checkout. Desktop never writes this marker;
 * each updater must atomically claim it with its own PID before handoff is
 * acknowledged.
 *
 * Returns the live foreign owner (with a ready-to-show message) when the
 * hand-off must be refused, or `null` when it's safe to spawn — no marker,
 * or the existing one is stale/dead and self-heals via
 * `readLiveUpdateMarker`.
 */
export function updateHandoffConflict(
  hermesHome,
  opts: UpdateMarkerReadOptions = {}
) {
  const owner = readLiveUpdateMarker(hermesHome, opts)

  if (!owner) {
    return null
  }

  if (owner.kind === 'unreadable') {
    return {
      pid: null,
      ageMs: null,
      message:
        'Hermes found an update marker but could not verify its owner safely. ' +
        'Wait for the current update to finish or resolve the marker permissions, then retry.'
    }
  }

  const mins = Math.floor(owner.ageMs / 60_000)
  const secs = Math.floor((owner.ageMs % 60_000) / 1000)
  const elapsed = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`

  return {
    pid: owner.pid,
    ageMs: owner.ageMs,
    message: `An update is already running (PID ${owner.pid}, started ${elapsed} ago). Wait for it to finish, then try again.`
  }
}
