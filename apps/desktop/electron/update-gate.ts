'use strict'

/**
 * update-gate.ts
 *
 * Pure, dependency-injected gate that parks local backend spawns while an
 * in-app update is running (#73822, #50238).
 *
 * Two independent signals mean "an update owns the venv right now":
 *
 *  - the on-disk marker (`HERMES_HOME/.hermes-update-in-progress`), atomically
 *    claimed by the updater with its own PID, and
 *  - the in-process `updateInFlight` flag, true for the whole
 *    either Desktop update handoff critical section.
 *
 * The marker alone is NOT enough (#73822): normal update and bootstrap
 * recovery both release tracked backends before preflight, while the updater
 * can claim its marker only after handoff. A marker-only gate can respawn a
 * backend inside that gap and create a new blocker. Consulting the shared
 * transaction closes the preflight-to-updater-claim window, and handoff
 * succeeds only after the exact updater PID owns the marker.
 */

export type UpdateGateReason = 'marker' | 'update-in-flight' | null

export interface UpdateGateDeps {
  /** True when a live on-disk update marker exists (see update-marker.ts). */
  hasLiveMarker: () => boolean
  /** True while this process is inside either update handoff transaction. */
  isUpdateInFlight: () => boolean
}

/** One synchronous, process-local mutex shared by every Desktop handoff path. */
export class UpdateInFlightTransaction {
  private active = false

  readonly isActive = (): boolean => this.active

  async run<T>(operation: () => T | Promise<T>): Promise<T> {
    if (this.active) {
      throw new Error('An update is already in progress.')
    }

    // Set before invoking operation: its first statement may already await a
    // preflight, and backend reconnects must observe the closed gate in that
    // same turn of the event loop.
    this.active = true

    try {
      return await operation()
    } finally {
      this.active = false
    }
  }
}

/** Why the gate is closed right now, or null when it is open. */
export function updateGateReason(deps: UpdateGateDeps): UpdateGateReason {
  if (deps.hasLiveMarker()) {
    return 'marker'
  }

  if (deps.isUpdateInFlight()) {
    return 'update-in-flight'
  }

  return null
}

export interface StillBlockedUpdateClearance {
  kind: 'still-blocked-timeout'
  reason: Exclude<UpdateGateReason, null>
}

export type UpdateClearanceOutcome = 'clear' | 'finished' | StillBlockedUpdateClearance

export interface WaitForUpdateClearanceOptions {
  timeoutMs: number
  pollMs: number
  /** Cancels a parked backend start (pool eviction, teardown, or app quit). */
  signal?: AbortSignal
  /** Invoked once per poll while parked (boot progress / logging). */
  onWaitTick?: (reason: Exclude<UpdateGateReason, null>) => void | Promise<void>
  now?: () => number
  sleep?: (ms: number) => Promise<void>
}

export interface WaitForLocalBackendClearanceOptions extends WaitForUpdateClearanceOptions {
  /** Called after each bounded UI wait while the safety gate remains closed. */
  onStillBlocked?: (reason: Exclude<UpdateGateReason, null>) => void | Promise<void>
}

function abortError(): Error {
  const error = new Error('The parked backend start was cancelled.')
  error.name = 'AbortError'

  return error
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw abortError()
  }
}

async function waitForPoll(
  ms: number,
  signal: AbortSignal | undefined,
  sleep?: (ms: number) => Promise<void>
): Promise<void> {
  throwIfAborted(signal)

  if (!signal) {
    await (sleep ? sleep(ms) : new Promise<void>(resolve => setTimeout(resolve, ms)))

    return
  }

  await new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | null = null
    let settled = false

    const finish = (callback: () => void) => {
      if (settled) {
        return
      }

      settled = true
      signal.removeEventListener('abort', onAbort)

      if (timer !== null) {
        clearTimeout(timer)
      }

      callback()
    }

    const onAbort = () => finish(() => reject(abortError()))

    signal.addEventListener('abort', onAbort, { once: true })

    if (sleep) {
      void sleep(ms).then(
        () => finish(resolve),
        error => finish(() => reject(error))
      )
    } else {
      timer = setTimeout(() => finish(resolve), ms)
    }

    if (signal.aborted) {
      onAbort()
    }
  })
}

/**
 * Park until no update signal remains, or the deadline passes.
 *
 * Returns 'clear' when the gate was already open (no wait happened),
 * 'finished' when it opened during the wait, and a typed still-blocked result
 * when the bounded UI wait expires. A timeout is never permission to start a
 * local backend; callers must keep the venv gate closed while the reason
 * remains present.
 */
export async function waitForUpdateClearance(
  deps: UpdateGateDeps,
  options: WaitForUpdateClearanceOptions
): Promise<UpdateClearanceOutcome> {
  const now = options.now || Date.now

  throwIfAborted(options.signal)

  let reason = updateGateReason(deps)

  if (!reason) {
    return 'clear'
  }

  const deadline = now() + options.timeoutMs

  while (reason && now() < deadline) {
    throwIfAborted(options.signal)

    if (options.onWaitTick) {
      await options.onWaitTick(reason)
    }

    await waitForPoll(options.pollMs, options.signal, options.sleep)
    reason = updateGateReason(deps)
  }

  return reason ? { kind: 'still-blocked-timeout', reason } : 'finished'
}

/**
 * Keep a local backend parked across any number of bounded UI wait windows.
 * The callback lets primary and pool callers surface actionable state without
 * turning a live or unreadable marker into permission to re-lock the venv.
 */
export async function waitForLocalBackendClearance(
  deps: UpdateGateDeps,
  options: WaitForLocalBackendClearanceOptions
): Promise<'clear' | 'finished'> {
  const { onStillBlocked, ...waitOptions } = options
  let waited = false

  while (true) {
    const outcome = await waitForUpdateClearance(deps, waitOptions)

    if (outcome === 'clear') {
      return waited ? 'finished' : 'clear'
    }

    if (outcome === 'finished') {
      return 'finished'
    }

    waited = true
    await onStillBlocked?.(outcome.reason)
  }
}
