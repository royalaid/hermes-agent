/**
 * Windows update force-release orchestration.
 *
 * Selecting Update authorizes terminating only processes that the final native
 * boundary proves currently own an exact install resource. This module:
 *   1. discovers holders via the existing scanner + Windows Restart Manager;
 *   2. binds each target to PID + create-time (+ resource evidence);
 *   3. terminates leaf-first with a SuperF4-style TerminateProcess path;
 *   4. stays inside a five-second non-elevated budget;
 *   5. reports a fully authenticated permission boundary without exposing an
 *      Administrator action until a signed elevated helper exists.
 *
 * Behavioral reference (not copied): stefansundin/superf4 @ 6b677d4, superf4.c
 * OpenProcess/TerminateProcess sequence. Implement from Win32 docs only.
 */

export type ForceReleaseHolder = {
  pid: number
  /** Approximate Unix seconds used only to correlate independent discovery rows. */
  createdAt: number
  /** Exact unsigned 64-bit Windows FILETIME. Required for termination authority. */
  creationFileTime?: string
  name: string
  cmdline: string
  source: 'scanner' | 'restart-manager'
  resource?: string
  /** Individually authenticated resources. Never concatenate paths into one claim. */
  resources?: string[]
  parentPid?: number
  wrapperPid?: number
  role?: 'worker' | 'wrapper' | 'other'
}

export type ForceReleaseTerminateResult =
  | { kind: 'terminated' }
  | { kind: 'already-gone' }
  | { kind: 'create-time-mismatch' }
  | { kind: 'permission-required'; win32Error: 5 }
  /** Generic access failures are not sufficient proof for Administrator UI. */
  | { kind: 'access-denied'; win32Error: number; detail?: string }
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
  listScannerHolders: (budgetMs: number, signal?: AbortSignal, deadlineAt?: number) => Promise<ForceReleaseHolder[]>
  listRestartManagerHolders: (
    budgetMs: number,
    signal?: AbortSignal,
    deadlineAt?: number
  ) => Promise<ForceReleaseHolder[]>
  /**
   * Terminate one holder. Must honor `signal` by cancelling any child work and
   * guaranteeing no process-mutation side effect after abort settles.
   */
  terminateHolder: (
    holder: ForceReleaseHolder,
    budgetMs: number,
    signal?: AbortSignal,
    /** One absolute deadline shared with the native mutation boundary. */
    deadlineAt?: number
  ) => Promise<ForceReleaseTerminateResult>
  excludePids?: ReadonlySet<number>
  /** Non-elevated budget. Spec: five seconds or less. */
  deadlineMs?: number
  /** Click-wide absolute deadline. Later phases must never reset it. */
  deadlineAt?: number
  settleMs?: number
}

export type WindowsUpdateForceReleaseOutcome =
  | { kind: 'clear' }
  | { kind: 'needs-elevation'; holders: ForceReleaseHolder[]; message: string }
  | { kind: 'blocked'; holders: ForceReleaseHolder[]; message: string }
  | { kind: 'timeout'; holders: ForceReleaseHolder[]; message: string }

const DEFAULT_DEADLINE_MS = 5_000
const DEFAULT_SETTLE_MS = 150
/** Approximate discovery rows may differ slightly; they never authorize mutation. */
export const HOLDER_CREATE_TIME_MATCH_SECONDS = 1.5

function wait(delayMs: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, delayMs))
}

export function holdersMatchIdentity(
  left: Pick<ForceReleaseHolder, 'pid' | 'createdAt' | 'creationFileTime'>,
  right: Pick<ForceReleaseHolder, 'pid' | 'createdAt' | 'creationFileTime'>,
  toleranceSeconds = HOLDER_CREATE_TIME_MATCH_SECONDS
): boolean {
  if (left.pid !== right.pid) {
    return false
  }

  const leftExact = left.creationFileTime
  const rightExact = right.creationFileTime

  if (leftExact && rightExact) {
    return leftExact === rightExact
  }

  if (!Number.isFinite(left.createdAt) || !Number.isFinite(right.createdAt)) {
    return false
  }

  return Math.abs(left.createdAt - right.createdAt) <= toleranceSeconds
}

function holderResources(holder: ForceReleaseHolder): string[] {
  return Array.from(
    new Set(
      [...(holder.resources ?? []), ...(holder.resource ? [holder.resource] : [])].filter(
        value => typeof value === 'string' && value.length > 0
      )
    )
  )
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
      (entry.parentPid && byPid.get(entry.parentPid)) || (entry.wrapperPid && byPid.get(entry.wrapperPid)) || null

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
    .sort((left, right) => right.depth - left.depth || right.roleBias - left.roleBias || left.index - right.index)
    .map(item => item.entry)
}

