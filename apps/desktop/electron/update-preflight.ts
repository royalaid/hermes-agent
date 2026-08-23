import type { McpBridgeQuiesceLease } from './mcp-bridge-quiesce'
import {
  type DesktopPluginServiceProcess,
  formatBlockerMessage,
  formatProbeFailedMessage,
  isExactVenvHolder,
  type ScanOutcome,
  type VenvBlockerProcess,
  type VenvBlockerScanResult
} from './venv-blocker-scan'
import type { ForceReleaseHolder, WindowsUpdateForceReleaseOutcome } from './windows-update-force-release'

export type UpdatePreflightPurpose = 'normal-update' | 'bootstrap-recovery'

export interface UpdateBlockerDeadline {
  readonly deadlineAt: number
}

export const NORMAL_UPDATE_BLOCKER_TIMEOUT_MS = 5_000

export function createNormalUpdateBlockerDeadline(now: () => number = Date.now): UpdateBlockerDeadline {
  return Object.freeze({ deadlineAt: now() + NORMAL_UPDATE_BLOCKER_TIMEOUT_MS })
}

export interface UpdatePreflightDeps {
  acquireMcpBridgeLease: () => McpBridgeQuiesceLease | null
  clearMcpBridgeLease: (lease: McpBridgeQuiesceLease) => void
  now?: () => number
  releaseTrackedBackendTrees: (deadline?: UpdateBlockerDeadline) => Promise<{ unlocked: boolean }>
  /**
   * Non-elevated ≤5s force-release of exact install holders when tracked
   * backends alone did not unlock the install. Optional only for unit tests
   * that intentionally exercise the legacy unlock-failed path.
   */
  forceReleaseInstallHolders?: (deadline?: UpdateBlockerDeadline) => Promise<WindowsUpdateForceReleaseOutcome>
  scan: (deadline?: UpdateBlockerDeadline) => Promise<ScanOutcome>
  terminateDesktopPluginService: (service: DesktopPluginServiceProcess) => Promise<boolean>
  terminateVenvHolder: (holder: VenvBlockerProcess) => Promise<boolean>
  wait?: (delayMs: number) => Promise<void>
}

export interface UpdatePreflightTiming {
  cooperativeExitMs?: number
  genericHolderPollMs?: number
  genericHolderTimeoutMs?: number
  respawnIntervalMs?: number
  terminationSettleMs?: number
}

export type UpdatePreflightOutcome =
  | { kind: 'clear'; lease: McpBridgeQuiesceLease }
  | {
      kind: 'blocked'
      message: string
      reason: 'unlock-failed' | 'holders' | 'lease-unavailable' | 'quiesce-incomplete' | 'needs-elevation'
      result?: VenvBlockerScanResult
      elevationHolders?: ForceReleaseHolder[]
    }
  | { kind: 'probe-failure'; error: string; message: string }

export interface UpdateMutationPermit {
  readonly preflight: Extract<UpdatePreflightOutcome, { kind: 'clear' }>
}

const successfulPreflightPermits = new WeakMap<object, UpdateMutationPermit>()
const updateMutationPermits = new WeakSet<object>()

export function authorizeUpdateMutation(preflight: UpdatePreflightOutcome): UpdateMutationPermit | null {
  const permit = successfulPreflightPermits.get(preflight) ?? null

  if (!permit) {
    return null
  }

  successfulPreflightPermits.delete(preflight)

  return permit
}

export function runAuthorizedUpdateMutation<T>(permit: UpdateMutationPermit, operation: () => T): T {
  if (!updateMutationPermits.has(permit)) {
    throw new Error('update mutation requires a clear-preflight permit')
  }

  return operation()
}

const DEFAULT_COOPERATIVE_EXIT_MS = 1_500
const DEFAULT_GENERIC_HOLDER_POLL_MS = 1_000
const DEFAULT_GENERIC_HOLDER_TIMEOUT_MS = 30_000
const DEFAULT_RESPAWN_INTERVAL_MS = 1_500
const DEFAULT_TERMINATION_SETTLE_MS = 750
const MAX_FALLBACK_DRAIN_GROUPS = 32
const MAX_FALLBACK_DRAIN_RECORDS = 64

