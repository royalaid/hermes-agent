/** Strict fixed-path v2 consumer and attempt-scoped Desktop ACK publisher. */

import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { TextDecoder } from 'node:util'

import {
  hasExactKeys,
  hasHandoffCapabilitySyntax,
  resolveCanonicalAbsolutePath,
  sameCanonicalPath
} from './handoff-wire-validation'

export const HANDOFF_RESULT_MAX_AGE_MS = 30 * 60 * 1000
export const HANDOFF_RESULT_CLOCK_SKEW_MS = 5 * 1000
// The updater publishes its receipt before a bounded 30-minute Desktop
// rebuild, five-minute gateway recovery, and one-minute single-instance
// handoff. Keep that protocol relation separate from result freshness and
// leave four minutes for stage-boundary and process-start coordination.
export const HANDOFF_RECEIPT_TO_RELAUNCH_MAX_AGE_MS = 40 * 60 * 1000

const GIT_SHA_PATTERN = /^[0-9a-fA-F]{40}$/
const ARCHIVE_SHA_PATTERN = /^[0-9a-fA-F]{64}$/

const RESULT_KEYS = [
  'schema_version',
  'attempt_id',
  'state',
  'ok',
  'exit_code',
  'message',
  'branch',
  'invocation_id',
  'lease_id',
  'root',
  'receipt',
  'cleanup',
  'runtime_health',
  'relaunch',
  'desktop',
  'finished_at'
]

const RECEIPT_KEYS = [
  'schema_version',
  'invocation_id',
  'lease_id',
  'mode',
  'root',
  'remote',
  'branch',
  'target_ref',
  'target_sha',
  'resulting_head',
  'archive_sha',
  'timestamp',
  'success',
  'gateway_resume_deferred',
  'health'
]

const HEALTH_KEYS = ['critical_syntax', 'critical_imports', 'dependencies', 'node_dependencies']
const CLEANUP_KEYS = ['update_marker_released', 'bridge_lease_released']
const RELAUNCH_KEYS = ['state', 'pid', 'process_started_at', 'executable', 'requested_at', 'acknowledged_at']
const DESKTOP_KEYS = ['build_id', 'build_source', 'root', 'backend_ready', 'backend_mode']
const LEGACY_RESULT_KEYS = ['ok', 'exit_code', 'message', 'branch', 'finished_at']
const POSIX_RESULT_KEYS = ['ok', 'exit_code', 'manual', 'message', 'branch', 'finished_at']

export interface RuntimeHealth {
  criticalSyntax: true
  criticalImports: true
  dependencies: true
  nodeDependencies: true
}

export interface HandoffReceipt {
  schemaVersion: 1
  invocationId: string
  leaseId: string
  mode: 'archive' | 'git'
  root: string
  remote: string | null
  branch: string
  targetRef: string | null
  targetSha: string | null
  resultingHead: string | null
  archiveSha: string | null
  timestamp: number
  success: true
  gatewayResumeDeferred: true
  health: RuntimeHealth
}

export interface HandoffCleanup {
  updateMarkerReleased: boolean
  bridgeLeaseReleased: boolean
}

export interface HandoffRelaunch {
  state: 'acknowledged' | 'failed' | 'pending'
  pid: number | null
  processStartedAt: number | null
  executable: string | null
  requestedAt: number
  acknowledgedAt: number | null
}

export interface HandoffDesktopProof {
  buildId: string | null
  buildSource: 'install-stamp' | null
  root: string | null
  backendReady: boolean
  backendMode: 'local' | 'remote' | null
}

export interface HandoffResult {
  schemaVersion: 2
  attemptId: string
  state: 'complete' | 'failed' | 'pending'
  ok: boolean
  exitCode: number | null
  message: string
  branch: string
  invocationId: string | null
  leaseId: string | null
  root: string
  receipt: HandoffReceipt | null
  cleanup: HandoffCleanup
  runtimeHealth: RuntimeHealth | null
  relaunch: HandoffRelaunch
  desktop: HandoffDesktopProof
  finishedAt: number | null
}

export interface HandoffAck {
  schemaVersion: 1
  attemptId: string
  invocationId: string
  leaseId: string
  pid: number
  processStartedAt: number
  root: string
  executable: string
  buildId: string
  buildSource: 'install-stamp'
  backendReady: true
  backendMode: 'local' | 'remote'
  acknowledgedAt: number
  error: null
}

