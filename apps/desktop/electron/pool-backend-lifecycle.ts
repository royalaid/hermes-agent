import {
  type UpdateGateDeps,
  waitForLocalBackendClearance,
  type WaitForLocalBackendClearanceOptions
} from './update-gate'

export interface PoolBackendStartEntry {
  startAbortController: AbortController
}

function abortError(): Error {
  const error = new Error('The pool backend start was cancelled.')
  error.name = 'AbortError'

  return error
}

export function throwIfPoolBackendStartCancelled<Entry extends PoolBackendStartEntry>(
  pool: Map<string, Entry>,
  profile: string,
  entry: Entry
): void {
  if (entry.startAbortController.signal.aborted || pool.get(profile) !== entry) {
    throw abortError()
  }
}

export function cancelPoolBackendStart<Entry extends PoolBackendStartEntry>(
  pool: Map<string, Entry>,
  profile: string,
  entry: Entry
): boolean {
  if (pool.get(profile) !== entry) {
    return false
  }

  entry.startAbortController.abort()

  return true
}

export function deletePoolBackendEntryIfCurrent<Entry>(
  pool: Map<string, Entry>,
  profile: string,
  entry: Entry
): boolean {
  if (pool.get(profile) !== entry) {
    return false
  }

  return pool.delete(profile)
}

export async function waitForPoolBackendStartClearance<Entry extends PoolBackendStartEntry>(
  pool: Map<string, Entry>,
  profile: string,
  entry: Entry,
  deps: UpdateGateDeps,
  options: Omit<WaitForLocalBackendClearanceOptions, 'signal'>
): Promise<'clear' | 'finished'> {
  throwIfPoolBackendStartCancelled(pool, profile, entry)

  const outcome = await waitForLocalBackendClearance(deps, {
    ...options,
    signal: entry.startAbortController.signal
  })

  throwIfPoolBackendStartCancelled(pool, profile, entry)

  return outcome
}
