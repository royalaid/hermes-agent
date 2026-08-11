import type { McpBridgeQuiesceLease } from './mcp-bridge-quiesce'
import {
  formatBlockerMessage,
  formatProbeFailedMessage,
  isExactActionableMcpBridge,
  type McpBridgeProcess,
  type ScanOutcome,
  type VenvBlockerScanResult
} from './venv-blocker-scan'

export type UpdatePreflightPurpose = 'normal-update' | 'bootstrap-recovery'

export interface McpBridgeConsentRequest {
  continueLabel: string
  detail: string
  message: string
  ownerLabel: string
  title: string
}

export interface UpdatePreflightDeps {
  acquireMcpBridgeLease: () => McpBridgeQuiesceLease | null
  clearMcpBridgeLease: (lease: McpBridgeQuiesceLease) => void
  now?: () => number
  releaseTrackedBackendTrees: () => Promise<{ unlocked: boolean }>
  requestMcpBridgeConsent: (request: McpBridgeConsentRequest) => Promise<boolean>
  scan: () => Promise<ScanOutcome>
  terminateMcpBridge: (bridge: McpBridgeProcess) => Promise<boolean>
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
      reason: 'unlock-failed' | 'holders' | 'consent-declined' | 'lease-unavailable' | 'quiesce-incomplete'
      result?: VenvBlockerScanResult
    }
  | { kind: 'probe-failure'; error: string; message: string }

const DEFAULT_COOPERATIVE_EXIT_MS = 1_500
const DEFAULT_GENERIC_HOLDER_POLL_MS = 1_000
const DEFAULT_GENERIC_HOLDER_TIMEOUT_MS = 30_000
const DEFAULT_RESPAWN_INTERVAL_MS = 1_500
const DEFAULT_TERMINATION_SETTLE_MS = 750
const MAX_FALLBACK_BRIDGE_GROUPS = 32
const MAX_FALLBACK_BRIDGE_RECORDS = 64

function wait(delayMs: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, delayMs))
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function exactMcpOnly(result: VenvBlockerScanResult): boolean {
  return result.processes.length === 0 && exactActionableMcpBridges(result)
}

function exactActionableMcpBridges(result: VenvBlockerScanResult): boolean {
  return result.mcpBridges.length > 0 && result.mcpBridges.every(isExactActionableMcpBridge)
}

function logicalMcpBridgeGroupCount(result: VenvBlockerScanResult): number {
  return new Set(result.mcpBridges.map(bridge => bridge.wrapperPid ?? bridge.pid)).size
}

function genericHoldersOnly(result: VenvBlockerScanResult): boolean {
  return result.processes.length > 0 && result.mcpBridges.length === 0
}

function normalizeOwner(owner?: string): string | null {
  switch (owner?.trim().toLowerCase()) {
    case 'codex':
      return 'Codex'

    case 'claude':
      return 'Claude'

    case 'desktop':
      return 'Hermes Desktop'

    case 'other':
      return 'another agent'

    default:
      return null
  }
}

function joinOwners(owners: string[]): string {
  if (owners.length === 0) {return 'Another agent'}

  if (owners.length === 1) {return owners[0]}

  if (owners.length === 2) {return `${owners[0]} and ${owners[1]}`}

  return `${owners.slice(0, -1).join(', ')}, and ${owners.at(-1)}`
}

