import { readCorrelatedInstallStamp, validateHandoffDesktopIdentity } from './handoff-desktop-proof'
import {
  expectedHandoffBuildId,
  type HandoffResult,
  readHandoffResult,
  waitForTerminalHandoffResult,
  writeHandoffAck
} from './handoff-result'

export interface HandoffBackendReadiness {
  backendReady: true
  backendMode: 'local' | 'remote'
}

export interface HandoffResultLifecycleOptions {
  currentExecutable: string
  currentPid: number
  currentProcessStartedAt: number
  expectedRoot: string
  getBackendReadiness: () => HandoffBackendReadiness | null
  resourcesPath: string
  discoveryTimeoutMs?: number
  terminalTimeoutMs?: number
  pollMs?: number
  wait?: (delayMs: number) => Promise<void>
  onStatus?: (status: string) => void
}

export interface HandoffResultRetryOptions {
  resolveCurrentProcessStartedAt: () => number | null | Promise<number | null>
  retryDelayMs?: number
  shouldRetryAfterNull: () => boolean
  wait?: (delayMs: number) => Promise<void>
}

interface HandoffLifecycleDeps {
  expectedBuildId: typeof expectedHandoffBuildId
  readBuildProof: typeof readCorrelatedInstallStamp
  readResult: typeof readHandoffResult
  validateDesktopIdentity: typeof validateHandoffDesktopIdentity
  waitForTerminal: typeof waitForTerminalHandoffResult
  writeAck: typeof writeHandoffAck
}

const DEFAULT_DEPS: HandoffLifecycleDeps = {
  expectedBuildId: expectedHandoffBuildId,
  readBuildProof: readCorrelatedInstallStamp,
  readResult: readHandoffResult,
  validateDesktopIdentity: validateHandoffDesktopIdentity,
  waitForTerminal: waitForTerminalHandoffResult,
  writeAck: writeHandoffAck
}

const defaultWait = (delayMs: number) => new Promise<void>(resolve => setTimeout(resolve, delayMs))

function isPositiveExactTimestamp(value: number | null): value is number {
  return Number.isSafeInteger(value) && value > 0
}

/**
 * Repeat bounded discovery windows while a caller can still prove that an
 * updater result is expected. This keeps a Desktop opened mid-update (remote
 * or local) from permanently exhausting one early ten-second poll.
 */
export async function retryHandoffResultLifecycle(
  runOnce: (currentProcessStartedAt: number | null) => Promise<HandoffResult | null>,
  {
    resolveCurrentProcessStartedAt,
    retryDelayMs = 1_000,
    shouldRetryAfterNull,
    wait = defaultWait
  }: HandoffResultRetryOptions
): Promise<HandoffResult | null> {
  if (!Number.isFinite(retryDelayMs) || retryDelayMs <= 0) {return null}

  let currentProcessStartedAt: number | null = null

  while (true) {
    if (!isPositiveExactTimestamp(currentProcessStartedAt)) {
      let resolvedProcessStartedAt: number | null = null

      try {
        resolvedProcessStartedAt = await resolveCurrentProcessStartedAt()
      } catch {
        // Keep identity unknown for this bounded discovery window. A later
        // lifecycle retry can make a fresh OS query.
      }

      if (isPositiveExactTimestamp(resolvedProcessStartedAt)) {
        currentProcessStartedAt = resolvedProcessStartedAt
      }
    }

    const result = await runOnce(currentProcessStartedAt)

    if (result || !shouldRetryAfterNull()) {return result}
    await wait(retryDelayMs)
  }
}

async function waitForBackendReadiness(
  getBackendReadiness: () => HandoffBackendReadiness | null,
  timeoutMs: number,
  pollMs: number,
  wait: (delayMs: number) => Promise<void>,
  shouldStop: () => boolean
): Promise<HandoffBackendReadiness | null> {
  let elapsedMs = 0

  while (!shouldStop()) {
    const readiness = getBackendReadiness()

    if (readiness) {return readiness}

    if (elapsedMs >= timeoutMs) {return null}
    const delayMs = Math.min(pollMs, timeoutMs - elapsedMs)
    await wait(delayMs)
    elapsedMs += delayMs
  }

  return null
}

/**
 * Discover this updater attempt, ACK only after exact process/build/backend
 * proof, then consume only the updater's correlated terminal result. Pending
 * is never returned to callers as success and remains on disk for the updater.
 */
