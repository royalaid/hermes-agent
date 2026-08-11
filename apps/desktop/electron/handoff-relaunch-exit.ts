/**
 * Attempt-scoped Desktop single-instance exit protocol.
 *
 * PowerShell owns request publication and cleanup. Each matching Desktop only
 * reads the pinned request and may publish one exclusive quit acknowledgement;
 * it never mutates the request or an acknowledgement another process won.
 */

import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { TextDecoder } from 'node:util'

import { type HandoffResult, readHandoffResult } from './handoff-result'
import { queryWindowsProcessCreatedAt } from './windows-process-identity'

export const HANDOFF_RELAUNCH_REQUEST_MAX_TTL_SECONDS = 120
export const HANDOFF_RELAUNCH_REQUEST_CLOCK_SKEW_MS = 5 * 1_000
export const HANDOFF_RELAUNCH_RESULT_PUBLICATION_GRACE_MS = 5 * 1_000

const CAPABILITY_PATTERN = /^[A-Za-z0-9._-]{16,128}$/
const REQUEST_PREFIX = '.hermes-update-relaunch-request-'
const REQUEST_SUFFIX = '.json'
const REQUEST_MAX_BYTES = 64 * 1024
const REQUEST_KEYS = ['schema_version', 'attempt_id', 'root', 'executable', 'requested_at', 'expires_at']

const REQUEST_RECOVERY_ARTIFACT_PATTERN =
  /^\.hermes-update-relaunch-request-([A-Za-z0-9._-]{16,128})\.json\.cas-(shadow|previous|displaced|release|emergency)-[1-9][0-9]*-(?:[A-Fa-f0-9]{32}|[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12})$/

export interface HandoffRelaunchRequest {
  schemaVersion: 1
  attemptId: string
  root: string
  executable: string
  requestedAt: number
  expiresAt: number
}

export interface HandoffRelaunchExitAck {
  schemaVersion: 1
  attemptId: string
  pid: number
  processStartedAt: number
  root: string
  executable: string
  acknowledgedAt: number
  action: 'quit'
}

export interface HandoffRelaunchAuthorization {
  attemptId: string
  processStartedAt: number
  requestFingerprint: string
}

export type HandoffRelaunchExitBlockedReason =
  | 'request-scan-failed'
  | 'request-unreadable'
  | 'request-malformed'
  | 'request-future'
  | 'request-recovery-artifact'
  | 'multiple-active-requests'
  | 'current-path-unverifiable'
  | 'process-identity-unavailable'
  | 'request-changed'
  | 'ack-publish-failed'

export type HandoffRelaunchExitDecision =
  | { kind: 'none' }
  | { kind: 'wait-for-result'; attemptId: string; retryAtMs: number }
  | {
      kind: 'authorized-relaunch'
      attemptId: string
      resultState: 'pending' | 'complete' | 'cached'
      authorization: HandoffRelaunchAuthorization
    }
  | {
      kind: 'quit-acknowledged'
      attemptId: string
      ack: HandoffRelaunchExitAck
    }
  | { kind: 'blocked'; reason: HandoffRelaunchExitBlockedReason }

export type HandoffRelaunchProcessStartProbe = (
  pid: number
) => number | null | Promise<number | null>

export type HandoffRelaunchResultReader = (
  hermesHome: string,
  request: HandoffRelaunchRequest
) => HandoffResult | null | Promise<HandoffResult | null>

export interface InspectHandoffRelaunchExitOptions {
  authorization?: HandoffRelaunchAuthorization | null
  currentRoot: string
  currentExecutable?: string
  currentPid?: number
  getProcessStartedAt?: HandoffRelaunchProcessStartProbe
  now?: () => number
  readResult?: HandoffRelaunchResultReader
}

export interface HandoffRelaunchRequestGateOptions {
  authorization?: HandoffRelaunchAuthorization | null
  currentRoot: string
  currentExecutable?: string
  now?: () => number
}

interface RequestSnapshot {
  file: string
  raw: Buffer
  request: HandoffRelaunchRequest
}

