import fs from 'node:fs'
import path from 'node:path'

const BUILD_ID_PATTERN = /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/

export interface HandoffDesktopIdentityInput {
  currentPid: number
  currentProcessStartedAt: number
  execPath: string
  expectedExecutable: string
  expectedPid: number
  expectedProcessStartedAt: number
  expectedRoot: string
  resourcesPath: string
}

export interface HandoffDesktopIdentity {
  executable: string
  root: string
}

export interface HandoffBuildProof {
  buildId: string
  buildSource: 'install-stamp'
}

interface FileDeps {
  readFile?: (file: string) => string
  realpath?: (file: string) => string
}

function exactKeys(value: unknown, expected: string[]): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {return false}
  const keys = Object.keys(value).sort()
  const sorted = [...expected].sort()

  return keys.length === sorted.length && keys.every((key, index) => key === sorted[index])
}

function comparablePath(value: string, realpath: (file: string) => string): string | null {
  if (typeof value !== 'string' || !path.isAbsolute(value)) {return null}

  try {
    const resolved = realpath(value)

    return process.platform === 'win32' ? resolved.toLowerCase() : resolved
  } catch {
    return null
  }
}

/** Prove this is the exact Desktop process requested by the updater. */
export function validateHandoffDesktopIdentity(
  input: HandoffDesktopIdentityInput,
  { realpath = fs.realpathSync.native }: FileDeps = {}
): HandoffDesktopIdentity | null {
  if (!Number.isInteger(input.currentPid) || input.currentPid <= 0 || input.currentPid !== input.expectedPid) {return null}

  if (
    !Number.isInteger(input.currentProcessStartedAt) ||
    input.currentProcessStartedAt <= 0 ||
    !Number.isInteger(input.expectedProcessStartedAt) ||
    input.expectedProcessStartedAt <= 0 ||
    Math.abs(input.currentProcessStartedAt - input.expectedProcessStartedAt) > 1
  ) {
    return null
  }

  const root = comparablePath(input.expectedRoot, realpath)
  const executable = comparablePath(input.execPath, realpath)
  const expectedExecutable = comparablePath(input.expectedExecutable, realpath)
  const resources = comparablePath(input.resourcesPath, realpath)

  if (!root || !executable || !expectedExecutable || !resources) {return null}

  // A packaged/NSIS Hermes.exe may live under LocalAppData\Programs while
  // the managed source install is under HERMES_HOME\hermes-agent. Root and
  // executable are independent correlated identities: require the exact
  // updater-requested executable, but do not invent a containment relation.
  if (executable !== expectedExecutable) {return null}

  const expectedResources = comparablePath(path.join(path.dirname(executable), 'resources'), realpath)

  if (!expectedResources || resources !== expectedResources) {return null}

  return { executable, root }
}

/** Read only the strict packaged install stamp beside the exact executable. */
export function readCorrelatedInstallStamp(
  resourcesPath: string,
  expectedBuildId: string,
  { readFile = file => fs.readFileSync(file, 'utf8') }: FileDeps = {}
): HandoffBuildProof | null {
  if (!BUILD_ID_PATTERN.test(expectedBuildId)) {return null}

  let parsed: unknown

  try {
    parsed = JSON.parse(readFile(path.join(resourcesPath, 'install-stamp.json')))
  } catch {
    return null
  }

  if (!exactKeys(parsed, ['schemaVersion', 'commit', 'branch', 'builtAt', 'dirty', 'source'])) {return null}

  if (
    parsed.schemaVersion !== 1 ||
    typeof parsed.commit !== 'string' ||
    !BUILD_ID_PATTERN.test(parsed.commit) ||
    parsed.commit.toLowerCase() !== expectedBuildId.toLowerCase() ||
    !(parsed.branch === null || (typeof parsed.branch === 'string' && parsed.branch.length > 0)) ||
    typeof parsed.builtAt !== 'string' ||
    !Number.isFinite(Date.parse(parsed.builtAt)) ||
    parsed.dirty !== false ||
    (parsed.source !== 'ci' && parsed.source !== 'local')
  ) {
    return null
  }

  return { buildId: parsed.commit.toLowerCase(), buildSource: 'install-stamp' }
}