export interface ReadHandoffResultOptions {
  expectedRoot?: string
  maxAgeMs?: number
  now?: () => number
}

export interface HandoffAckProof {
  currentPid: number
  processStartedAt: number
  currentRoot: string
  currentExecutable: string
  buildId: string
  buildSource: 'install-stamp'
  backendReady: true
  backendMode: 'local' | 'remote'
  now?: () => number
}

export interface WaitForTerminalHandoffResultOptions extends ReadHandoffResultOptions {
  attemptId?: string
  invocationId?: string | null
  leaseId?: string | null
  pollMs?: number
  timeoutMs?: number
  wait?: (delayMs: number) => Promise<void>
}

export interface LegacyHandoffFailureDiagnostic {
  exitCode: number
  message: string
  branch: string
}

/**
 * Exact result emitted by scripts/desktop-update/posix.sh. This record is a
 * compatibility diagnostic only: it is never promoted to authenticated v2
 * Windows transaction success.
 */
export interface PosixHandoffResult {
  ok: boolean
  exitCode: number
  manual: boolean
  message: string
  branch: string
}

export interface ConsumeLegacyHandoffResultOptions {
  maxAgeMs?: number
  now?: () => number
}

interface ResultSnapshot {
  raw: string
  result: HandoffResult
}

interface ResultCorrelation {
  attemptId: string | undefined
  invocationId: string | null | undefined
  leaseId: string | null | undefined
}

interface LegacyHandoffResult {
  ok: boolean
  exitCode: number
  message: string
  branch: string
  finishedAt: number
}

interface ParsedPosixHandoffResult extends PosixHandoffResult {
  finishedAt: number
}

export function handoffResultPath(hermesHome: string): string {
  return path.join(hermesHome, '.hermes-update-result.json')
}

export function handoffAckPath(hermesHome: string, attemptId: string): string {
  if (!hasHandoffCapabilitySyntax(attemptId)) {
    throw new TypeError('invalid update attempt id')
  }

  return path.join(hermesHome, `.hermes-update-ack-${attemptId}.json`)
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0
}

function parseHealth(value: unknown): RuntimeHealth | null {
  if (!hasExactKeys(value, HEALTH_KEYS)) {return null}

  if (
    value.critical_syntax !== true ||
    value.critical_imports !== true ||
    value.dependencies !== true ||
    value.node_dependencies !== true
  ) {
    return null
  }

  return {
    criticalSyntax: true,
    criticalImports: true,
    dependencies: true,
    nodeDependencies: true
  }
}

function parseReceipt(
  value: unknown,
  branch: string,
  root: string,
  requestedAt: number
): HandoffReceipt | null {
  if (!hasExactKeys(value, RECEIPT_KEYS)) {return null}

  if (
    value.schema_version !== 1 ||
    typeof value.invocation_id !== 'string' ||
    !hasHandoffCapabilitySyntax(value.invocation_id) ||
    typeof value.lease_id !== 'string' ||
    !hasHandoffCapabilitySyntax(value.lease_id) ||
    value.branch !== branch ||
    value.success !== true ||
    value.gateway_resume_deferred !== true ||
    !isPositiveInteger(value.timestamp)
  ) {
    return null
  }

  const receiptRoot = resolveCanonicalAbsolutePath(value.root)
  const health = parseHealth(value.health)

  if (!receiptRoot || !sameCanonicalPath(receiptRoot, root) || !health) {
    return null
  }

  if (
    value.timestamp > requestedAt + HANDOFF_RESULT_CLOCK_SKEW_MS / 1_000 ||
    requestedAt - value.timestamp > HANDOFF_RECEIPT_TO_RELAUNCH_MAX_AGE_MS / 1_000
  ) {
    return null
  }

  if (value.mode === 'git') {
    if (
      typeof value.remote !== 'string' ||
      value.remote.trim().length === 0 ||
      value.target_ref !== `refs/remotes/${value.remote}/${branch}` ||
      typeof value.target_sha !== 'string' ||
      !GIT_SHA_PATTERN.test(value.target_sha) ||
      typeof value.resulting_head !== 'string' ||
      !GIT_SHA_PATTERN.test(value.resulting_head) ||
      value.target_sha.toLowerCase() !== value.resulting_head.toLowerCase() ||
      value.archive_sha !== null
    ) {
      return null
    }
  } else if (value.mode === 'archive') {
    if (
      value.remote !== null ||
      value.target_ref !== null ||
      value.target_sha !== null ||
      value.resulting_head !== null ||
      typeof value.archive_sha !== 'string' ||
      !ARCHIVE_SHA_PATTERN.test(value.archive_sha)
    ) {
      return null
    }
  } else {
    return null
  }

  return {
    schemaVersion: 1,
    invocationId: value.invocation_id,
    leaseId: value.lease_id,
    mode: value.mode,
    root: receiptRoot,
    remote: value.remote as string | null,
    branch,
    targetRef: value.target_ref as string | null,
    targetSha: value.target_sha as string | null,
    resultingHead: value.resulting_head as string | null,
    archiveSha: value.archive_sha as string | null,
    timestamp: value.timestamp,
    success: true,
    gatewayResumeDeferred: true,
    health
  }
}