function requestFingerprint(raw: Buffer): string {
  return createHash('sha256').update(raw).digest('hex')
}

function authorizationFor(
  snapshot: RequestSnapshot,
  processStartedAt: number
): HandoffRelaunchAuthorization {
  return {
    attemptId: snapshot.request.attemptId,
    processStartedAt,
    requestFingerprint: requestFingerprint(snapshot.raw)
  }
}

function authorizationMatches(
  authorization: HandoffRelaunchAuthorization | null | undefined,
  snapshot: RequestSnapshot,
  processStartedAt?: number
): boolean {
  return Boolean(
    authorization &&
      authorization.attemptId === snapshot.request.attemptId &&
      (processStartedAt === undefined || authorization.processStartedAt === processStartedAt) &&
      authorization.requestFingerprint === requestFingerprint(snapshot.raw)
  )
}

interface RequestDirectoryEntries {
  requests: string[]
  recoveryArtifacts: string[]
}

type RequestReadResult =
  | { kind: 'active'; snapshot: RequestSnapshot }
  | { kind: 'expired' }
  | { kind: 'blocked'; reason: HandoffRelaunchExitBlockedReason }

type RequestScanResult =
  | { kind: 'ok'; active: RequestSnapshot[] }
  | { kind: 'blocked'; reason: HandoffRelaunchExitBlockedReason }

export function handoffRelaunchRequestPath(hermesHome: string, attemptId: string): string {
  assertAttemptId(attemptId)

  return path.join(hermesHome, `${REQUEST_PREFIX}${attemptId}${REQUEST_SUFFIX}`)
}

export function handoffRelaunchExitAckPath(
  hermesHome: string,
  attemptId: string,
  pid: number
): string {
  assertAttemptId(attemptId)

  if (!isPositiveInteger(pid)) {
    throw new TypeError('invalid Desktop process id')
  }

  return path.join(hermesHome, `.hermes-update-relaunch-exit-ack-${attemptId}-${pid}.json`)
}

function assertAttemptId(attemptId: string): void {
  if (!CAPABILITY_PATTERN.test(attemptId)) {
    throw new TypeError('invalid update attempt id')
  }
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0
}

function exactKeys(value: unknown, expected: string[]): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()

  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index])
}

function canonicalPath(candidate: unknown): string | null {
  if (typeof candidate !== 'string' || !path.isAbsolute(candidate)) {
    return null
  }

  try {
    return fs.realpathSync.native(candidate)
  } catch {
    return null
  }
}

function pathIdentity(candidate: string): string {
  return process.platform === 'win32' ? candidate.toLowerCase() : candidate
}

function sameCanonicalPath(left: string, right: string): boolean {
  return pathIdentity(left) === pathIdentity(right)
}

function decodeRequest(raw: Buffer): unknown | null {
  if (raw.length === 0 || raw.length > REQUEST_MAX_BYTES) {
    return null
  }

  let decoded: string

  try {
    decoded = new TextDecoder('utf-8', { fatal: true }).decode(raw)
  } catch {
    return null
  }

  try {
    return JSON.parse(decoded)
  } catch {
    return null
  }
}

