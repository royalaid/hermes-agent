/**
 * Windows update force-release orchestration.
 *
 * Selecting Update authorizes terminating every process that currently holds or
 * executes files inside the target install. This module:
 *   1. discovers holders via the existing scanner + Windows Restart Manager;
 *   2. binds each target to PID + create-time (+ resource evidence);
 *   3. terminates leaf-first with a SuperF4-style TerminateProcess path;
 *   4. stays inside a five-second non-elevated budget;
 *   5. escalates to an elevated helper only on access-denied survivors.
 *
 * Behavioral reference (not copied): stefansundin/superf4 @ 6b677d4, superf4.c
 * OpenProcess/TerminateProcess sequence. Implement from Win32 docs only.
 */

export type ForceReleaseHolder = {
  pid: number
  createdAt: number
  name: string
  cmdline: string
  source: 'scanner' | 'restart-manager'
  resource?: string
  parentPid?: number
  wrapperPid?: number
  role?: 'worker' | 'wrapper' | 'other'
}

export type ForceReleaseTerminateResult =
  | { kind: 'terminated' }
  | { kind: 'already-gone' }
  | { kind: 'create-time-mismatch' }
  | { kind: 'access-denied'; win32Error: number }
  | { kind: 'protected'; win32Error: number }
  | { kind: 'failed'; detail: string; win32Error?: number }

export type WindowsUpdateForceReleaseDeps = {
  now?: () => number
  wait?: (delayMs: number) => Promise<void>
  isResourceLocked: () => Promise<boolean>
  /**
   * Discover scanner holders. `budgetMs` is the hard remaining wall-clock
   * budget; implementations must honor it (or the orchestrator races them).
   */
  listScannerHolders: (budgetMs: number) => Promise<ForceReleaseHolder[]>
  listRestartManagerHolders: (budgetMs: number) => Promise<ForceReleaseHolder[]>
  terminateHolder: (holder: ForceReleaseHolder, budgetMs: number) => Promise<ForceReleaseTerminateResult>
  excludePids?: ReadonlySet<number>
  /** Non-elevated budget. Spec: five seconds or less. */
  deadlineMs?: number
  settleMs?: number
}

export type WindowsUpdateForceReleaseOutcome =
  | { kind: 'clear' }
  | { kind: 'needs-elevation'; holders: ForceReleaseHolder[]; message: string }
  | { kind: 'blocked'; holders: ForceReleaseHolder[]; message: string }
  | { kind: 'timeout'; holders: ForceReleaseHolder[]; message: string }

const DEFAULT_DEADLINE_MS = 5_000
const DEFAULT_SETTLE_MS = 150

function wait(delayMs: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, delayMs))
}

function holderKey(holder: ForceReleaseHolder): string {
  return `${holder.pid}:${holder.createdAt}`
}

export function mergeInstallHolders(
  holders: readonly ForceReleaseHolder[],
  excludePids: ReadonlySet<number> = new Set()
): ForceReleaseHolder[] {
  const byKey = new Map<string, ForceReleaseHolder>()

  for (const entry of holders) {
    if (!Number.isInteger(entry.pid) || entry.pid <= 0) {
      continue
    }

    if (!Number.isFinite(entry.createdAt) || entry.createdAt <= 0) {
      continue
    }

    if (excludePids.has(entry.pid)) {
      continue
    }

    const key = holderKey(entry)
    const existing = byKey.get(key)

    if (!existing) {
      byKey.set(key, { ...entry })
      continue
    }

    const resources = [existing.resource, entry.resource].filter(Boolean) as string[]
    const preferredSource = existing.source === 'scanner' || entry.source === 'scanner' ? 'scanner' : entry.source

    byKey.set(key, {
      ...existing,
      ...entry,
      source: preferredSource,
      resource: resources.length ? Array.from(new Set(resources)).join('; ') : existing.resource,
      parentPid: existing.parentPid ?? entry.parentPid,
      wrapperPid: existing.wrapperPid ?? entry.wrapperPid,
      role: existing.role ?? entry.role
    })
  }

  return [...byKey.values()]
}

/**
 * Leaf-first order: deepest descendants first, then workers before wrappers.
 * Stable for unrelated peers (original relative order among ties).
 */