function parseCleanup(value: unknown): HandoffCleanup | null {
  if (!hasExactKeys(value, CLEANUP_KEYS)) {return null}

  if (typeof value.update_marker_released !== 'boolean' || typeof value.bridge_lease_released !== 'boolean') {
    return null
  }

  return {
    updateMarkerReleased: value.update_marker_released,
    bridgeLeaseReleased: value.bridge_lease_released
  }
}

function parseRelaunch(value: unknown): HandoffRelaunch | null {
  if (!hasExactKeys(value, RELAUNCH_KEYS)) {return null}

  if (!['pending', 'acknowledged', 'failed'].includes(String(value.state)) || !isPositiveInteger(value.requested_at)) {
    return null
  }

  const pid = value.pid === null ? null : isPositiveInteger(value.pid) ? value.pid : undefined

  const processStartedAt =
    value.process_started_at === null
      ? null
      : isPositiveInteger(value.process_started_at)
        ? value.process_started_at
        : undefined

  let executable: string | null | undefined

  if (value.executable === null) {
    executable = null
  } else {
    executable = resolveCanonicalAbsolutePath(value.executable) ?? undefined
  }

  const acknowledgedAt =
    value.acknowledged_at === null ? null : isPositiveInteger(value.acknowledged_at) ? value.acknowledged_at : undefined

  if (pid === undefined || processStartedAt === undefined || executable === undefined || acknowledgedAt === undefined) {
    return null
  }

  const identityCount = [pid, processStartedAt, executable].filter(item => item !== null).length

  if (identityCount !== 0 && identityCount !== 3) {
    return null
  }

  if (processStartedAt !== null && processStartedAt > value.requested_at + HANDOFF_RESULT_CLOCK_SKEW_MS / 1_000) {
    return null
  }

  if (acknowledgedAt !== null && acknowledgedAt < value.requested_at) {
    return null
  }

  return {
    state: value.state as HandoffRelaunch['state'],
    pid,
    processStartedAt,
    executable,
    requestedAt: value.requested_at,
    acknowledgedAt
  }
}

function parseDesktop(value: unknown): HandoffDesktopProof | null {
  if (!hasExactKeys(value, DESKTOP_KEYS) || typeof value.backend_ready !== 'boolean') {return null}

  if (!(
    value.build_id === null ||
    (typeof value.build_id === 'string' &&
      (GIT_SHA_PATTERN.test(value.build_id) || ARCHIVE_SHA_PATTERN.test(value.build_id)))
  )) {
    return null
  }

  if (!(value.build_source === null || value.build_source === 'install-stamp')) {
    return null
  }

  if (!(value.backend_mode === null || value.backend_mode === 'local' || value.backend_mode === 'remote')) {
    return null
  }

  let root: string | null

  if (value.root === null) {
    root = null
  } else {
    const canonical = resolveCanonicalAbsolutePath(value.root)

    if (!canonical) {
      return null
    }
    root = canonical
  }

  const buildId = value.build_id as string | null
  const buildSource = value.build_source as 'install-stamp' | null
  const backendMode = value.backend_mode as 'local' | 'remote' | null

  return {
    buildId,
    buildSource,
    root,
    backendReady: value.backend_ready,
    backendMode
  }
}

function isEmptyDesktopProof(desktop: HandoffDesktopProof): boolean {
  return (
    desktop.buildId === null &&
    desktop.buildSource === null &&
    desktop.root === null &&
    desktop.backendReady === false &&
    desktop.backendMode === null
  )
}