export async function runHandoffResultLifecycle(
  hermesHome: string,
  {
    currentExecutable,
    currentPid,
    currentProcessStartedAt,
    expectedRoot,
    getBackendReadiness,
    resourcesPath,
    discoveryTimeoutMs = 10_000,
    terminalTimeoutMs = 210_000,
    pollMs = 200,
    wait = defaultWait,
    onStatus = () => {}
  }: HandoffResultLifecycleOptions,
  deps: HandoffLifecycleDeps = DEFAULT_DEPS
): Promise<HandoffResult | null> {
  if (
    !Number.isFinite(discoveryTimeoutMs) ||
    discoveryTimeoutMs < 0 ||
    !Number.isFinite(terminalTimeoutMs) ||
    terminalTimeoutMs < 0 ||
    !Number.isFinite(pollMs) ||
    pollMs <= 0
  ) {
    return null
  }

  let elapsedMs = 0
  let result: HandoffResult | null = null

  while (true) {
    result = deps.readResult(hermesHome, { expectedRoot })

    if (result) {break}

    if (elapsedMs >= discoveryTimeoutMs) {return null}
    const delayMs = Math.min(pollMs, discoveryTimeoutMs - elapsedMs)
    await wait(delayMs)
    elapsedMs += delayMs
  }

  const correlation = {
    attemptId: result.attemptId,
    invocationId: result.invocationId,
    leaseId: result.leaseId,
    expectedRoot,
    pollMs,
    terminalTimeoutMs,
    wait
  }

  const consumeTerminal = (timeoutMs: number) =>
    deps.waitForTerminal(hermesHome, {
      attemptId: correlation.attemptId,
      invocationId: correlation.invocationId,
      leaseId: correlation.leaseId,
      expectedRoot: correlation.expectedRoot,
      pollMs: correlation.pollMs,
      timeoutMs,
      wait: correlation.wait
    })

  if (result.state !== 'pending') {return consumeTerminal(0)}

  // Start the terminal watcher immediately. It catches updater failure/timeout
  // while Electron is still proving its backend, instead of serially adding
  // two independent deadline windows.
  const terminalPromise = consumeTerminal(terminalTimeoutMs)
  let terminalSettled = false

  const terminalOutcome = terminalPromise.then(
    terminal => {
      terminalSettled = true

      return { kind: 'terminal' as const, terminal }
    },
    error => {
      terminalSettled = true
      throw error
    }
  )

  const first = await Promise.race([
    terminalOutcome,
    waitForBackendReadiness(
      getBackendReadiness,
      terminalTimeoutMs,
      pollMs,
      wait,
      () => terminalSettled
    ).then(readiness => ({ kind: 'readiness' as const, readiness }))
  ])

  if (first.kind === 'terminal') {return first.terminal}

  if (!first.readiness) {return terminalPromise}

  const expectedBuildId = deps.expectedBuildId(result)

  if (
    result.relaunch.pid === null ||
    result.relaunch.processStartedAt === null ||
    result.relaunch.executable === null ||
    !expectedBuildId
  ) {
    onStatus('pending result lacks exact relaunch or build identity')

    return terminalPromise
  }

  const identity = deps.validateDesktopIdentity({
    currentPid,
    currentProcessStartedAt,
    expectedPid: result.relaunch.pid,
    expectedProcessStartedAt: result.relaunch.processStartedAt,
    execPath: currentExecutable,
    expectedExecutable: result.relaunch.executable,
    expectedRoot: result.root,
    resourcesPath
  })

  const build = identity ? deps.readBuildProof(resourcesPath, expectedBuildId) : null

  if (!identity || !build) {
    onStatus('relaunch process or packaged build identity could not be proven')

    return terminalPromise
  }

  const ack = deps.writeAck(hermesHome, result, {
    currentPid,
    processStartedAt: result.relaunch.processStartedAt,
    currentRoot: identity.root,
    currentExecutable: identity.executable,
    buildId: build.buildId,
    buildSource: build.buildSource,
    backendReady: true,
    backendMode: first.readiness.backendMode
  })

  onStatus(
    ack
      ? `authenticated ${first.readiness.backendMode} backend readiness acknowledged`
      : 'correlated relaunch acknowledgement could not be published'
  )

  return terminalPromise
}