function parseRequest(
  value: unknown,
  filenameAttemptId: string,
  currentRoot: string,
  currentExecutable: string,
  nowMs: number
): RequestReadResult {
  if (!exactKeys(value, REQUEST_KEYS)) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  if (
    value.schema_version !== 1 ||
    typeof value.attempt_id !== 'string' ||
    value.attempt_id !== filenameAttemptId ||
    !CAPABILITY_PATTERN.test(value.attempt_id) ||
    !isPositiveInteger(value.requested_at) ||
    !isPositiveInteger(value.expires_at)
  ) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  const ttlSeconds = value.expires_at - value.requested_at

  if (ttlSeconds <= 0 || ttlSeconds > HANDOFF_RELAUNCH_REQUEST_MAX_TTL_SECONDS) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  if (value.requested_at * 1_000 - nowMs > HANDOFF_RELAUNCH_REQUEST_CLOCK_SKEW_MS) {
    return { kind: 'blocked', reason: 'request-future' }
  }

  // Expiry is producer-authenticated by the exact schema, attempt capability,
  // integer epochs, and bounded TTL. Check it before filesystem identity: an
  // old packaged executable may already be gone and must not strand startup.
  if (nowMs > value.expires_at * 1_000) {
    return { kind: 'expired' }
  }

  const root = canonicalPath(value.root)
  const executable = canonicalPath(value.executable)

  if (
    !root ||
    !sameCanonicalPath(root, currentRoot) ||
    !executable ||
    !sameCanonicalPath(executable, currentExecutable)
  ) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  return {
    kind: 'active',
    snapshot: {
      file: '',
      raw: Buffer.alloc(0),
      request: {
        schemaVersion: 1,
        attemptId: value.attempt_id,
        root,
        executable,
        requestedAt: value.requested_at,
        expiresAt: value.expires_at
      }
    }
  }
}

function readRequest(
  file: string,
  filenameAttemptId: string,
  currentRoot: string,
  currentExecutable: string,
  nowMs: number
): RequestReadResult {
  let raw: Buffer

  try {
    raw = fs.readFileSync(file)
  } catch {
    return { kind: 'blocked', reason: 'request-unreadable' }
  }

  const value = decodeRequest(raw)

  if (value === null) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  const parsed = parseRequest(value, filenameAttemptId, currentRoot, currentExecutable, nowMs)

  if (parsed.kind !== 'active') {
    return parsed
  }

  return {
    kind: 'active',
    snapshot: { file, raw, request: parsed.snapshot.request }
  }
}

function requestDirectoryEntries(hermesHome: string): RequestDirectoryEntries | null {
  try {
    const names = fs.readdirSync(hermesHome)

    const requests = names
      .filter(name => name.startsWith(REQUEST_PREFIX) && name.endsWith(REQUEST_SUFFIX))
      .sort()

    return {
      requests,
      recoveryArtifacts: names
        .filter(name => REQUEST_RECOVERY_ARTIFACT_PATTERN.test(name))
        .sort()
    }
  } catch (error: any) {
    return error?.code === 'ENOENT' ? { requests: [], recoveryArtifacts: [] } : null
  }
}

function filenameAttemptId(name: string): string | null {
  const attemptId = name.slice(REQUEST_PREFIX.length, -REQUEST_SUFFIX.length)

  return CAPABILITY_PATTERN.test(attemptId) ? attemptId : null
}

function scanRequestNames(
  hermesHome: string,
  names: string[],
  currentRoot: string,
  currentExecutable: string,
  nowMs: number
): RequestScanResult {
  const active: RequestSnapshot[] = []

  for (const name of names) {
    const attemptId = filenameAttemptId(name)

    if (!attemptId) {
      return { kind: 'blocked', reason: 'request-malformed' }
    }

    const candidate = readRequest(path.join(hermesHome, name), attemptId, currentRoot, currentExecutable, nowMs)

    if (candidate.kind === 'blocked') {
      return candidate
    }

    if (candidate.kind === 'active') {
      active.push(candidate.snapshot)
    }
  }

  if (active.length > 1) {
    return { kind: 'blocked', reason: 'multiple-active-requests' }
  }

  return { kind: 'ok', active }
}

/**
 * Synchronous startup gate using the same strict request parser as the async
 * inspector. Expired valid requests do not strand backend startup; malformed,
 * unreadable, future, multiple, and directory-uncertain states fail closed.
 */