function wait(delayMs: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, delayMs))
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function exactDrainableOnly(result: VenvBlockerScanResult): boolean {
  return (
    result.processes.every(isExactVenvHolder) &&
    result.mcpBridges.every(bridge => isExactVenvHolder(bridge)) &&
    result.desktopPluginServices.every(service => isExactVenvHolder(service)) &&
    result.processes.length + result.mcpBridges.length + result.desktopPluginServices.length > 0
  )
}

function logicalDrainGroupCount(processes: ReadonlyArray<{ pid: number; wrapperPid?: number }>): number {
  return new Set(processes.map(process => process.wrapperPid ?? process.pid)).size
}

function genericHoldersOnly(result: VenvBlockerScanResult): boolean {
  return result.processes.length > 0 && result.mcpBridges.length === 0
}

function quiesceIncompleteMessage(): string {
  return (
    'Update aborted: processes from this Hermes installation did not remain stopped. ' +
    'Close or stop the restarting service, then retry.'
  )
}

function isAuthenticatedPermissionHolder(holder: ForceReleaseHolder): boolean {
  const resources = holder.resources ?? []

  return (
    Number.isInteger(holder.pid) &&
    holder.pid > 0 &&
    Number.isFinite(holder.createdAt) &&
    holder.createdAt > 0 &&
    typeof holder.creationFileTime === 'string' &&
    /^\d+$/.test(holder.creationFileTime) &&
    BigInt(holder.creationFileTime) > 0n &&
    (holder.source === 'scanner' || holder.source === 'restart-manager') &&
    typeof holder.resource === 'string' &&
    holder.resource.trim().length > 0 &&
    resources.length > 0 &&
    resources.every(resource => typeof resource === 'string' && resource.trim().length > 0) &&
    resources.includes(holder.resource)
  )
}