function isCorrelatedDesktopObservation(
  desktop: HandoffDesktopProof,
  root: string,
  expectedBuildId: string | null,
  receiptPresent: boolean,
  relaunchIdentityPresent: boolean
): boolean {
  if (isEmptyDesktopProof(desktop)) {
    return true
  }

  return Boolean(
    receiptPresent &&
    relaunchIdentityPresent &&
    expectedBuildId &&
    desktop.buildId &&
    desktop.buildId.toLowerCase() === expectedBuildId.toLowerCase() &&
    desktop.buildSource === 'install-stamp' &&
    desktop.root &&
    sameCanonicalPath(desktop.root, root) &&
    (desktop.backendMode === 'local' || desktop.backendMode === 'remote')
  )
}

export function expectedHandoffBuildId(result: HandoffResult): string | null {
  if (!result.receipt) {
    return null
  }

  return result.receipt.mode === 'git' ? result.receipt.resultingHead : result.receipt.archiveSha
}

function parseHandoffResultValue(
  value: unknown,
  { expectedRoot, now = Date.now, maxAgeMs = HANDOFF_RESULT_MAX_AGE_MS }: ReadHandoffResultOptions = {}
): HandoffResult | null {
  if (!hasExactKeys(value, RESULT_KEYS)) {return null}

  if (
    value.schema_version !== 2 ||
    typeof value.attempt_id !== 'string' ||
    !hasHandoffCapabilitySyntax(value.attempt_id) ||
    !['pending', 'complete', 'failed'].includes(String(value.state)) ||
    typeof value.ok !== 'boolean' ||
    !(value.exit_code === null || Number.isInteger(value.exit_code)) ||
    typeof value.message !== 'string' ||
    value.message.trim().length === 0 ||
    typeof value.branch !== 'string' ||
    value.branch.trim().length === 0 ||
    !(value.finished_at === null || isPositiveInteger(value.finished_at)) ||
    !Number.isFinite(maxAgeMs) ||
    maxAgeMs < 0
  ) {
    return null
  }

  const nowMs = now()

  if (!Number.isFinite(nowMs)) {
    return null
  }

  const root = resolveCanonicalAbsolutePath(value.root)

  if (!root) {
    return null
  }

  if (expectedRoot !== undefined) {
    const canonicalExpectedRoot = resolveCanonicalAbsolutePath(expectedRoot)

    if (!canonicalExpectedRoot || !sameCanonicalPath(root, canonicalExpectedRoot)) {
      return null
    }
  }

  const cleanup = parseCleanup(value.cleanup)
  const relaunch = parseRelaunch(value.relaunch)
  const desktop = parseDesktop(value.desktop)

  if (!cleanup || !relaunch || !desktop) {
    return null
  }

  const referenceSeconds = value.state === 'pending' ? relaunch.requestedAt : value.finished_at

  if (!isPositiveInteger(referenceSeconds)) {
    return null
  }
  const ageMs = nowMs - referenceSeconds * 1_000

  if (ageMs < -HANDOFF_RESULT_CLOCK_SKEW_MS || ageMs > maxAgeMs) {
    return null
  }

  let receipt: HandoffReceipt | null = null
  let runtimeHealth: RuntimeHealth | null = null
  let invocationId: string | null = null
  let leaseId: string | null = null

  if (value.receipt === null) {
    if (value.invocation_id !== null || value.lease_id !== null || value.runtime_health !== null) {
      return null
    }
  } else {
    receipt = parseReceipt(value.receipt, value.branch, root, relaunch.requestedAt)
    runtimeHealth = parseHealth(value.runtime_health)

    if (
      !receipt ||
      !runtimeHealth ||
      value.invocation_id !== receipt.invocationId ||
      value.lease_id !== receipt.leaseId
    ) {
      return null
    }

    invocationId = receipt.invocationId
    leaseId = receipt.leaseId
  }

  const identityPresent = relaunch.pid !== null && relaunch.processStartedAt !== null && relaunch.executable !== null
  const identityAbsent = relaunch.pid === null && relaunch.processStartedAt === null && relaunch.executable === null

  if (!identityPresent && !identityAbsent) {
    return null
  }

  if (value.state === 'pending') {
    if (
      value.ok !== false ||
      value.exit_code !== null ||
      value.finished_at !== null ||
      !receipt ||
      !runtimeHealth ||
      cleanup.updateMarkerReleased !== true ||
      cleanup.bridgeLeaseReleased !== true ||
      relaunch.state !== 'pending' ||
      !identityPresent ||
      relaunch.acknowledgedAt !== null ||
      !isEmptyDesktopProof(desktop)
    ) {
      return null
    }
  } else if (value.state === 'complete') {
    const expectedBuildId = receipt?.mode === 'git' ? receipt.resultingHead : receipt?.archiveSha

    if (
      value.ok !== true ||
      value.exit_code !== 0 ||
      !isPositiveInteger(value.finished_at) ||
      !receipt ||
      !runtimeHealth ||
      cleanup.updateMarkerReleased !== true ||
      cleanup.bridgeLeaseReleased !== true ||
      relaunch.state !== 'acknowledged' ||
      !identityPresent ||
      !isPositiveInteger(relaunch.acknowledgedAt) ||
      relaunch.acknowledgedAt > value.finished_at ||
      !expectedBuildId ||
      desktop.buildId === null ||
      desktop.buildId.toLowerCase() !== expectedBuildId.toLowerCase() ||
      desktop.buildSource !== 'install-stamp' ||
      desktop.root === null ||
      !sameCanonicalPath(desktop.root, root) ||
      desktop.backendReady !== true ||
      (desktop.backendMode !== 'local' && desktop.backendMode !== 'remote')
    ) {
      return null
    }
  } else {
    const expectedBuildId = receipt?.mode === 'git' ? receipt.resultingHead : (receipt?.archiveSha ?? null)

    if (
      value.ok !== false ||
      !Number.isInteger(value.exit_code) ||
      value.exit_code === 0 ||
      !isPositiveInteger(value.finished_at) ||
      value.finished_at < relaunch.requestedAt ||
      relaunch.state !== 'failed' ||
      relaunch.acknowledgedAt !== null ||
      !isCorrelatedDesktopObservation(desktop, root, expectedBuildId, Boolean(receipt), identityPresent)
    ) {
      return null
    }
  }

  return {
    schemaVersion: 2,
    attemptId: value.attempt_id,
    state: value.state as HandoffResult['state'],
    ok: value.ok,
    exitCode: value.exit_code as number | null,
    message: value.message,
    branch: value.branch,
    invocationId,
    leaseId,
    root,
    receipt,
    cleanup,
    runtimeHealth,
    relaunch,
    desktop,
    finishedAt: value.finished_at as number | null
  }
}