/**
 * When holders carry parent/wrapper edges among the set, annotate roles so
 * orderHoldersLeafFirst can drain leaves before roots in production mappings.
 */
export function attachHolderTreeRelationships(holders: readonly ForceReleaseHolder[]): ForceReleaseHolder[] {
  if (holders.length === 0) {
    return []
  }

  const byPid = new Map(holders.map(entry => [entry.pid, entry] as const))
  const childCount = new Map<number, number>()

  const exactParent = (entry: ForceReleaseHolder, candidatePid?: number): number | undefined => {
    if (!candidatePid || !entry.creationFileTime) {
      return undefined
    }

    const candidate = byPid.get(candidatePid)

    if (!candidate?.creationFileTime) {
      return undefined
    }

    try {
      // A PROCESSENTRY32 parent PID is useful only when the RM-authenticated
      // parent generation existed before the exact child generation. This
      // rejects the stale ParentProcessId + reused-PID case.
      if (BigInt(candidate.creationFileTime) > BigInt(entry.creationFileTime)) {
        return undefined
      }
    } catch {
      return undefined
    }

    return candidatePid
  }

  const withEdges: ForceReleaseHolder[] = holders.map(entry => {
    const parentPid = exactParent(entry, entry.parentPid) ?? exactParent(entry, entry.wrapperPid)

    if (parentPid != null) {
      childCount.set(parentPid, (childCount.get(parentPid) ?? 0) + 1)
    }

    const { parentPid: _untrustedParent, wrapperPid: _untrustedWrapper, ...base } = entry

    return {
      ...base,
      ...(parentPid != null ? { parentPid } : {})
    }
  })

  return withEdges.map(entry => {
    const isParent = (childCount.get(entry.pid) ?? 0) > 0
    const hasParent = entry.parentPid != null && byPid.has(entry.parentPid)

    if (entry.role === 'worker' || entry.role === 'wrapper') {
      return entry
    }

    if (hasParent) {
      return { ...entry, role: 'worker' as const }
    }

    if (isParent) {
      return { ...entry, role: 'wrapper' as const }
    }

    return entry
  })
}

export function mergeInstallHolders(
  holders: readonly ForceReleaseHolder[],
  excludePids: ReadonlySet<number> = new Set()
): ForceReleaseHolder[] {
  const merged: ForceReleaseHolder[] = []

  // Restart Manager rows are the only termination authority. Scanner rows may
  // enrich ordering metadata for an already-authoritative RM generation, but
  // they must never introduce a PID or resource claim of their own.
  for (const entry of holders.filter(candidate => candidate.source === 'restart-manager')) {
    if (!Number.isInteger(entry.pid) || entry.pid <= 0) {
      continue
    }

    if (!Number.isFinite(entry.createdAt) || entry.createdAt <= 0) {
      continue
    }

    if (excludePids.has(entry.pid)) {
      continue
    }

    const existingIndex = merged.findIndex(candidate => holdersMatchIdentity(candidate, entry))

    if (existingIndex < 0) {
      merged.push({ ...entry })

      continue
    }

    const existing = merged[existingIndex]!
    const resources = Array.from(new Set([...holderResources(existing), ...holderResources(entry)]))
    merged[existingIndex] = {
      ...existing,
      ...entry,
      creationFileTime: existing.creationFileTime ?? entry.creationFileTime,
      source: 'restart-manager',
      resource: resources[0] ?? existing.resource,
      resources,
      parentPid: existing.parentPid ?? entry.parentPid,
      wrapperPid: existing.wrapperPid ?? entry.wrapperPid,
      role: existing.role === 'other' || existing.role == null ? (entry.role ?? existing.role) : existing.role
    }
  }

  return attachHolderTreeRelationships(merged)
}

function elevationMessage(holders: readonly ForceReleaseHolder[]): string {
  return (
    'Update verified that a current Hermes install holder requires Administrator permission. ' +
    `Verified holder count: ${Math.min(holders.length, 64)}. Close it and retry the ordinary update.`
  )
}

function blockedMessage(holders: readonly ForceReleaseHolder[], detail: string): string {
  return (
    `Update aborted: ${detail}. ` +
    `Verified holder count: ${Math.min(holders.length, 64)}. ` +
    'The virtual environment was not modified.'
  )
}

/**
 * Race work against the remaining wall-clock budget.
 *
 * When `work` is a factory, the orchestrator passes an AbortSignal. Abort fires
 * early enough that kill-and-reap normally fits inside `budgetMs`. After
 * abort, a factory is always awaited to terminal settlement: returning while
 * its child can still write would violate the updater's post-return boundary.
 * Dependencies therefore must implement abort by closing/reaping their child.
 * Plain promises remain supported for discovery calls (no drain wait).
 */
