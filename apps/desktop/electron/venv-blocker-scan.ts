'use strict'

/**
 * venv-blocker-scan.ts
 *
 * Thin helper that runs the Python venv-blocker scan as a subprocess and
 * returns a typed result for the Desktop update preflight.
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type VenvBlockerKind = 'local-preview' | 'other'

/** The identity fields every scanner record carries, whatever its role. */
export interface VenvBlockerIdentity {
  pid: number
  name: string
  cmdline: string
}

export interface VenvBlockerProcess extends VenvBlockerIdentity {
  kind: VenvBlockerKind
  safeToStop: boolean
  label?: string
  port?: number
  createTime?: number
}

// An MCP bridge is never "safe to stop" through the local-preview path: it is
// paused by terminateMcpBridge after explicit consent.  So it carries the
// shared identity without the preview classification.
export interface McpBridgeProcess extends VenvBlockerIdentity {
  action: 'refuse' | 'terminate_exact_mcp'
  actionable: boolean
  actionability: 'exact_mcp_bridge' | 'hard_block'
  createdAt: number
  owner: 'claude' | 'codex' | 'desktop' | 'unknown'
  role: 'mcp_bridge_worker' | 'mcp_bridge_wrapper'
  wrapperPid?: number
}

export interface VenvBlockerScanResult {
  blocked: boolean
  processes: VenvBlockerProcess[]
  mcpBridges: McpBridgeProcess[]
  pausableGateways: number
}

export type ScanOutcome =
  | { kind: 'clear'; result: VenvBlockerScanResult }
  | { kind: 'blocked'; result: VenvBlockerScanResult }
  | { kind: 'probe-failure'; error: string }