export function orderHoldersLeafFirst(holders: readonly ForceReleaseHolder[]): ForceReleaseHolder[] {
  const byPid = new Map(holders.map(entry => [entry.pid, entry] as const))
  const depthMemo = new Map<number, number>()

  const depthOf = (entry: ForceReleaseHolder, stack: Set<number> = new Set()): number => {
    const cached = depthMemo.get(entry.pid)
    if (cached !== undefined) {
      return cached
    }
    if (stack.has(entry.pid)) {
      return 0
    }
    stack.add(entry.pid)

    const parentRef =
      (entry.parentPid && byPid.get(entry.parentPid)) ||
      (entry.wrapperPid && byPid.get(entry.wrapperPid)) ||
      null

    let depth = 0
    if (parentRef) {
      depth = depthOf(parentRef, stack) + 1
    } else if (entry.role === 'worker') {
      depth = 1
    } else if (entry.role === 'wrapper') {
      depth = 0
    }

    depthMemo.set(entry.pid, depth)
    stack.delete(entry.pid)
    return depth
  }

  return holders
    .map((entry, index) => ({
      entry,
      index,
      depth: depthOf(entry),
      roleBias: entry.role === 'worker' ? 1 : entry.role === 'wrapper' ? -1 : 0
    }))
    .sort(
      (left, right) =>
        right.depth - left.depth || right.roleBias - left.roleBias || left.index - right.index
    )
    .map(item => item.entry)
}

function formatHolderLine(holder: ForceReleaseHolder): string {
  const resource = holder.resource ? ` resource=${holder.resource}` : ''
  return `PID ${holder.pid} ${holder.name}${resource}`
}

function elevationMessage(holders: readonly ForceReleaseHolder[]): string {
  const sample = holders
    .slice(0, 5)
    .map(formatHolderLine)
    .join('; ')
  return (
    'Update needs Administrator permission to stop processes still locking this Hermes install. ' +
    `Survivors: ${sample || 'unknown'}. Choose Force update (Administrator) to continue, or close those processes and retry.`
  )
}

function blockedMessage(holders: readonly ForceReleaseHolder[], detail: string): string {
  const sample = holders
    .slice(0, 5)
    .map(formatHolderLine)
    .join('; ')
  return (
    `Update aborted: ${detail}. ` +
    `Still holding the install: ${sample || 'unknown'}. ` +
    'The virtual environment was not modified.'
  )
}

/**
 * Race a dependency against the remaining wall-clock budget. Hung/slow
 * scanners, RM queries, or terminations must not exceed the five-second path.
 *
 * Uses a real timer (not deps.wait) so fake-clock unit tests that complete
 * work synchronously are not charged the full budget by the hang guard.
 */
export function raceWithBudget<T>(
  work: Promise<T>,
  budgetMs: number,
  onTimeout: () => T
): Promise<T> {
  const budget = Math.trunc(budgetMs)
  if (budget <= 0) {
    return Promise.resolve(onTimeout())
  }

  let settled = false
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      resolve(onTimeout())
    }, budget)

    void work.then(
      value => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve(value)
      },
      error => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        reject(error)
      }
    )
  })
}