export function buildMcpBridgeConsentRequest(bridges: McpBridgeProcess[]): McpBridgeConsentRequest {
  if (bridges.length === 0) {
    return {
      ownerLabel: 'Codex and Claude',
      title: 'Temporarily pause Hermes MCP tools to update',
      continueLabel: 'Pause MCP bridges and continue',
      message: 'Hermes will temporarily pause new Codex and Claude MCP bridge launches during this update.',
      detail:
        'No exact Hermes MCP tool bridges are running now. During this bounded update transaction, ' +
        'any newly launched exact Hermes MCP tool bridges will pause until the update finishes. ' +
        'No Codex or Claude parent process will be stopped.'
    }
  }

  const owners = [...new Set(bridges.map(bridge => normalizeOwner(bridge.owner)).filter(Boolean))] as string[]
  const ownerLabel = joinOwners(owners)
  const processWord = bridges.length === 1 ? 'bridge' : 'bridges'
  const verb = owners.length > 1 ? 'are' : 'is'

  return {
    ownerLabel,
    title: 'Pause Hermes MCP tools to update',
    continueLabel: 'Pause tool bridges and continue',
    message: `${ownerLabel} ${verb} using ${bridges.length} Hermes MCP tool ${processWord}.`,
    detail:
      'Updating Hermes must pause the current exact Hermes MCP tool bridges shown below and may interrupt active tool calls. ' +
      'Any newly launched exact Hermes MCP tool bridges will also remain paused for this bounded update transaction. ' +
      'No Codex or Claude parent process will be stopped.\n\n' +
      bridges
        .slice(0, 8)
        .map(bridge => `PID ${bridge.pid}  ${normalizeOwner(bridge.owner) || 'Unknown owner'}  ${bridge.name}`)
        .join('\n')
  }
}

function quiesceIncompleteMessage(result?: VenvBlockerScanResult): string {
  const owners = result?.mcpBridges ?? []
  const ownerLabel = buildMcpBridgeConsentRequest(owners).ownerLabel

  return (
    `Update aborted: ${ownerLabel} Hermes MCP tool bridges did not remain stopped. ` +
    'Finish or cancel active tool calls, close the owning agent session if needed, then retry.'
  )
}