function readResultSnapshot(file: string, options: ReadHandoffResultOptions): ResultSnapshot | null {
  let raw: string

  try {
    raw = fs.readFileSync(file, 'utf8')
  } catch {
    return null
  }

  let value: unknown

  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }

  const result = parseHandoffResultValue(value, options)

  return result ? { raw, result } : null
}

export function readHandoffResult(hermesHome: string, options: ReadHandoffResultOptions = {}): HandoffResult | null {
  return readResultSnapshot(handoffResultPath(hermesHome), options)?.result ?? null
}

function parseLegacyHandoffResult(raw: Buffer): LegacyHandoffResult | null {
  let decoded: string

  try {
    decoded = new TextDecoder('utf-8', { fatal: true }).decode(raw)
  } catch {
    return null
  }

  let value: unknown

  try {
    value = JSON.parse(decoded)
  } catch {
    return null
  }

  if (!hasExactKeys(value, LEGACY_RESULT_KEYS)) {
    return null
  }

  if (
    typeof value.ok !== 'boolean' ||
    !Number.isSafeInteger(value.exit_code) ||
    typeof value.message !== 'string' ||
    typeof value.branch !== 'string' ||
    !Number.isSafeInteger(value.finished_at) ||
    (value.finished_at as number) <= 0 ||
    (value.ok ? value.exit_code !== 0 : value.exit_code === 0)
  ) {
    return null
  }

  return {
    ok: value.ok,
    exitCode: value.exit_code as number,
    message: value.message,
    branch: value.branch,
    finishedAt: value.finished_at as number
  }
}