export function raceWithBudget<T>(
  work: Promise<T> | ((signal: AbortSignal) => Promise<T>),
  budgetMs: number,
  onTimeout: () => T,
  options?: { drainMs?: number }
): Promise<T> {
  const budget = Math.trunc(budgetMs)

  if (budget <= 0) {
    return Promise.resolve(onTimeout())
  }

  const isFactory = typeof work === 'function'
  const controller = new AbortController()
  const pending = isFactory ? work(controller.signal) : work

  // Reserve drain room inside the budget so total elapsed stays <= budgetMs.
  const requestedDrain = Math.max(
    0,
    Math.trunc(options?.drainMs ?? (isFactory ? Math.min(750, Math.max(100, Math.floor(budget * 0.2))) : 0))
  )

  const drainMs = isFactory ? Math.min(requestedDrain, Math.max(0, budget - 1)) : 0
  const workBudget = Math.max(0, budget - drainMs)
  let settled = false
  let timedOut = false

  return new Promise<T>((resolve, reject) => {
    const finishTimeout = () => {
      if (settled) {
        return
      }

      settled = true
      resolve(onTimeout())
    }

    const timer = setTimeout(() => {
      if (settled) {
        return
      }

      timedOut = true
      controller.abort()

      if (!isFactory || drainMs <= 0) {
        finishTimeout()
      }
    }, workBudget)

    void pending.then(
      value => {
        if (settled) {
          return
        }

        if (timedOut) {
          finishTimeout()

          return
        }

        settled = true
        clearTimeout(timer)
        resolve(value)
      },
      error => {
        if (settled) {
          return
        }

        if (timedOut) {
          finishTimeout()

          return
        }

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
  const configuredDeadline = started + deadlineMs

  const deadline =
    typeof deps.deadlineAt === 'number' && Number.isFinite(deps.deadlineAt)
      ? Math.min(configuredDeadline, Math.trunc(deps.deadlineAt))
      : configuredDeadline

  const remaining = () => Math.max(0, deadline - now())

  if (!(await deps.isResourceLocked())) {
    return { kind: 'clear' }
  }

  let permissionRequired: ForceReleaseHolder[] = []
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

    if (scanBudget <= 0) {
      break
    }

    const scanned = await raceWithBudget(
      signal => deps.listScannerHolders(scanBudget, signal, deadline),
      scanBudget,
      () => [] as ForceReleaseHolder[]
    )

    if (remaining() <= 0) {
      break
    }

    const rmBudget = remaining()

    const fromRm = await raceWithBudget(
      signal => deps.listRestartManagerHolders(rmBudget, signal, deadline),
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

    permissionRequired = []
    accessDenied = []
    protectedHolders = []
    mismatchHolders = []

    for (const target of holders) {
      const budget = remaining()

      if (budget <= 0) {
        break
      }

      // Mutating termination owns the remaining budget. The controller is
      // cancelled at the same absolute deadline that is passed to the native
      // boundary; the boundary must return before this deadline or fail closed.
      const terminateController = new AbortController()

      const terminateTimer = setTimeout(() => terminateController.abort(), Math.max(1, Math.trunc(budget)))

      let result: ForceReleaseTerminateResult

      try {
        result = await deps.terminateHolder(target, budget, terminateController.signal, deadline)
      } finally {
        clearTimeout(terminateTimer)
      }

      switch (result.kind) {
        case 'terminated':

        case 'already-gone':
          break

        case 'permission-required':
          permissionRequired.push(target)

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

    // A dedicated native permission result stops the ordinary path promptly.
    if (permissionRequired.length > 0) {
      return {
        kind: 'needs-elevation',
        holders: permissionRequired,
        message: elevationMessage(permissionRequired)
      }
    }

    if (protectedHolders.length > 0 || accessDenied.length > 0) {
      const blocked = [...protectedHolders, ...accessDenied]

      return {
        kind: 'blocked',
        holders: blocked,
        message: blockedMessage(blocked, 'the exact holder identity or permission boundary could not be authenticated')
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

  if (permissionRequired.length > 0) {
    return {
      kind: 'needs-elevation',
      holders: permissionRequired,
      message: elevationMessage(permissionRequired)
    }
  }

  if (protectedHolders.length > 0 || accessDenied.length > 0) {
    const blocked = [...protectedHolders, ...accessDenied]

    return {
      kind: 'blocked',
      holders: blocked,
      message: blockedMessage(blocked, 'the exact holder identity or permission boundary could not be authenticated')
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
    message: blockedMessage(lastHolders, 'install file locks could not be cleared within five seconds')
  }
}