export function hasHandoffRelaunchRequest(
  hermesHome: string,
  {
    authorization,
    currentRoot,
    currentExecutable = process.execPath,
    now = Date.now
  }: HandoffRelaunchRequestGateOptions
): boolean {
  const entries = requestDirectoryEntries(hermesHome)

  if (entries === null) {
    return true
  }

  if (entries.recoveryArtifacts.length > 0) {
    return true
  }

  if (entries.requests.length === 0) {
    return false
  }

  const nowMs = now()
  const root = canonicalPath(currentRoot)
  const executable = canonicalPath(currentExecutable)

  if (!Number.isFinite(nowMs) || !root || !executable) {
    return true
  }

  const scan = scanRequestNames(hermesHome, entries.requests, root, executable, nowMs)

  if (scan.kind === 'blocked') {
    return true
  }

  if (scan.active.length === 0) {
    return false
  }

  return !authorizationMatches(authorization, scan.active[0])
}

function exactResultAuthorizes(
  result: HandoffResult | null,
  request: HandoffRelaunchRequest,
  currentPid: number,
  processStartedAt: number,
  currentExecutable: string
): result is HandoffResult & { state: 'pending' | 'complete' } {
  if (!result || (result.state !== 'pending' && result.state !== 'complete')) {
    return false
  }

  if (
    result.attemptId !== request.attemptId ||
    !sameCanonicalPath(result.root, request.root) ||
    result.relaunch.pid !== currentPid ||
    result.relaunch.processStartedAt !== processStartedAt ||
    !result.relaunch.executable
  ) {
    return false
  }

  const executable = canonicalPath(result.relaunch.executable)

  return Boolean(executable && sameCanonicalPath(executable, currentExecutable))
}

function requestIsUnchanged(
  hermesHome: string,
  snapshot: RequestSnapshot,
  currentRoot: string,
  currentExecutable: string,
  nowMs: number
): boolean {
  const entries = requestDirectoryEntries(hermesHome)

  if (!entries || entries.recoveryArtifacts.length > 0) {
    return false
  }

  const scan = scanRequestNames(hermesHome, entries.requests, currentRoot, currentExecutable, nowMs)

  return (
    scan.kind === 'ok' &&
    scan.active.length === 1 &&
    scan.active[0].file === snapshot.file &&
    scan.active[0].raw.equals(snapshot.raw)
  )
}

function publishAckExclusive(file: string, ack: HandoffRelaunchExitAck): boolean {
  const temporary = `${file}.tmp-${process.pid}-${randomUUID()}`

  const wire = {
    schema_version: ack.schemaVersion,
    attempt_id: ack.attemptId,
    pid: ack.pid,
    process_started_at: ack.processStartedAt,
    root: ack.root,
    executable: ack.executable,
    acknowledged_at: ack.acknowledgedAt,
    action: ack.action
  }

  let descriptor: number | null = null

  try {
    descriptor = fs.openSync(temporary, 'wx', 0o600)
    fs.writeFileSync(descriptor, JSON.stringify(wire), 'utf8')
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null
    fs.linkSync(temporary, file)

    return true
  } catch {
    return false
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor)
      } catch {
        void 0
      }
    }

    try {
      fs.unlinkSync(temporary)
    } catch {
      void 0
    }
  }
}

function acknowledgeQuit(
  hermesHome: string,
  snapshot: RequestSnapshot,
  currentPid: number,
  processStartedAt: number,
  currentRoot: string,
  currentExecutable: string,
  nowMs: number
): HandoffRelaunchExitDecision {
  if (!requestIsUnchanged(hermesHome, snapshot, currentRoot, currentExecutable, nowMs)) {
    return { kind: 'blocked', reason: 'request-changed' }
  }

  const ack: HandoffRelaunchExitAck = {
    schemaVersion: 1,
    attemptId: snapshot.request.attemptId,
    pid: currentPid,
    processStartedAt,
    root: currentRoot,
    executable: currentExecutable,
    acknowledgedAt: Math.max(Math.floor(nowMs / 1_000), snapshot.request.requestedAt),
    action: 'quit'
  }

  const file = handoffRelaunchExitAckPath(hermesHome, snapshot.request.attemptId, currentPid)

  if (!publishAckExclusive(file, ack)) {
    return { kind: 'blocked', reason: 'ack-publish-failed' }
  }

  return { kind: 'quit-acknowledged', attemptId: snapshot.request.attemptId, ack }
}