function parsePosixHandoffResult(raw: Buffer): ParsedPosixHandoffResult | null {
  let decoded: string

  try {
    decoded = new TextDecoder('utf-8', { fatal: true }).decode(raw)
  } catch {
    return null
  }

  let value: unknown

  try {
    value = JSON.parse(decoded)
  } catch {
    return null
  }

  if (!hasExactKeys(value, POSIX_RESULT_KEYS)) {
    return null
  }

  if (
    typeof value.ok !== 'boolean' ||
    !Number.isSafeInteger(value.exit_code) ||
    typeof value.manual !== 'boolean' ||
    typeof value.message !== 'string' ||
    typeof value.branch !== 'string' ||
    !Number.isSafeInteger(value.finished_at) ||
    (value.finished_at as number) <= 0 ||
    (value.ok ? value.exit_code !== 0 : value.exit_code === 0) ||
    (value.manual && !value.ok)
  ) {
    return null
  }

  return {
    ok: value.ok,
    exitCode: value.exit_code as number,
    manual: value.manual,
    message: value.message,
    branch: value.branch,
    finishedAt: value.finished_at as number
  }
}

/**
 * Consume one exact six-key POSIX result using the same rename/byte-compare
 * one-shot protocol as v2. Unknown or raced bytes remain available to a newer
 * consumer. Manual outcomes do not expire, but future-dated records never
 * surface outside the bounded clock-skew allowance.
 */
export function consumePosixHandoffResult(
  hermesHome: string,
  { maxAgeMs = HANDOFF_RESULT_MAX_AGE_MS, now = Date.now }: ConsumeLegacyHandoffResultOptions = {}
): PosixHandoffResult | null {
  const nowMs = now()

  if (!Number.isFinite(nowMs) || !Number.isFinite(maxAgeMs) || maxAgeMs < 0) {
    return null
  }

  const file = handoffResultPath(hermesHome)
  let preview: Buffer

  try {
    preview = fs.readFileSync(file)
  } catch {
    return null
  }

  const parsed = parsePosixHandoffResult(preview)

  if (!parsed) {
    return null
  }

  const isolated = `${file}.consume-${process.pid}-${randomUUID()}`

  try {
    fs.renameSync(file, isolated)
  } catch {
    return null
  }

  let isolatedRaw: Buffer

  try {
    isolatedRaw = fs.readFileSync(isolated)
  } catch {
    restoreIsolatedResult(isolated, file)

    return null
  }

  if (!isolatedRaw.equals(preview)) {
    restoreIsolatedResult(isolated, file)

    return null
  }

  try {
    fs.unlinkSync(isolated)
  } catch {
    // The recognized record is detached from the fixed path. A delayed delete
    // of the unique one-shot name cannot replay it.
  }

  const ageMs = nowMs - parsed.finishedAt * 1_000
  const notFromFuture = ageMs >= -HANDOFF_RESULT_CLOCK_SKEW_MS
  const fresh = notFromFuture && ageMs <= maxAgeMs

  if (!notFromFuture || (!parsed.manual && !fresh)) {
    return null
  }

  return {
    ok: parsed.ok,
    exitCode: parsed.exitCode,
    manual: parsed.manual,
    message: parsed.message,
    branch: parsed.branch
  }
}

/**
 * Retire the exact five-key result written by Desktop builds before the v2
 * transaction existed. Only a fresh strict failure may surface as a legacy
 * diagnostic; a legacy success is deliberately never promoted to v2 success.
 * Unrecognized bytes remain at the fixed path for a newer consumer.
 */
export function consumeLegacyHandoffResult(
  hermesHome: string,
  { maxAgeMs = HANDOFF_RESULT_MAX_AGE_MS, now = Date.now }: ConsumeLegacyHandoffResultOptions = {}
): LegacyHandoffFailureDiagnostic | null {
  const nowMs = now()

  if (!Number.isFinite(nowMs) || !Number.isFinite(maxAgeMs) || maxAgeMs < 0) {
    return null
  }

  const file = handoffResultPath(hermesHome)
  let preview: Buffer

  try {
    preview = fs.readFileSync(file)
  } catch {
    return null
  }

  const legacy = parseLegacyHandoffResult(preview)

  if (!legacy) {
    return null
  }

  const isolated = `${file}.consume-${process.pid}-${randomUUID()}`

  try {
    fs.renameSync(file, isolated)
  } catch {
    return null
  }

  let isolatedRaw: Buffer

  try {
    isolatedRaw = fs.readFileSync(isolated)
  } catch {
    restoreIsolatedResult(isolated, file)

    return null
  }

  if (!isolatedRaw.equals(preview)) {
    restoreIsolatedResult(isolated, file)

    return null
  }

  try {
    fs.unlinkSync(isolated)
  } catch {
    // The recognized record is already detached from the fixed path. Antivirus
    // may delay cleanup of the unique one-shot name without risking a replay.
  }

  const ageMs = nowMs - legacy.finishedAt * 1_000
  const fresh = ageMs >= -HANDOFF_RESULT_CLOCK_SKEW_MS && ageMs <= maxAgeMs

  if (legacy.ok || !fresh) {
    return null
  }

  return {
    exitCode: legacy.exitCode,
    message: legacy.message,
    branch: legacy.branch
  }
}