export async function runWindowsUpdatePreflight(
  _purpose: UpdatePreflightPurpose,
  deps: UpdatePreflightDeps,
  timing: UpdatePreflightTiming = {}
): Promise<UpdatePreflightOutcome> {
  const sleep = deps.wait ?? wait
  const now = deps.now ?? Date.now
  const cooperativeExitMs = timing.cooperativeExitMs ?? DEFAULT_COOPERATIVE_EXIT_MS
  const genericHolderPollMs = Math.max(1, timing.genericHolderPollMs ?? DEFAULT_GENERIC_HOLDER_POLL_MS)
  const genericHolderTimeoutMs = Math.max(0, timing.genericHolderTimeoutMs ?? DEFAULT_GENERIC_HOLDER_TIMEOUT_MS)
  const respawnIntervalMs = timing.respawnIntervalMs ?? DEFAULT_RESPAWN_INTERVAL_MS
  const terminationSettleMs = timing.terminationSettleMs ?? DEFAULT_TERMINATION_SETTLE_MS

  let lock: { unlocked: boolean }

  try {
    lock = await deps.releaseTrackedBackendTrees()
  } catch {
    lock = { unlocked: false }
  }

  if (lock?.unlocked !== true) {
    return {
      kind: 'blocked',
      reason: 'unlock-failed',
      message:
        'Update aborted: Hermes could not release its tracked local processes. ' +
        'Close other Hermes windows and terminals, then retry.'
    }
  }

  // Observe before activating the prevention lease. Existing bridge watchers
  // exit when they see that lease; scanning first is what guarantees the user
  // sees the proven owner and consents before active tool calls are interrupted.
  const scanFailClosed = async (): Promise<ScanOutcome> => {
    try {
      return await deps.scan()
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

      await sleep(Math.min(genericHolderPollMs, remainingMs))
      outcome = await scanFailClosed()
    }

    return outcome
  }

  const observed = await scanFailClosed()

  if (observed.kind === 'probe-failure') {
    return { kind: 'probe-failure', error: observed.error, message: formatProbeFailedMessage() }
  }

  if (observed.kind === 'blocked' && !exactMcpOnly(observed.result)) {
    return {
      kind: 'blocked',
      reason: 'holders',
      result: observed.result,
      message: formatBlockerMessage(observed.result)
    }
  }

  let accepted = false

  try {
    accepted = await deps.requestMcpBridgeConsent(
      buildMcpBridgeConsentRequest(observed.kind === 'blocked' ? observed.result.mcpBridges : [])
    )
  } catch {
    accepted = false
  }

  if (!accepted) {
    return {
      kind: 'blocked',
      reason: 'consent-declined',
      ...(observed.kind === 'blocked' ? { result: observed.result } : {}),
      message:
        observed.kind === 'blocked'
          ? 'Update cancelled. Hermes MCP tool bridges are still running.'
          : 'Update cancelled. Hermes did not pause new MCP bridge launches.'
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
    await sleep(cooperativeExitMs)
    let firstClear = await scanFailClosed()
    let genericHolderDeadline: number | null = null

    if (firstClear.kind === 'probe-failure') {
      return { kind: 'probe-failure', error: firstClear.error, message: formatProbeFailedMessage() }
    }

    // The cooperative lease may already have drained every MCP bridge while a
    // scheduled status/presence helper briefly holds the venv. It was never
    // consented for termination, so only wait boundedly for its natural exit.
    if (firstClear.kind === 'blocked' && genericHoldersOnly(firstClear.result)) {
      genericHolderDeadline = now() + genericHolderTimeoutMs
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
      const exactCurrentBridges = exactActionableMcpBridges(firstClear.result)
      const logicalBridgeGroups = logicalMcpBridgeGroupCount(firstClear.result)

      if (
        !exactCurrentBridges ||
        logicalBridgeGroups > MAX_FALLBACK_BRIDGE_GROUPS ||
        firstClear.result.mcpBridges.length > MAX_FALLBACK_BRIDGE_RECORDS
      ) {
        return {
          kind: 'blocked',
          reason: 'quiesce-incomplete',
          result: firstClear.result,
          message: exactMcpOnly(firstClear.result)
            ? quiesceIncompleteMessage(firstClear.result)
            : formatBlockerMessage(firstClear.result)
        }
      }

      // One bounded fallback pass. Each call delegates to the scanner, which
      // revalidates exact argv, executable, PID, and create time before killing
      // only that bridge process. There is deliberately no process-tree kill.
      const terminationOrder = [...firstClear.result.mcpBridges].sort(
        (left, right) => Number(right.role === 'mcp_bridge_worker') - Number(left.role === 'mcp_bridge_worker')
      )

      // A managed-runtime worker can derive its exact identity from its live
      // wrapper ancestry. Drain workers before wrappers and await each one so
      // the wrapper cannot disappear before the scanner revalidates a worker.
      for (const bridge of terminationOrder) {
        try {
          await deps.terminateMcpBridge(bridge)
        } catch {
          // The mandatory rescan below is the authority. One failed request
          // must not skip revalidation of the remaining consented entries.
          void 0
        }
      }

      // A scheduled gateway-status or presence probe can start after the
      // consent scan and briefly share this venv with an exact MCP bridge. It
      // is never part of the consented termination set. Give such generic
      // holders one bounded window to exit on their own after the exact bridge
      // fallback, then refuse if they remain. The deadline includes the first
      // post-termination settle scan so repeated probes cannot extend it.
      genericHolderDeadline ??= now() + genericHolderTimeoutMs

      await sleep(terminationSettleMs)
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
              ? quiesceIncompleteMessage(firstClear.result)
              : formatBlockerMessage(firstClear.result)
        }
      }
    }

    while (true) {
      await sleep(respawnIntervalMs)
      let secondClear = await scanFailClosed()

      if (secondClear.kind === 'probe-failure') {
        return { kind: 'probe-failure', error: secondClear.error, message: formatProbeFailedMessage() }
      }

      if (secondClear.kind === 'blocked' && genericHoldersOnly(secondClear.result)) {
        genericHolderDeadline ??= now() + genericHolderTimeoutMs
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
          message: exactMcpOnly(secondClear.result)
            ? quiesceIncompleteMessage(secondClear.result)
            : formatBlockerMessage(secondClear.result)
        }
      }

      break
    }

    returnLease = true

    return { kind: 'clear', lease }
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