/**
 * Inspect the one active relaunch request and decide whether this exact Desktop
 * is the updater-authorized relaunch or must acknowledge and quit.
 */
export async function inspectHandoffRelaunchExit(
  hermesHome: string,
  {
    authorization,
    currentRoot,
    currentExecutable = process.execPath,
    currentPid = process.pid,
    getProcessStartedAt = queryWindowsProcessCreatedAt,
    now = Date.now,
    readResult
  }: InspectHandoffRelaunchExitOptions
): Promise<HandoffRelaunchExitDecision> {
  const entries = requestDirectoryEntries(hermesHome)

  if (entries === null) {
    return { kind: 'blocked', reason: 'request-scan-failed' }
  }

  if (entries.recoveryArtifacts.length > 0) {
    return { kind: 'blocked', reason: 'request-recovery-artifact' }
  }

  if (entries.requests.length === 0) {
    return { kind: 'none' }
  }

  const nowMs = now()

  if (!Number.isFinite(nowMs)) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  const root = canonicalPath(currentRoot)
  const executable = canonicalPath(currentExecutable)

  if (!root || !executable) {
    return { kind: 'blocked', reason: 'current-path-unverifiable' }
  }

  const scan = scanRequestNames(hermesHome, entries.requests, root, executable, nowMs)

  if (scan.kind === 'blocked') {
    return scan
  }

  if (scan.active.length === 0) {
    return { kind: 'none' }
  }

  if (!isPositiveInteger(currentPid)) {
    return { kind: 'blocked', reason: 'process-identity-unavailable' }
  }

  let processStartedAt: number | null

  try {
    processStartedAt = await getProcessStartedAt(currentPid)
  } catch {
    processStartedAt = null
  }

  if (!isPositiveInteger(processStartedAt)) {
    return { kind: 'blocked', reason: 'process-identity-unavailable' }
  }

  const snapshot = scan.active[0]
  const request = snapshot.request
  let decisionNowMs = now()

  if (!Number.isFinite(decisionNowMs)) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  if (decisionNowMs > request.expiresAt * 1_000) {
    return { kind: 'none' }
  }

  if (authorization && authorizationMatches(authorization, snapshot, processStartedAt)) {
    return {
      kind: 'authorized-relaunch',
      attemptId: request.attemptId,
      resultState: 'cached',
      authorization
    }
  }

  if (processStartedAt < request.requestedAt) {
    return acknowledgeQuit(
      hermesHome,
      snapshot,
      currentPid,
      processStartedAt,
      root,
      executable,
      decisionNowMs
    )
  }

  let result: HandoffResult | null = null

  try {
    result = readResult
      ? await readResult(hermesHome, request)
      : readHandoffResult(hermesHome, { expectedRoot: root, now })
  } catch {
    result = null
  }

  decisionNowMs = now()

  if (!Number.isFinite(decisionNowMs)) {
    return { kind: 'blocked', reason: 'request-malformed' }
  }

  if (decisionNowMs > request.expiresAt * 1_000) {
    return { kind: 'none' }
  }

  if (exactResultAuthorizes(result, request, currentPid, processStartedAt, executable)) {
    return {
      kind: 'authorized-relaunch',
      attemptId: request.attemptId,
      resultState: result.state,
      authorization: authorizationFor(snapshot, processStartedAt)
    }
  }

  // PowerShell publishes the request before draining old Desktop processes,
  // then records a new relaunch requested_at immediately before the eventual
  // spawn. The exact OS creation time is therefore the only safe grace anchor.
  const publicationDeadline = processStartedAt * 1_000 + HANDOFF_RELAUNCH_RESULT_PUBLICATION_GRACE_MS

  if (decisionNowMs < publicationDeadline) {
    return {
      kind: 'wait-for-result',
      attemptId: request.attemptId,
      retryAtMs: publicationDeadline
    }
  }

  return acknowledgeQuit(
    hermesHome,
    snapshot,
    currentPid,
    processStartedAt,
    root,
    executable,
    decisionNowMs
  )
}
