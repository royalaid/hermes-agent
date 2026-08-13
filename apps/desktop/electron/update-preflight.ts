import {
  formatBlockerMessage,
  formatProbeFailedMessage,
  isExactVenvHolder,
  type DesktopPluginServiceProcess,
  type ScanOutcome,
  type VenvBlockerProcess,
  type VenvBlockerScanResult
} from './venv-blocker-scan'

export interface ForceDrainDeps {
  releaseTrackedBackendTrees: () => Promise<{ unlocked: boolean }>
  scan: () => Promise<ScanOutcome>
  terminateDesktopPluginService: (service: DesktopPluginServiceProcess) => Promise<boolean>
  terminateVenvHolder: (holder: VenvBlockerProcess) => Promise<boolean>
  wait?: (delayMs: number) => Promise<void>
}

export interface ForceDrainTiming {
  respawnIntervalMs?: number
  terminationSettleMs?: number
}

export type ForceDrainOutcome =
  | { kind: 'clear' }
  | {
      kind: 'blocked'
      message: string
      reason: 'unlock-failed' | 'holders' | 'drain-incomplete'
      result?: VenvBlockerScanResult
    }
  | { kind: 'probe-failure'; error: string; message: string }

const DEFAULT_RESPAWN_INTERVAL_MS = 1_500
const DEFAULT_TERMINATION_SETTLE_MS = 750
const MAX_DRAIN_GROUPS = 32
const MAX_DRAIN_RECORDS = 64

function wait(delayMs: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, delayMs))
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function exactDrainableOnly(result: VenvBlockerScanResult): boolean {
  return (
    result.processes.every(isExactVenvHolder) &&
    result.mcpBridges.every(isExactVenvHolder) &&
    result.desktopPluginServices.every(isExactVenvHolder) &&
    result.processes.length + result.mcpBridges.length + result.desktopPluginServices.length > 0
  )
}

function logicalDrainGroupCount(processes: ReadonlyArray<{ pid: number; wrapperPid?: number }>): number {
  return new Set(processes.map(process => process.wrapperPid ?? process.pid)).size
}

function drainIncompleteMessage(): string {
  return (
    'Update aborted: processes from this Hermes installation did not remain stopped. ' +
    'Close or stop the restarting service, then retry.'
  )
}

/**
 * Release Desktop-owned backends and force-drain every process the scanner can
 * freshly prove belongs to this exact Hermes root and venv. Choosing Update is
 * the authorization; unproven processes remain a hard refusal.
 */
export async function runWindowsForceDrainPreflight(
  deps: ForceDrainDeps,
  timing: ForceDrainTiming = {}
): Promise<ForceDrainOutcome> {
  const sleep = deps.wait ?? wait
  const terminationSettleMs = timing.terminationSettleMs ?? DEFAULT_TERMINATION_SETTLE_MS
  const respawnIntervalMs = timing.respawnIntervalMs ?? DEFAULT_RESPAWN_INTERVAL_MS

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

  const scanFailClosed = async (): Promise<ScanOutcome> => {
    try {
      return await deps.scan()
    } catch (error) {
      return { kind: 'probe-failure', error: `scanner threw: ${errorText(error)}` }
    }
  }

  let outcome = await scanFailClosed()

  if (outcome.kind === 'probe-failure') {
    return { kind: 'probe-failure', error: outcome.error, message: formatProbeFailedMessage() }
  }

  if (outcome.kind === 'clear') {
    return { kind: 'clear' }
  }

  if (!exactDrainableOnly(outcome.result)) {
    return {
      kind: 'blocked',
      reason: 'holders',
      result: outcome.result,
      message: formatBlockerMessage(outcome.result)
    }
  }

  const logicalDrainGroups =
    logicalDrainGroupCount(outcome.result.mcpBridges) +
    logicalDrainGroupCount(outcome.result.desktopPluginServices)
  const drainRecordCount =
    outcome.result.processes.length + outcome.result.mcpBridges.length + outcome.result.desktopPluginServices.length

  if (logicalDrainGroups > MAX_DRAIN_GROUPS || drainRecordCount > MAX_DRAIN_RECORDS) {
    return {
      kind: 'blocked',
      reason: 'drain-incomplete',
      result: outcome.result,
      message: drainIncompleteMessage()
    }
  }

  // Workers first: their identity can depend on their wrapper's live ancestry.
  const bridges = [...outcome.result.mcpBridges].sort(
    (left, right) => Number(right.role === 'mcp_bridge_worker') - Number(left.role === 'mcp_bridge_worker')
  )
  const services = [...outcome.result.desktopPluginServices].sort(
    (left, right) => Number(right.role === 'desktop_plugin_worker') - Number(left.role === 'desktop_plugin_worker')
  )

  for (const bridge of bridges) {
    try {
      await deps.terminateVenvHolder(bridge)
    } catch {
      // The mandatory rescan below remains authoritative.
    }
  }

  for (const service of services) {
    try {
      await deps.terminateDesktopPluginService(service)
    } catch {
      // The mandatory rescan below remains authoritative.
    }
  }

  for (const holder of outcome.result.processes) {
    try {
      await deps.terminateVenvHolder(holder)
    } catch {
      // The mandatory rescan below remains authoritative.
    }
  }

  await sleep(terminationSettleMs)
  outcome = await scanFailClosed()

  if (outcome.kind === 'probe-failure') {
    return { kind: 'probe-failure', error: outcome.error, message: formatProbeFailedMessage() }
  }

  if (outcome.kind === 'blocked') {
    return {
      kind: 'blocked',
      reason: 'drain-incomplete',
      result: outcome.result,
      message: exactDrainableOnly(outcome.result) ? drainIncompleteMessage() : formatBlockerMessage(outcome.result)
    }
  }

  await sleep(respawnIntervalMs)
  outcome = await scanFailClosed()

  if (outcome.kind === 'probe-failure') {
    return { kind: 'probe-failure', error: outcome.error, message: formatProbeFailedMessage() }
  }

  if (outcome.kind === 'blocked') {
    return {
      kind: 'blocked',
      reason: 'drain-incomplete',
      result: outcome.result,
      message: exactDrainableOnly(outcome.result) ? drainIncompleteMessage() : formatBlockerMessage(outcome.result)
    }
  }

  return { kind: 'clear' }
}