export async function runWindowsUpdatePreflight(
  purpose: UpdatePreflightPurpose,
  deps: UpdatePreflightDeps,
  timing: UpdatePreflightTiming = {},
  blockerDeadline?: UpdateBlockerDeadline
): Promise<UpdatePreflightOutcome> {
  const sleep = deps.wait ?? wait
  const now = deps.now ?? Date.now
  const cooperativeExitMs = timing.cooperativeExitMs ?? DEFAULT_COOPERATIVE_EXIT_MS
  const genericHolderPollMs = Math.max(1, timing.genericHolderPollMs ?? DEFAULT_GENERIC_HOLDER_POLL_MS)
  const genericHolderTimeoutMs = Math.max(0, timing.genericHolderTimeoutMs ?? DEFAULT_GENERIC_HOLDER_TIMEOUT_MS)
  const respawnIntervalMs = timing.respawnIntervalMs ?? DEFAULT_RESPAWN_INTERVAL_MS
  const terminationSettleMs = timing.terminationSettleMs ?? DEFAULT_TERMINATION_SETTLE_MS

  const remainingBlockerMs = () =>
    blockerDeadline ? Math.max(0, blockerDeadline.deadlineAt - now()) : Number.POSITIVE_INFINITY

  let lock: { unlocked: boolean }

  try {
    lock = await deps.releaseTrackedBackendTrees(blockerDeadline)
  } catch {
    lock = { unlocked: false }
  }

  // Selecting Update authorizes a bounded force-release of exact install
  // holders. Do not dead-end on the tracked-backend unlock wait alone — that
  // is how a stale `hermes tools | head` tree blocked the scanner forever.
  if (lock?.unlocked !== true) {
    if (remainingBlockerMs() <= 0) {
      return {
        kind: 'blocked',
        reason: 'unlock-failed',
        message: 'Update aborted: Hermes could not prepare the install within the shared five-second deadline.'
      }
    }

    if (typeof deps.forceReleaseInstallHolders === 'function') {
      let forceOutcome: WindowsUpdateForceReleaseOutcome

      try {
        forceOutcome = await deps.forceReleaseInstallHolders(blockerDeadline)
      } catch (error) {
        return {
          kind: 'probe-failure',
          error: `force-release threw: ${errorText(error)}`,
          message: formatProbeFailedMessage()
        }
      }

      if (forceOutcome.kind === 'clear') {
        lock = { unlocked: true }
      } else if (
        forceOutcome.kind === 'needs-elevation' &&
        forceOutcome.holders.length > 0 &&
        forceOutcome.holders.every(isAuthenticatedPermissionHolder)
      ) {
        return {
          kind: 'blocked',
          reason: 'needs-elevation',
          message: forceOutcome.message,
          elevationHolders: forceOutcome.holders
        }
      } else {
        return {
          kind: 'blocked',
          reason: 'unlock-failed',
          message: forceOutcome.message
        }
      }
    } else {
      return {
        kind: 'blocked',
        reason: 'unlock-failed',
        message:
          'Update aborted: Hermes could not release its tracked local processes. ' +
          'Close other Hermes windows and terminals, then retry.'
      }
    }
  }

  if (purpose === 'normal-update') {
    let lease: McpBridgeQuiesceLease | null = null

    try {
      lease = deps.acquireMcpBridgeLease()
    } catch {
      lease = null
    }

    if (!lease) {
      return {
        kind: 'blocked',
        reason: 'lease-unavailable',
        message: 'Update aborted: Hermes could not acquire the MCP bridge pause safely.'
      }
    }

    const outcome: Extract<UpdatePreflightOutcome, { kind: 'clear' }> = Object.freeze({ kind: 'clear', lease })
    const permit: UpdateMutationPermit = Object.freeze({ preflight: outcome })
    successfulPreflightPermits.set(outcome, permit)
    updateMutationPermits.add(permit)

    return outcome
  }

  // Observe before activating the prevention lease. Existing bridge watchers
  // exit when they see that lease, while this first scan defines the exact
  // current holder set authorized by the user's Update action.
  const scanFailClosed = async (): Promise<ScanOutcome> => {
    if (remainingBlockerMs() <= 0) {
      return { kind: 'probe-failure', error: 'shared update blocker deadline elapsed' }
    }

    try {
      return await deps.scan(blockerDeadline)
    } catch (error) {
      return { kind: 'probe-failure', error: `scanner threw: ${errorText(error)}` }
    }
  }

  const pollGenericHoldersUntil = async (current: ScanOutcome, deadline: number): Promise<ScanOutcome> => {
    let outcome = current

    while (outcome.kind === 'blocked' && genericHoldersOnly(outcome.result)) {
      const remainingMs = deadline - now()

      if (remainingMs <= 0) {
        break
      }

      await sleep(Math.min(genericHolderPollMs, remainingMs, remainingBlockerMs()))
      outcome = await scanFailClosed()
    }

    return outcome
  }

  const observed = await scanFailClosed()

  if (observed.kind === 'probe-failure') {
    return { kind: 'probe-failure', error: observed.error, message: formatProbeFailedMessage() }
  }

  if (observed.kind === 'blocked' && !exactDrainableOnly(observed.result)) {
    return {
      kind: 'blocked',
      reason: 'holders',
      result: observed.result,
      message: formatBlockerMessage(observed.result)
    }
  }

  let lease: McpBridgeQuiesceLease | null = null

  try {
    lease = deps.acquireMcpBridgeLease()
  } catch {
    lease = null
  }

  if (!lease) {
    return {
      kind: 'blocked',
      reason: 'lease-unavailable',
      ...(observed.kind === 'blocked' ? { result: observed.result } : {}),
      message:
        'Update aborted: Hermes could not acquire the MCP bridge pause safely. Retry after any other update finishes.'
    }
  }

  let returnLease = false

  try {
    await sleep(Math.min(cooperativeExitMs, remainingBlockerMs()))
    let firstClear = await scanFailClosed()
    let genericHolderDeadline: number | null = null

    if (firstClear.kind === 'probe-failure') {
      return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
    }

    // Ordinary updates must cross the same native authentication boundary for
    // a holder that appears after the first observation. The legacy Python
    // per-PID terminators remain only as a bootstrap-compatibility fallback;
    // production ordinary updates always wire this native force-release seam.
    if (
      purpose === 'normal-update' &&
      firstClear.kind === 'blocked' &&
      typeof deps.forceReleaseInstallHolders === 'function'
    ) {
      if (remainingBlockerMs() <= 0) {
        return {
          kind: 'blocked',
          reason: 'unlock-failed',
          message: 'Update aborted: exact holder handling did not finish within five seconds.'
        }
      }

      let forceOutcome: WindowsUpdateForceReleaseOutcome

      try {
        forceOutcome = await deps.forceReleaseInstallHolders(blockerDeadline)
      } catch (error) {
        return {
          kind: 'probe-failure',
          error: `force-release threw: ${errorText(error)}`,
          message: formatProbeFailedMessage()
        }
      }

      if (forceOutcome.kind === 'clear') {
        firstClear = await scanFailClosed()

        if (firstClear.kind === 'probe-failure') {
          return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
        }
      } else if (
        forceOutcome.kind === 'needs-elevation' &&
        forceOutcome.holders.length > 0 &&
        forceOutcome.holders.every(isAuthenticatedPermissionHolder)
      ) {
        return {
          kind: 'blocked',
          reason: 'needs-elevation',
          message: forceOutcome.message,
          elevationHolders: forceOutcome.holders
        }
      } else {
        return {
          kind: 'blocked',
          reason: 'unlock-failed',
          message: forceOutcome.message
        }
      }
    }

    if (
      firstClear.kind === 'blocked' &&
      genericHoldersOnly(firstClear.result) &&
      !exactDrainableOnly(firstClear.result)
    ) {
      genericHolderDeadline = Math.min(now() + genericHolderTimeoutMs, blockerDeadline?.deadlineAt ?? Infinity)
      firstClear = await pollGenericHoldersUntil(firstClear, genericHolderDeadline)

      if (firstClear.kind === 'probe-failure') {
        return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
      }

      if (firstClear.kind === 'blocked' && genericHoldersOnly(firstClear.result)) {
        return {
          kind: 'blocked',
          reason: 'quiesce-incomplete',
          result: firstClear.result,
          message: formatBlockerMessage(firstClear.result)
        }
      }
    }

    if (firstClear.kind === 'blocked') {
      const forceableMcpAndDesktopServices =
        firstClear.result.mcpBridges.every(bridge => isExactVenvHolder(bridge)) &&
        firstClear.result.desktopPluginServices.every(service => isExactVenvHolder(service))

      const naturallyExitingGenericHolders =
        firstClear.result.processes.length > 0 &&
        firstClear.result.processes.every(holder => !isExactVenvHolder(holder))

      const exactCurrentHolders =
        exactDrainableOnly(firstClear.result) || (forceableMcpAndDesktopServices && naturallyExitingGenericHolders)

      const logicalDrainGroups =
        logicalDrainGroupCount(firstClear.result.mcpBridges) +
        logicalDrainGroupCount(firstClear.result.desktopPluginServices)

      const drainRecordCount =
        firstClear.result.processes.length +
        firstClear.result.mcpBridges.length +
        firstClear.result.desktopPluginServices.length

      if (
        !exactCurrentHolders ||
        logicalDrainGroups > MAX_FALLBACK_DRAIN_GROUPS ||
        drainRecordCount > MAX_FALLBACK_DRAIN_RECORDS
      ) {
        return {
          kind: 'blocked',
          reason: 'quiesce-incomplete',
          result: firstClear.result,
          message: exactDrainableOnly(firstClear.result)
            ? quiesceIncompleteMessage()
            : formatBlockerMessage(firstClear.result)
        }
      }

      // One bounded force-stop pass. The user already chose Update. Each call
      // re-scans the target installation and verifies PID/create-time before
      // stopping that single holder; there is deliberately no process-tree kill.
      const terminationOrder = [...firstClear.result.mcpBridges].sort(
        (left, right) => Number(right.role === 'mcp_bridge_worker') - Number(left.role === 'mcp_bridge_worker')
      )

      // A managed-runtime worker can derive its exact identity from its live
      // wrapper ancestry. Drain workers before wrappers and await each one so
      // the wrapper cannot disappear before the scanner revalidates a worker.
      for (const bridge of terminationOrder) {
        try {
          await deps.terminateVenvHolder(bridge)
        } catch {
          // The mandatory rescan below is the authority. One failed request
          // must not skip revalidation of the remaining current entries.
          void 0
        }
      }

      const desktopPluginTerminationOrder = [...firstClear.result.desktopPluginServices].sort(
        (left, right) => Number(right.role === 'desktop_plugin_worker') - Number(left.role === 'desktop_plugin_worker')
      )

      // A plugin's managed-runtime worker must stop before its venv wrapper.
      // The wrapper termination then stops its proven service host, preventing
      // that host from recreating either child during the update handoff.
      for (const service of desktopPluginTerminationOrder) {
        try {
          await deps.terminateDesktopPluginService(service)
        } catch {
          void 0
        }
      }

      for (const holder of firstClear.result.processes) {
        if (!isExactVenvHolder(holder)) {
          continue
        }

        try {
          await deps.terminateVenvHolder(holder)
        } catch {
          void 0
        }
      }

      // A scheduled gateway-status or presence probe can start after the
      // first scan and briefly share this venv with an exact MCP bridge. A
      // holder without an identity timestamp cannot be force-stopped, so give
      // it one bounded chance to exit before refusing. The deadline includes
      // the first post-termination settle scan.
      genericHolderDeadline ??= Math.min(now() + genericHolderTimeoutMs, blockerDeadline?.deadlineAt ?? Infinity)

      await sleep(Math.min(terminationSettleMs, remainingBlockerMs()))
      firstClear = await scanFailClosed()

      if (firstClear.kind === 'probe-failure') {
        return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
      }

      firstClear = await pollGenericHoldersUntil(firstClear, genericHolderDeadline)

      if (firstClear.kind === 'probe-failure') {
        return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
      }

      if (firstClear.kind === 'blocked') {
        return {
          kind: 'blocked',
          reason: 'quiesce-incomplete',
          result: firstClear.result,
          message:
            firstClear.result.mcpBridges.length > 0
              ? quiesceIncompleteMessage()
              : formatBlockerMessage(firstClear.result)
        }
      }
    }

    while (true) {
      await sleep(Math.min(respawnIntervalMs, remainingBlockerMs()))
      let secondClear = await scanFailClosed()

      if (secondClear.kind === 'probe-failure') {
        return { kind: 'probe-failure', error: secondClear.error, message: formatProbeFailedMessage() }
      }

      if (secondClear.kind === 'blocked' && genericHoldersOnly(secondClear.result)) {
        genericHolderDeadline ??= Math.min(now() + genericHolderTimeoutMs, blockerDeadline?.deadlineAt ?? Infinity)
        secondClear = await pollGenericHoldersUntil(secondClear, genericHolderDeadline)

        if (secondClear.kind === 'probe-failure') {
          return { kind: 'probe-failure', error: secondClear.error, message: formatProbeFailedMessage() }
        }

        if (secondClear.kind === 'clear') {
          // The holder exited, but the last clear observation has not yet
          // survived a full stability interval.
          continue
        }
      }

      if (secondClear.kind === 'blocked') {
        return {
          kind: 'blocked',
          reason: 'quiesce-incomplete',
          result: secondClear.result,
          message: exactDrainableOnly(secondClear.result)
            ? quiesceIncompleteMessage()
            : formatBlockerMessage(secondClear.result)
        }
      }

      break
    }

    returnLease = true

    const outcome: Extract<UpdatePreflightOutcome, { kind: 'clear' }> = Object.freeze({
      kind: 'clear',
      // Preserve the exact capability-bearing lease object. The lease module
      // binds its private nonce to object identity; cloning it here would keep
      // the public fields while silently destroying handoff authority.
      lease
    })

    const permit: UpdateMutationPermit = Object.freeze({ preflight: outcome })
    successfulPreflightPermits.set(outcome, permit)
    updateMutationPermits.add(permit)

    return outcome
  } catch (error) {
    const detail = `preflight transaction failed: ${errorText(error)}`

    return { kind: 'probe-failure', error: detail, message: formatProbeFailedMessage() }
  } finally {
    if (!returnLease) {
      try {
        deps.clearMcpBridgeLease(lease)
      } catch {
        void 0
      }
    }
  }
}