function writeAtomicExclusive(file: string, contents: string): boolean {
  const temporary = `${file}.ack-tmp-${process.pid}-${randomUUID()}`
  let descriptor: number | null = null

  try {
    descriptor = fs.openSync(temporary, 'wx', 0o600)
    fs.writeFileSync(descriptor, contents, 'utf8')
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null

    // A hard-link publish is both atomic and exclusive: the final name is
    // attached only after the complete temp inode is durable, and EEXIST can
    // never replace an acknowledgement another process already published.
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

export function writeHandoffAck(
  hermesHome: string,
  pending: HandoffResult,
  {
    currentPid,
    processStartedAt,
    currentRoot,
    currentExecutable,
    buildId,
    buildSource,
    backendReady,
    backendMode,
    now = Date.now
  }: HandoffAckProof
): HandoffAck | null {
  if (
    pending.state !== 'pending' ||
    pending.ok !== false ||
    !pending.receipt ||
    !pending.invocationId ||
    !pending.leaseId ||
    pending.relaunch.state !== 'pending' ||
    pending.relaunch.pid === null ||
    pending.relaunch.processStartedAt === null ||
    pending.relaunch.executable === null ||
    pending.relaunch.acknowledgedAt !== null ||
    currentPid !== pending.relaunch.pid ||
    processStartedAt !== pending.relaunch.processStartedAt ||
    buildSource !== 'install-stamp' ||
    backendReady !== true ||
    (backendMode !== 'local' && backendMode !== 'remote')
  ) {
    return null
  }

  const root = resolveCanonicalAbsolutePath(currentRoot)
  const executable = resolveCanonicalAbsolutePath(currentExecutable)
  const expectedBuildId = expectedHandoffBuildId(pending)

  if (
    !root ||
    !sameCanonicalPath(root, pending.root) ||
    !executable ||
    !sameCanonicalPath(executable, pending.relaunch.executable) ||
    !expectedBuildId ||
    typeof buildId !== 'string' ||
    buildId.toLowerCase() !== expectedBuildId.toLowerCase()
  ) {
    return null
  }

  const nowMs = now()
  const acknowledgedAt = Math.floor(nowMs / 1_000)

  if (!Number.isFinite(nowMs) || !isPositiveInteger(acknowledgedAt) || acknowledgedAt < pending.relaunch.requestedAt) {
    return null
  }

  const ack: HandoffAck = {
    schemaVersion: 1,
    attemptId: pending.attemptId,
    invocationId: pending.invocationId,
    leaseId: pending.leaseId,
    pid: pending.relaunch.pid,
    processStartedAt: pending.relaunch.processStartedAt,
    root,
    executable,
    buildId,
    buildSource: 'install-stamp',
    backendReady: true,
    backendMode,
    acknowledgedAt,
    error: null
  }

  const wire = {
    schema_version: ack.schemaVersion,
    attempt_id: ack.attemptId,
    invocation_id: ack.invocationId,
    lease_id: ack.leaseId,
    pid: ack.pid,
    process_started_at: ack.processStartedAt,
    root: ack.root,
    executable: ack.executable,
    build_id: ack.buildId,
    build_source: ack.buildSource,
    backend_ready: ack.backendReady,
    backend_mode: ack.backendMode,
    acknowledged_at: ack.acknowledgedAt,
    error: null
  }

  let file: string

  try {
    file = handoffAckPath(hermesHome, pending.attemptId)
  } catch {
    return null
  }

  return writeAtomicExclusive(file, JSON.stringify(wire)) ? ack : null
}

function matchesCorrelation(result: HandoffResult, correlation: ResultCorrelation): boolean {
  return (
    (correlation.attemptId === undefined || result.attemptId === correlation.attemptId) &&
    (correlation.invocationId === undefined || result.invocationId === correlation.invocationId) &&
    (correlation.leaseId === undefined || result.leaseId === correlation.leaseId)
  )
}

function restoreIsolatedResult(isolated: string, file: string): void {
  try {
    fs.linkSync(isolated, file)
    fs.unlinkSync(isolated)
  } catch {
    // If a producer already replaced the fixed path, retain the isolated file
    // rather than deleting an unconsumed record from a racing attempt.
  }
}

function consumeTerminalSnapshot(
  file: string,
  snapshot: ResultSnapshot,
  options: ReadHandoffResultOptions,
  correlation: ResultCorrelation
): HandoffResult | null {
  const isolated = `${file}.consume-${process.pid}-${randomUUID()}`

  try {
    fs.renameSync(file, isolated)
  } catch {
    return null
  }

  let isolatedRaw: string

  try {
    isolatedRaw = fs.readFileSync(isolated, 'utf8')
  } catch {
    restoreIsolatedResult(isolated, file)

    return null
  }

  let isolatedValue: unknown

  try {
    isolatedValue = JSON.parse(isolatedRaw)
  } catch {
    restoreIsolatedResult(isolated, file)

    return null
  }

  const result = parseHandoffResultValue(isolatedValue, options)

  if (
    isolatedRaw !== snapshot.raw ||
    !result ||
    result.state === 'pending' ||
    !matchesCorrelation(result, correlation)
  ) {
    restoreIsolatedResult(isolated, file)

    return null
  }

  try {
    fs.unlinkSync(isolated)
  } catch {
    // The fixed path is already detached; returning the terminal record is
    // safe even if antivirus delays cleanup of its isolated one-shot name.
  }

  return result
}

export async function waitForTerminalHandoffResult(
  hermesHome: string,
  {
    attemptId,
    invocationId,
    leaseId,
    pollMs = 200,
    timeoutMs = 10_000,
    wait = delayMs => new Promise(resolve => setTimeout(resolve, delayMs)),
    ...readOptions
  }: WaitForTerminalHandoffResultOptions = {}
): Promise<HandoffResult | null> {
  if (
    !Number.isFinite(pollMs) ||
    pollMs <= 0 ||
    !Number.isFinite(timeoutMs) ||
    timeoutMs < 0 ||
    (attemptId !== undefined && !hasHandoffCapabilitySyntax(attemptId)) ||
    (typeof invocationId === 'string' && !hasHandoffCapabilitySyntax(invocationId)) ||
    (typeof leaseId === 'string' && !hasHandoffCapabilitySyntax(leaseId))
  ) {
    return null
  }

  const file = handoffResultPath(hermesHome)
  const correlation: ResultCorrelation = { attemptId, invocationId, leaseId }
  let elapsedMs = 0

  while (true) {
    const snapshot = readResultSnapshot(file, readOptions)

    if (snapshot && matchesCorrelation(snapshot.result, correlation)) {
      if (correlation.attemptId === undefined) {
        correlation.attemptId = snapshot.result.attemptId
      }

      if (correlation.invocationId === undefined) {
        correlation.invocationId = snapshot.result.invocationId
      }

      if (correlation.leaseId === undefined) {
        correlation.leaseId = snapshot.result.leaseId
      }

      if (snapshot.result.state !== 'pending') {
        const terminal = consumeTerminalSnapshot(file, snapshot, readOptions, correlation)

        if (terminal) {
          return terminal
        }
      }
    }

    if (elapsedMs >= timeoutMs) {
      return null
    }
    const delayMs = Math.min(pollMs, timeoutMs - elapsedMs)
    await wait(delayMs)
    elapsedMs += delayMs
  }
}

interface LegacyWaitOptions extends WaitForTerminalHandoffResultOptions {
  currentPid?: number
}

/**
 * Transitional name for the main-process integration while it moves to the
 * v2 ACK orchestration. It is still strict v2 behavior: pending is preserved,
 * currentPid is deliberately not treated as relaunch proof, and only terminal
 * correlated records are consumed.
 */
export function waitForAndConsumeHandoffResult(
  hermesHome: string,
  { currentPid: _untrustedCurrentPid, ...options }: LegacyWaitOptions = {}
): Promise<HandoffResult | null> {
  void _untrustedCurrentPid

  return waitForTerminalHandoffResult(hermesHome, options)
}