export function isExactActionableMcpBridge(bridge: McpBridgeProcess): boolean {
  return (
    (bridge.owner === 'codex' || bridge.owner === 'claude') &&
    (bridge.role === 'mcp_bridge_wrapper' || bridge.role === 'mcp_bridge_worker') &&
    bridge.actionable === true &&
    bridge.actionability === 'exact_mcp_bridge' &&
    bridge.action === 'terminate_exact_mcp' &&
    Number.isInteger(bridge.pid) &&
    bridge.pid > 0 &&
    Number.isFinite(bridge.createdAt) &&
    bridge.createdAt > 0
  )
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SCAN_TIMEOUT_MS = 15000
const SCAN_MODULE = 'hermes_cli._scan_venv_blockers'

/** Optional UI metadata the scanner attaches to an exact `-m http.server` record. */
const LOCAL_PREVIEW_HINT_KEYS = ['kind', 'safeToStop', 'label', 'port', 'createTime']

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function classifyVenvBlocker(
  process: Pick<VenvBlockerProcess, 'pid' | 'name' | 'cmdline'>,
  hints?: Record<string, unknown>
): VenvBlockerProcess {
  const moduleMatch = process.cmdline.match(/(?:^|\s)-m\s+http\.server(?:\s+(\d{1,5}))?(?:\s|$)/i)
  const isPython = /^python(?:w)?(?:\.exe)?$/i.test(process.name)
  const hintedCreateTime = typeof hints?.createTime === 'number' ? hints.createTime : undefined

  const trustedScannerIdentity =
    hints?.kind === 'local-preview' &&
    hints.safeToStop === true &&
    hintedCreateTime !== undefined &&
    Number.isFinite(hintedCreateTime) &&
    hintedCreateTime > 0

  if (!isPython || !moduleMatch || !trustedScannerIdentity) {
    return { ...process, kind: 'other', safeToStop: false }
  }

  const parsedPort = moduleMatch[1] ? Number(moduleMatch[1]) : 8000
  const hintedPort = trustedScannerIdentity && typeof hints?.port === 'number' ? hints.port : undefined
  const candidatePort = hintedPort ?? parsedPort

  const port =
    Number.isInteger(candidatePort) && candidatePort > 0 && candidatePort <= 65535 ? candidatePort : undefined

  const directoryMatch = process.cmdline.match(/(?:^|\s)--directory\s+(?:"([^"]+)"|'([^']+)'|(.+))$/i)
  const directory = (directoryMatch?.[1] || directoryMatch?.[2] || directoryMatch?.[3] || '').trim()
  const parsedLabel = directory ? path.win32.basename(directory.replace(/["']$/, '')) : undefined
  const hintedLabel = trustedScannerIdentity && typeof hints?.label === 'string' ? hints.label.trim() : ''
  const label = hintedLabel || parsedLabel

  return {
    ...process,
    kind: 'local-preview',
    safeToStop: true,
    ...(label ? { label } : {}),
    ...(port ? { port } : {}),
    createTime: hintedCreateTime
  }
}

/**
 * Stop only blockers that the fresh scanner identified as Python static-file
 * preview servers. Unknown Python/Hermes processes are deliberately ignored.
 */
export async function stopSafeVenvBlockers(
  updateRoot: string,
  result: VenvBlockerScanResult,
  execOverride?: typeof execFileAsync,
  resolvePython: typeof resolveVenvPython = resolveVenvPython
): Promise<{ stopped: number[]; failed: number[] }> {
  const execFn = execOverride || execFileAsync
  const stopped: number[] = []
  const failed: number[] = []
  const pythonPath = resolvePython(updateRoot)

  for (const process of result.processes) {
    if (
      !pythonPath ||
      !process.safeToStop ||
      process.kind !== 'local-preview' ||
      !process.createTime ||
      !Number.isFinite(process.createTime)
    ) {
      if (process.safeToStop && process.kind === 'local-preview') {
        failed.push(process.pid)
      }

      continue
    }

    try {
      await execFn(
        pythonPath,
        ['-m', 'hermes_cli._scan_venv_blockers', '--terminate-safe', String(process.pid), String(process.createTime)],
        { cwd: updateRoot, windowsHide: true, timeout: 10_000, maxBuffer: 256 * 1024 }
      )
      stopped.push(process.pid)
    } catch {
      failed.push(process.pid)
    }
  }

  return { stopped, failed }
}

/**
 * Strictly validate and parse the JSON output from the venv-blocker scan.
 * Pure function — no side effects.
 */
interface ScanTargetIdentity {
  expectedRoot: string
  expectedVenv: string
}

function hasExactKeys(
  value: unknown,
  required: string[],
  optional: string[] = []
): value is Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {return false}
  const actual = Object.keys(value).sort()
  const allowed = new Set([...required, ...optional])

  return required.every(key => Object.hasOwn(value, key)) && actual.every(key => allowed.has(key))
}

function comparableCanonicalPath(value: unknown): string | null {
  if (typeof value !== 'string' || !path.isAbsolute(value)) {return null}
  const normalized = path.normalize(value)

  return process.platform === 'win32' ? normalized.toLowerCase() : normalized
}

function parseIdentityRecord(
  entry: unknown,
  kind: 'gateway' | 'process',
  seenPids: Set<number>
): VenvBlockerProcess | null {
  const required = ['pid', 'name', 'cmdline', 'owner', 'role', 'actionable', 'actionability', 'action']

  if (kind === 'gateway') {required.push('created_at')}

  // A generic record may additionally carry the scanner's local-preview UI
  // metadata.  Those hints never relax the hard block the tuple below enforces;
  // they only tell Desktop which single PID it may ask the scanner to stop.
  const optional = kind === 'process' ? ['created_at', ...LOCAL_PREVIEW_HINT_KEYS] : []

  if (!hasExactKeys(entry, required, optional)) {return null}
  const { pid, name, cmdline, owner, role, actionable, actionability, action, created_at: createdAt } = entry

  if (
    !Number.isInteger(pid) ||
    pid <= 0 ||
    seenPids.has(pid) ||
    typeof name !== 'string' ||
    name.length === 0 ||
    typeof cmdline !== 'string' ||
    cmdline.length > 120 ||
    (createdAt !== undefined &&
      (typeof createdAt !== 'number' || !Number.isFinite(createdAt) || createdAt <= 0))
  ) {
    return null
  }

  const validTuple =
    kind === 'gateway'
      ? owner === 'gateway' &&
        role === 'gateway_run' &&
        actionable === false &&
        actionability === 'downstream_drainable' &&
        action === 'pause_downstream'
      : ((owner === 'desktop' && role === 'desktop_backend') || (owner === 'unknown' && role === 'other')) &&
        actionable === false &&
        actionability === 'hard_block' &&
        action === 'refuse'

  if (!validTuple) {return null}

  seenPids.add(pid)

  // classifyVenvBlocker re-validates every hint itself, so an absent, partial,
  // or malformed hint set degrades to a plain unstoppable 'other' blocker.
  return classifyVenvBlocker({ pid, name, cmdline }, entry)
}

export function parseVenvBlockerScanOutput(raw: string, target: ScanTargetIdentity): ScanOutcome {
  let parsed: any

  try {
    parsed = JSON.parse(raw)
  } catch {
    return { kind: 'probe-failure', error: 'malformed JSON' }
  }

  const fields = [
    'schema_version',
    'mode',
    'ok',
    'ready',
    'blocked',
    'reason',
    'root',
    'venv',
    'processes',
    'mcp_bridges',
    'pausable_gateways',
    'pausable_gateway_processes',
    'error'
  ]

  if (!hasExactKeys(parsed, fields)) {return { kind: 'probe-failure', error: 'scanner envelope fields are invalid' }}

  if (
    parsed.schema_version !== 1 ||
    parsed.mode !== 'scan' ||
    parsed.ok !== true ||
    typeof parsed.ready !== 'boolean' ||
    typeof parsed.blocked !== 'boolean' ||
    parsed.error !== null
  ) {
    return { kind: 'probe-failure', error: 'scanner envelope metadata is invalid' }
  }

  const actualRoot = comparableCanonicalPath(parsed.root)
  const actualVenv = comparableCanonicalPath(parsed.venv)
  const expectedRoot = comparableCanonicalPath(target.expectedRoot)
  const expectedVenv = comparableCanonicalPath(target.expectedVenv)

  if (
    actualRoot === null ||
    actualVenv === null ||
    expectedRoot === null ||
    expectedVenv === null ||
    actualRoot !== expectedRoot ||
    actualVenv !== expectedVenv
  ) {
    return { kind: 'probe-failure', error: 'scanner target identity does not match the requested root and venv' }
  }

  if (!Array.isArray(parsed.processes) || !Array.isArray(parsed.mcp_bridges)) {
    return { kind: 'probe-failure', error: 'scanner process fields must be arrays' }
  }

  if (!Array.isArray(parsed.pausable_gateway_processes)) {
    return { kind: 'probe-failure', error: 'pausable_gateway_processes must be an array' }
  }

  const processes: VenvBlockerProcess[] = []
  const seenPids = new Set<number>()

  for (const entry of parsed.processes) {
    const process = parseIdentityRecord(entry, 'process', seenPids)

    if (!process) {return { kind: 'probe-failure', error: 'generic process identity is invalid' }}
    processes.push(process)
  }

  const parsedMcpBridges: Array<{ bridge: McpBridgeProcess; wrapperPid?: number }> = []

  for (const entry of parsed.mcp_bridges) {
    const required = [
      'pid',
      'name',
      'cmdline',
      'created_at',
      'owner',
      'role',
      'actionable',
      'actionability',
      'action'
    ]

    if (!hasExactKeys(entry, required, ['wrapper_pid'])) {
      return { kind: 'probe-failure', error: 'MCP bridge entry must be an object' }
    }

    const {
      pid,
      name,
      cmdline,
      created_at: createdAt,
      owner,
      role,
      actionable,
      actionability,
      action,
      wrapper_pid: wrapperPid
    } = entry

    if (!Number.isInteger(pid) || pid <= 0) {
      return { kind: 'probe-failure', error: 'MCP bridge pid must be a positive integer' }
    }

    if (typeof name !== 'string' || name.length === 0) {
      return { kind: 'probe-failure', error: 'MCP bridge name must be a non-empty string' }
    }

    if (typeof cmdline !== 'string' || cmdline.length > 120) {
      return { kind: 'probe-failure', error: 'MCP bridge cmdline must be a string' }
    }

    if (typeof createdAt !== 'number' || !Number.isFinite(createdAt) || createdAt <= 0) {
      return { kind: 'probe-failure', error: 'MCP bridge created_at must be a positive number' }
    }

    if (!['claude', 'codex', 'desktop', 'unknown'].includes(owner)) {
      return { kind: 'probe-failure', error: 'MCP bridge owner is invalid' }
    }

    if (!['mcp_bridge_worker', 'mcp_bridge_wrapper'].includes(role)) {
      return { kind: 'probe-failure', error: 'MCP bridge role is invalid' }
    }

    if (typeof actionable !== 'boolean') {
      return { kind: 'probe-failure', error: 'MCP bridge actionable flag is missing or invalid' }
    }

    if (!['exact_mcp_bridge', 'hard_block'].includes(actionability)) {
      return { kind: 'probe-failure', error: 'MCP bridge actionability is invalid' }
    }

    if (!['refuse', 'terminate_exact_mcp'].includes(action)) {
      return { kind: 'probe-failure', error: 'MCP bridge action is missing or invalid' }
    }

    if (wrapperPid !== undefined && (!Number.isInteger(wrapperPid) || wrapperPid <= 0 || wrapperPid === pid)) {
      return { kind: 'probe-failure', error: 'MCP bridge wrapper_pid is invalid' }
    }

    if (wrapperPid !== undefined && role !== 'mcp_bridge_worker') {
      return { kind: 'probe-failure', error: 'only an MCP bridge worker may name a wrapper_pid' }
    }

    if (
      actionable !== (owner === 'codex' || owner === 'claude') ||
      (actionable && (actionability !== 'exact_mcp_bridge' || action !== 'terminate_exact_mcp')) ||
      (!actionable && (actionability !== 'hard_block' || action !== 'refuse'))
    ) {
      return { kind: 'probe-failure', error: 'MCP bridge action fields are inconsistent' }
    }

    if (seenPids.has(pid)) {
      return { kind: 'probe-failure', error: 'a PID cannot appear more than once' }
    }

    seenPids.add(pid)
    parsedMcpBridges.push({
      bridge: {
        pid,
        name,
        cmdline,
        createdAt,
        owner,
        role,
        actionable,
        actionability,
        action,
        ...(wrapperPid === undefined ? {} : { wrapperPid })
      },
      ...(wrapperPid === undefined ? {} : { wrapperPid })
    })
  }

  const mcpRolesByPid = new Map(
    parsedMcpBridges.map(({ bridge }) => [bridge.pid, bridge.role] as const)
  )

  for (const { wrapperPid } of parsedMcpBridges) {
    if (wrapperPid !== undefined && mcpRolesByPid.get(wrapperPid) !== 'mcp_bridge_wrapper') {
      return { kind: 'probe-failure', error: 'MCP bridge wrapper_pid does not identify a wrapper record' }
    }
  }

  const mcpBridges = parsedMcpBridges.map(({ bridge }) => bridge)

  const pausableGateways = parsed.pausable_gateways

  if (
    !Number.isInteger(pausableGateways) ||
    pausableGateways < 0 ||
    pausableGateways !== parsed.pausable_gateway_processes.length
  ) {
    return { kind: 'probe-failure', error: 'pausable_gateways must be a non-negative integer' }
  }

  for (const entry of parsed.pausable_gateway_processes) {
    if (!parseIdentityRecord(entry, 'gateway', seenPids)) {
      return { kind: 'probe-failure', error: 'pausable gateway identity is invalid' }
    }
  }

  // Reject inconsistent combinations.
  const blocked = processes.length + mcpBridges.length > 0

  if (
    parsed.blocked !== blocked ||
    parsed.ready !== !blocked ||
    parsed.reason !== (blocked ? 'processes_running' : null)
  ) {
    return { kind: 'probe-failure', error: 'scanner readiness fields are inconsistent' }
  }

  return parsed.blocked
    ? {
        kind: 'blocked',
        result: { blocked: true, processes, mcpBridges, pausableGateways }
      }
    : {
        kind: 'clear',
        result: { blocked: false, processes, mcpBridges, pausableGateways }
      }
}

/**
 * Run the venv-blocker scan subprocess.  Async so the Electron main-process
 * event loop is never blocked by the psutil process scan (up to 15s on a
 * loaded Windows box).  Accepts optional overrides for testing (dependency
 * injection).
 */
export async function scanVenvBlockers(
  updateRoot: string,
  execOverride?: typeof execFileAsync,
  resolveOverride?: typeof resolveVenvPython,
  canonicalizeOverride?: (root: string) => string
): Promise<ScanOutcome> {
  const execFn = execOverride || execFileAsync
  const resolveFn = resolveOverride || resolveVenvPython

  const canonicalizeFn =
    canonicalizeOverride ?? ((target: string) => fs.realpathSync.native(target))

  let scanRoot: string

  try {
    scanRoot = canonicalizeFn(updateRoot)
  } catch {
    return { kind: 'probe-failure', error: 'update root could not be resolved' }
  }

  const venvPython = resolveFn(scanRoot)

  if (!venvPython) {
    return { kind: 'probe-failure', error: 'venv python not found' }
  }

  let scanVenv: string

  try {
    scanVenv = canonicalizeFn(path.dirname(path.dirname(venvPython)))
  } catch {
    return { kind: 'probe-failure', error: 'venv directory could not be resolved' }
  }

  let stdout: string

  try {
    const env = { ...process.env }
    delete env.PYTHONPATH

    const proc = await execFn(venvPython, ['-m', SCAN_MODULE, '--root', scanRoot], {
      cwd: scanRoot,
      encoding: 'utf-8',
      timeout: SCAN_TIMEOUT_MS,
      windowsHide: true,
      env
    } as any)

    stdout = String((proc as any).stdout ?? '')
  } catch (err: any) {
    const diag = [`exit code ${err.status ?? err.code ?? -1}`]

    if (err.stderr) {
      diag.push(String(err.stderr).slice(0, 200))
    }

    return { kind: 'probe-failure', error: diag.join('; ') }
  }

  return parseVenvBlockerScanOutput(stdout, {
    expectedRoot: scanRoot,
    expectedVenv: scanVenv
  })
}

function parseTerminateMcpBridgeOutput(
  raw: string,
  target: ScanTargetIdentity,
  bridge: McpBridgeProcess
): boolean {
  let parsed: any

  try {
    parsed = JSON.parse(raw)
  } catch {
    return false
  }

  const fields = [
    'schema_version',
    'mode',
    'ok',
    'terminated',
    'pid',
    'created_at',
    'root',
    'venv',
    'error'
  ]

  if (!hasExactKeys(parsed, fields)) {return false}

  const actualRoot = comparableCanonicalPath(parsed.root)
  const actualVenv = comparableCanonicalPath(parsed.venv)
  const expectedRoot = comparableCanonicalPath(target.expectedRoot)
  const expectedVenv = comparableCanonicalPath(target.expectedVenv)

  if (
    parsed.schema_version !== 1 ||
    parsed.mode !== 'terminate_mcp_bridge' ||
    parsed.ok !== true ||
    typeof parsed.terminated !== 'boolean' ||
    parsed.pid !== bridge.pid ||
    parsed.created_at !== bridge.createdAt ||
    parsed.error !== null ||
    actualRoot === null ||
    actualVenv === null ||
    expectedRoot === null ||
    expectedVenv === null ||
    actualRoot !== expectedRoot ||
    actualVenv !== expectedVenv
  ) {
    return false
  }

  return parsed.terminated
}

/**
 * Ask the scanner to terminate one already-consented MCP bridge.
 *
 * The Python side re-reads executable, argv, PID, and create time immediately
 * before terminating that one process. This helper never calls taskkill and
 * never targets the owning Codex/Claude process tree.
 */
export async function terminateMcpBridge(
  updateRoot: string,
  bridge: McpBridgeProcess,
  execOverride?: typeof execFileAsync,
  resolveOverride?: typeof resolveVenvPython,
  canonicalizeOverride?: (root: string) => string
): Promise<boolean> {
  if (!isExactActionableMcpBridge(bridge)) {
    return false
  }

  const execFn = execOverride || execFileAsync
  const resolveFn = resolveOverride || resolveVenvPython

  const canonicalizeFn =
    canonicalizeOverride ?? ((target: string) => fs.realpathSync.native(target))

  let scanRoot: string

  try {
    scanRoot = canonicalizeFn(updateRoot)
  } catch {
    return false
  }

  const venvPython = resolveFn(scanRoot)

  if (!venvPython) {
    return false
  }

  let scanVenv: string

  try {
    scanVenv = canonicalizeFn(path.dirname(path.dirname(venvPython)))
  } catch {
    return false
  }

  const env = { ...process.env }
  delete env.PYTHONPATH

  try {
    const proc = await execFn(
      venvPython,
      [
        '-m',
        SCAN_MODULE,
        '--root',
        scanRoot,
        '--terminate-mcp-bridge',
        String(bridge.pid),
        '--created-at',
        String(bridge.createdAt)
      ],
      { cwd: scanRoot, encoding: 'utf-8', timeout: SCAN_TIMEOUT_MS, windowsHide: true, env } as any
    )

    return parseTerminateMcpBridgeOutput(
      String((proc as any).stdout ?? ''),
      {
        expectedRoot: scanRoot,
        expectedVenv: scanVenv
      },
      bridge
    )
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// Internal helpers (exported for testing)
// ---------------------------------------------------------------------------

/** Resolve the venv python path.  Returns null if the file does not exist. */
export function resolveVenvPython(updateRoot: string): string | null {
  const isWindows = process.platform === 'win32'
  const pythonName = isWindows ? 'python.exe' : 'python3'
  const scriptsDir = isWindows ? 'Scripts' : 'bin'
  const candidate = path.join(updateRoot, 'venv', scriptsDir, pythonName)

  try {
    fs.accessSync(candidate)

    return candidate
  } catch {
    return null
  }
}

/**
 * Build a human-readable error message from blocker scan results.
 * Does NOT recommend --force-venv.
 */
export function formatBlockerMessage(result: VenvBlockerScanResult): string {
  const lines = [
    'Update aborted: another process is using this Hermes installation.',
    '',
    'These processes must be stopped before updating:',
    ''
  ]

  for (const proc of result.processes.slice(0, 10)) {
    lines.push(`  PID ${proc.pid}  ${proc.name}  ${proc.cmdline}`)
  }

  if (result.processes.length > 10) {
    lines.push(`  ... and ${result.processes.length - 10} more`)
  }

  if (result.mcpBridges.length > 0) {
    lines.push('')
    lines.push('Hermes MCP tool bridges still using this installation:')

    for (const bridge of result.mcpBridges.slice(0, 10)) {
      const owner =
        bridge.owner === 'codex'
          ? 'Codex'
          : bridge.owner === 'claude'
            ? 'Claude'
            : bridge.owner === 'desktop'
              ? 'Hermes Desktop'
              : 'another agent'

      lines.push(`  PID ${bridge.pid}  ${owner}  ${bridge.name}`)
    }
  }

  lines.push('')
  lines.push(
    'Close the terminal, app, service, or owning agent session shown above. ' +
      'Stopping a remote Hermes service will disconnect its clients.'
  )
  lines.push('Then retry the update.')

  return lines.join('\n')
}

/**
 * Build a probe-failure error message.
 */
export function formatProbeFailedMessage(): string {
  return (
    'Update aborted: Desktop could not verify the Hermes installation is free.\n' +
    '\n' +
    'Close other Hermes windows and terminals, then retry.  If the problem\n' +
    'persists, run `hermes update` in a terminal for detailed diagnostics.'
  )
}