export async function runWindowsUpdateForceRelease(
  deps: WindowsUpdateForceReleaseDeps
): Promise<WindowsUpdateForceReleaseOutcome> {
  const now = deps.now ?? Date.now
  const sleep = deps.wait ?? wait
  const deadlineMs = Math.max(0, deps.deadlineMs ?? DEFAULT_DEADLINE_MS)
  const settleMs = Math.max(0, deps.settleMs ?? DEFAULT_SETTLE_MS)
  const excludePids = deps.excludePids ?? new Set<number>()
  const started = now()
  const deadline = started + deadlineMs
  const remaining = () => Math.max(0, deadline - now())

  if (!(await deps.isResourceLocked())) {
    return { kind: 'clear' }
  }

  let accessDenied: ForceReleaseHolder[] = []
  let protectedHolders: ForceReleaseHolder[] = []
  let mismatchHolders: ForceReleaseHolder[] = []
  let lastHolders: ForceReleaseHolder[] = []

  while (remaining() > 0) {
    if (!(await deps.isResourceLocked())) {
      return { kind: 'clear' }
    }

    const passStarted = now()
    const scanBudget = remaining()
    if (scanBudget <= 0) break

    const scanned = await raceWithBudget(
      deps.listScannerHolders(scanBudget),
      scanBudget,
      () => [] as ForceReleaseHolder[]
    )
    if (remaining() <= 0) break

    const rmBudget = remaining()
    const fromRm = await raceWithBudget(
      deps.listRestartManagerHolders(rmBudget),
      rmBudget,
      () => [] as ForceReleaseHolder[]
    )
    if (remaining() <= 0) {
      lastHolders = orderHoldersLeafFirst(mergeInstallHolders([...scanned, ...fromRm], excludePids))
      break
    }

    const holders = orderHoldersLeafFirst(mergeInstallHolders([...scanned, ...fromRm], excludePids))
    lastHolders = holders

    if (holders.length === 0) {
      // Locked with no enumerable holders: fail closed rather than mutate.
      const pause = Math.min(settleMs || 50, remaining())
      if (pause > 0) {
        await sleep(pause)
      }
      if (!(await deps.isResourceLocked())) {
        return { kind: 'clear' }
      }
      break
    }

    accessDenied = []
    protectedHolders = []
    mismatchHolders = []

    for (const target of holders) {
      const budget = remaining()
      if (budget <= 0) {
        break
      }

      const result = await raceWithBudget(
        deps.terminateHolder(target, budget),
        budget,
        () => ({ kind: 'failed', detail: 'deadline-exhausted' }) as ForceReleaseTerminateResult
      )

      switch (result.kind) {
        case 'terminated':
        case 'already-gone':
          break
        case 'access-denied':
          accessDenied.push(target)
          break
        case 'protected':
          protectedHolders.push(target)
          break
        case 'create-time-mismatch':
          mismatchHolders.push(target)
          break
        case 'failed':
          // Keep trying others; final lock proof decides.
          break
      }
    }

    const settle = Math.min(Math.max(settleMs, 1), remaining())
    if (settle > 0) {
      await sleep(settle)
    }

    if (!(await deps.isResourceLocked())) {
      return { kind: 'clear' }
    }

    // Access-denied survivors need elevation — stop the quick path promptly.
    if (accessDenied.length > 0) {
      return {
        kind: 'needs-elevation',
        holders: accessDenied,
        message: elevationMessage(accessDenied)
      }
    }

    if (protectedHolders.length > 0) {
      return {
        kind: 'blocked',
        holders: protectedHolders,
        message: blockedMessage(
          protectedHolders,
          'a protected or unkillable process still holds install files (Win32 access denied / protected process)'
        )
      }
    }

    if (mismatchHolders.length > 0 && holders.every(h => mismatchHolders.some(m => m.pid === h.pid))) {
      return {
        kind: 'blocked',
        holders: mismatchHolders,
        message: blockedMessage(
          mismatchHolders,
          'PID create-time no longer matches (PID reuse); refusing to terminate a different process'
        )
      }
    }

    // Guarantee forward progress even when wait() is a no-op in tests.
    if (now() <= passStarted && deps.now) {
      // Fake clocks only advance in wait(); ensure we cannot spin forever.
      break
    }
  }

  if (!(await deps.isResourceLocked())) {
    return { kind: 'clear' }
  }

  if (accessDenied.length > 0) {
    return {
      kind: 'needs-elevation',
      holders: accessDenied,
      message: elevationMessage(accessDenied)
    }
  }

  if (protectedHolders.length > 0) {
    return {
      kind: 'blocked',
      holders: protectedHolders,
      message: blockedMessage(
        protectedHolders,
        'a protected or unkillable process still holds install files (Win32 access denied / protected process)'
      )
    }
  }

  if (mismatchHolders.length > 0) {
    return {
      kind: 'blocked',
      holders: mismatchHolders,
      message: blockedMessage(
        mismatchHolders,
        'PID create-time no longer matches (PID reuse); refusing to terminate a different process'
      )
    }
  }

  return {
    kind: 'timeout',
    holders: lastHolders,
    message: blockedMessage(
      lastHolders,
      'install file locks could not be cleared within five seconds'
    )
  }
}
