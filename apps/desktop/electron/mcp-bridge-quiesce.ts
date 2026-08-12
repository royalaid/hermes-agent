import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { TextDecoder } from 'node:util'

import { type PidIdentityStatus, probePidIdentity, type ProcessCreateTimeProbe } from './update-marker'
import {
  getCachedWindowsProcessCreatedAt,
  queryWindowsProcessCreatedAt
} from './windows-process-identity'

export const MCP_BRIDGE_QUIESCE_MARKER = '.hermes-venv-quiesce'
export const MCP_BRIDGE_LEASE_SECONDS = 20 * 60
export const MCP_BRIDGE_HANDOFF_GRACE_SECONDS = 90
export const MCP_BRIDGE_MAX_HANDOFF_GRACE_SECONDS = 90
export const MCP_BRIDGE_EMERGENCY_LEASE_SECONDS = 2 * 60
export const MCP_BRIDGE_CLOCK_SKEW_SECONDS = 5

const LEASE_ID_PATTERN = /^[A-Za-z0-9._-]{16,128}$/

const V1_LEASE_KEYS = new Set([
  'schema_version',
  'lease_id',
  'owner_pid',
  'created_at',
  'expires_at',
  'handoff_grace_until',
  'install_root'
])

const CAS_PURPOSES = ['shadow', 'previous', 'displaced', 'release', 'emergency'] as const
const FATAL_UTF8_DECODER = new TextDecoder('utf-8', { fatal: true })
const leaseGenerations = new WeakMap<McpBridgeQuiesceLease, Buffer>()

type CasPurpose = (typeof CAS_PURPOSES)[number]
type MaybeAsyncProcessCreateTimeProbe = (
  pid: number
) => number | null | undefined | Promise<number | null | undefined>

export interface McpBridgeQuiesceLease {
  schemaVersion: 1
  leaseId: string
  ownerPid: number
  createdAt: number
  expiresAt: number
  handoffGraceUntil: number
  installRoot: string
}

interface IdentityDeps {
  getProcessCreatedAt?: ProcessCreateTimeProbe
  isPidAlive?: (pid: number) => boolean
}

interface LeaseDeps extends IdentityDeps {
  now?: () => number
  ownerPid?: number
  randomId?: () => string
}

interface HandoffDeps extends IdentityDeps {
  now?: () => number
}

interface UpdateOwnerClaim {
  pid: number
  startedAt: number
}

interface AdoptionDeps {
  excludedOwnerPids?: number[]
  getProcessCreatedAt?: MaybeAsyncProcessCreateTimeProbe
  isPidAlive?: (pid: number) => boolean
  now?: () => number
  nowMs?: () => number
  pollMs?: number
  readUpdateOwner: () => UpdateOwnerClaim | null
  requiredOwnerPid?: number
  timeoutMs?: number
  wait?: (delayMs: number) => Promise<void>
}

interface StagedHandoffDeps extends Omit<AdoptionDeps, 'requiredOwnerPid' | 'timeoutMs'> {
  handoffTimeoutMs?: number
  requiredOwnerStartedAt: number
  verifyRequiredOwnerGeneration: () => boolean | Promise<boolean>
}

interface RevocationDeps {
  maxAttempts?: number
  now?: () => number
  ownerPid?: number
}

export type McpBridgeQuiesceRevocationResult = 'revoked' | 'unproven'

export type StagedMcpBridgeLeaseHandoff =
  | { kind: 'adopted'; lease: McpBridgeQuiesceLease }
  | { kind: 'failed' }

interface LegacyLease {
  createdAt: number
  ownerPid: number
}

type RawSnapshot =
  | { kind: 'absent' }
  | { kind: 'unreadable' }
  | { kind: 'present'; raw: Buffer }

type RecoveryState = 'clear' | 'active' | 'unreadable'

function epochSeconds(): number {
  return Math.floor(Date.now() / 1000)
}

function errorCode(error: unknown): string | undefined {
  return typeof error === 'object' && error !== null && 'code' in error
    ? String((error as NodeJS.ErrnoException).code)
    : undefined
}

function pidIsAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return false
  }

  try {
    process.kill(pid, 0)

    return true
  } catch (error) {
    return errorCode(error) !== 'ESRCH'
  }
}

function pidAppearsAlive(pid: number, probe?: (pid: number) => boolean): boolean {
  try {
    return (probe ?? pidIsAlive)(pid)
  } catch {
    return true
  }
}

function killFromLiveness(probe: (pid: number) => boolean): typeof process.kill {
  return ((pid: number) => {
    if (probe(pid)) {
      return true
    }

    const error = new Error(`process ${pid} does not exist`) as NodeJS.ErrnoException
    error.code = 'ESRCH'
    throw error
  }) as typeof process.kill
}

function syncPidIdentity(pid: number, createdAt: number, deps: IdentityDeps = {}): PidIdentityStatus {
  // Existing callers use isPidAlive as a complete deterministic identity test
  // double. Production never takes this shortcut: it uses the cached native
  // creation-time probe, whose initial/failed lookup is deliberately unknown.
  if (deps.isPidAlive && deps.getProcessCreatedAt === undefined) {
    try {
      return deps.isPidAlive(pid) ? 'matching' : 'stale'
    } catch {
      return 'unknown'
    }
  }

  return probePidIdentity(pid, createdAt, {
    ...(deps.isPidAlive ? { kill: killFromLiveness(deps.isPidAlive) } : {}),
    getProcessCreatedAt: deps.getProcessCreatedAt ?? getCachedWindowsProcessCreatedAt
  })
}

async function resolveProcessCreatedAt(
  pid: number,
  deps: Pick<AdoptionDeps, 'getProcessCreatedAt'>
): Promise<number | null | undefined> {
  let processCreatedAt: number | null | undefined

  try {
    processCreatedAt = deps.getProcessCreatedAt
      ? await deps.getProcessCreatedAt(pid)
      : await queryWindowsProcessCreatedAt(pid)
  } catch {
    processCreatedAt = null
  }

  return processCreatedAt
}

async function verifyRequiredOwnerGeneration(
  deps: Pick<StagedHandoffDeps, 'verifyRequiredOwnerGeneration'>
): Promise<boolean> {
  try {
    return (await deps.verifyRequiredOwnerGeneration()) === true
  } catch {
    return false
  }
}

function resolvedPidIdentity(
  pid: number,
  createdAt: number,
  processCreatedAt: number | null | undefined,
  deps: Pick<AdoptionDeps, 'isPidAlive'>
): PidIdentityStatus {
  return syncPidIdentity(pid, createdAt, {
    getProcessCreatedAt: () => processCreatedAt,
    isPidAlive: deps.isPidAlive
  })
}

function readUpdateOwnerClaim(read: () => UpdateOwnerClaim | null): UpdateOwnerClaim | null {
  try {
    const claim = read()

    return claim &&
      Number.isSafeInteger(claim.pid) &&
      claim.pid > 0 &&
      Number.isSafeInteger(claim.startedAt) &&
      claim.startedAt > 0
      ? claim
      : null
  } catch {
    return null
  }
}

export function mcpBridgeQuiesceMarkerPath(hermesHome: string): string {
  return path.join(hermesHome, MCP_BRIDGE_QUIESCE_MARKER)
}

function decodeUtf8(raw: Buffer): string | null {
  try {
    return FATAL_UTF8_DECODER.decode(raw)
  } catch {
    return null
  }
}

function parseLease(raw: Buffer): McpBridgeQuiesceLease | null {
  const decoded = decodeUtf8(raw)

  if (decoded === null) {
    return null
  }

  let value: unknown

  try {
    value = JSON.parse(decoded)
  } catch {
    return null
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>
  const keys = Object.keys(record)

  if (keys.length !== V1_LEASE_KEYS.size || keys.some(key => !V1_LEASE_KEYS.has(key))) {
    return null
  }

  const lease: McpBridgeQuiesceLease = {
    schemaVersion: record.schema_version as 1,
    leaseId: record.lease_id as string,
    ownerPid: record.owner_pid as number,
    createdAt: record.created_at as number,
    expiresAt: record.expires_at as number,
    handoffGraceUntil: record.handoff_grace_until as number,
    installRoot: record.install_root as string
  }

  if (
    lease.schemaVersion !== 1 ||
    typeof lease.leaseId !== 'string' ||
    !LEASE_ID_PATTERN.test(lease.leaseId) ||
    !Number.isSafeInteger(lease.ownerPid) ||
    lease.ownerPid <= 0 ||
    !Number.isSafeInteger(lease.createdAt) ||
    !Number.isSafeInteger(lease.expiresAt) ||
    !Number.isSafeInteger(lease.handoffGraceUntil) ||
    lease.createdAt <= 0 ||
    lease.handoffGraceUntil < lease.createdAt ||
    lease.expiresAt < lease.handoffGraceUntil ||
    lease.expiresAt - lease.createdAt > MCP_BRIDGE_LEASE_SECONDS ||
    lease.handoffGraceUntil - lease.createdAt > MCP_BRIDGE_MAX_HANDOFF_GRACE_SECONDS ||
    typeof lease.installRoot !== 'string' ||
    !path.isAbsolute(lease.installRoot)
  ) {
    return null
  }

  return lease
}

function parseLegacyLease(raw: Buffer): LegacyLease | null {
  const decoded = decodeUtf8(raw)

  if (decoded === null) {
    return null
  }

  const lines = decoded.split(/\r?\n/)

  if (lines.at(-1) === '') {
    lines.pop()
  }

  if (lines.length !== 2) {
    return null
  }

  const ownerPid = Number(lines[0])
  const createdAt = Number(lines[1])

  return Number.isSafeInteger(ownerPid) && ownerPid > 0 && Number.isSafeInteger(createdAt) && createdAt > 0
    ? { createdAt, ownerPid }
    : null
}

function serializeLease(lease: McpBridgeQuiesceLease): Buffer {
  return Buffer.from(
    `${JSON.stringify({
      schema_version: lease.schemaVersion,
      lease_id: lease.leaseId,
      owner_pid: lease.ownerPid,
      created_at: lease.createdAt,
      expires_at: lease.expiresAt,
      handoff_grace_until: lease.handoffGraceUntil,
      install_root: lease.installRoot
    })}\n`,
    'utf8'
  )
}

function bindLeaseGeneration(lease: McpBridgeQuiesceLease, raw: Buffer): McpBridgeQuiesceLease {
  leaseGenerations.set(lease, Buffer.from(raw))

  return lease
}

function leaseGeneration(lease: McpBridgeQuiesceLease): Buffer | null {
  return leaseGenerations.get(lease) ?? null
}

function readRaw(file: string): RawSnapshot {
  try {
    return { kind: 'present', raw: fs.readFileSync(file) }
  } catch (error) {
    return errorCode(error) === 'ENOENT' ? { kind: 'absent' } : { kind: 'unreadable' }
  }
}

function uniqueCasSibling(marker: string, purpose: CasPurpose): string {
  return `${marker}.cas-${purpose}-${process.pid}-${randomUUID()}`
}

function uniquePendingSibling(marker: string): string {
  return path.join(path.dirname(marker), `.hermes-lease-pending-${process.pid}-${randomUUID()}`)
}

function casPurpose(marker: string, candidate: string): CasPurpose | null {
  const prefix = `${path.basename(marker)}.cas-`
  const name = path.basename(candidate)

  if (!name.startsWith(prefix)) {
    return null
  }

  const match = /^(shadow|previous|displaced|release|emergency)-[1-9][0-9]*-[A-Za-z0-9._-]+$/.exec(
    name.slice(prefix.length)
  )

  return match ? (match[1] as CasPurpose) : null
}

function listCasArtifacts(marker: string): string[] | null {
  try {
    const prefix = `${path.basename(marker)}.cas-`

    return fs
      .readdirSync(path.dirname(marker))
      .filter(name => name.startsWith(prefix))
      .map(name => path.join(path.dirname(marker), name))
  } catch {
    return null
  }
}

function removeOwnedPending(file: string): void {
  try {
    fs.unlinkSync(file)
  } catch {
    void 0
  }
}

function writeCompleteUnpublished(file: string, raw: Buffer): void {
  let descriptor: number | null = null

  try {
    descriptor = fs.openSync(file, 'wx', 0o600)
    fs.writeFileSync(descriptor, raw)
    fs.fsyncSync(descriptor)
  } finally {
    if (descriptor !== null) {
      fs.closeSync(descriptor)
    }
  }
}

function publishExclusivePrimary(marker: string, raw: Buffer): boolean {
  const pending = uniquePendingSibling(marker)

  try {
    writeCompleteUnpublished(pending, raw)
    fs.linkSync(pending, marker)

    return true
  } catch {
    return false
  } finally {
    removeOwnedPending(pending)
  }
}

function publishCasArtifact(marker: string, purpose: CasPurpose, raw: Buffer): string | null {
  if (!parseLease(raw)) {
    return null
  }

  const pending = uniquePendingSibling(marker)
  const artifact = uniqueCasSibling(marker, purpose)

  try {
    writeCompleteUnpublished(pending, raw)
    fs.linkSync(pending, artifact)

    return artifact
  } catch {
    return null
  } finally {
    removeOwnedPending(pending)
  }
}

/** Restore moved bytes only with exclusive publication; never rename-overwrite. */
function restoreIsolatedPath(tombstone: string, destination: string): boolean {
  try {
    fs.linkSync(tombstone, destination)
  } catch {
    // Hard-link publication is the only restoration primitive that preserves
    // exact inode bytes without opening a pathname cleanup race. Keep the
    // tombstone as fail-closed evidence on every failure, including EEXIST.
    return false
  }

  try {
    fs.unlinkSync(tombstone)

    return true
  } catch {
    return false
  }
}

function movePathIfExact(
  source: string,
  expectedRaw: Buffer,
  marker: string,
  purpose: CasPurpose
): string | null {
  const tombstone = uniqueCasSibling(marker, purpose)

  try {
    fs.renameSync(source, tombstone)
  } catch {
    return null
  }

  const moved = readRaw(tombstone)

  if (moved.kind !== 'present' || !moved.raw.equals(expectedRaw)) {
    restoreIsolatedPath(tombstone, source)

    return null
  }

  return tombstone
}

function removePathIfExact(source: string, expectedRaw: Buffer, marker: string, purpose: CasPurpose): boolean {
  const tombstone = movePathIfExact(source, expectedRaw, marker, purpose)

  if (!tombstone) {
    return false
  }

  try {
    fs.unlinkSync(tombstone)

    return true
  } catch {
    return false
  }
}

function removeArtifactIfExact(artifact: string, expectedRaw: Buffer, marker: string): boolean {
  return removePathIfExact(artifact, expectedRaw, marker, 'displaced')
}

function replaceMarkerIfExact(
  marker: string,
  expectedRaw: Buffer,
  replacementRaw: Buffer
): boolean {
  // The fully-written valid shadow is visible before the primary moves. Every
  // reader scans these artifacts, so there is no false-clear rename window.
  const shadow = publishCasArtifact(marker, 'shadow', replacementRaw)

  if (!shadow) {
    return false
  }

  const previous = movePathIfExact(marker, expectedRaw, marker, 'previous')

  if (!previous) {
    removeArtifactIfExact(shadow, replacementRaw, marker)

    return false
  }

  try {
    fs.linkSync(shadow, marker)
  } catch {
    // A foreign primary wins. Keep both valid recovery artifacts so readers
    // fail closed and no evidence is silently discarded.
    return false
  }

  const previousRemoved = removeArtifactIfExact(previous, expectedRaw, marker)
  const shadowRemoved = removeArtifactIfExact(shadow, replacementRaw, marker)

  return previousRemoved && shadowRemoved
}

function markerMtimeIsFresh(file: string, now: number): boolean {
  try {
    const age = now - fs.statSync(file).mtimeMs / 1000

    return Number.isFinite(age) && age <= MCP_BRIDGE_LEASE_SECONDS
  } catch {
    return true
  }
}

function inspectRecoveryArtifacts(marker: string, now: number, deps: IdentityDeps = {}): RecoveryState {
  const candidates = listCasArtifacts(marker)

  if (candidates === null) {
    return 'unreadable'
  }

  let state: RecoveryState = 'clear'

  for (const candidate of candidates) {
    const snapshot = readRaw(candidate)

    if (snapshot.kind === 'absent') {
      continue
    }

    if (snapshot.kind === 'unreadable') {
      return 'unreadable'
    }

    const lease = parseLease(snapshot.raw)
    const purpose = casPurpose(marker, candidate)

    const emergencyValid =
      purpose !== 'emergency' ||
      (lease !== null && lease.expiresAt - lease.createdAt <= MCP_BRIDGE_EMERGENCY_LEASE_SECONDS)

    const timeValid = lease !== null && lease.createdAt <= now + MCP_BRIDGE_CLOCK_SKEW_SECONDS

    if (lease && emergencyValid && timeValid) {
      const emergency = purpose === 'emergency'
      const ownerIsActive = emergency || syncPidIdentity(lease.ownerPid, lease.createdAt, deps) !== 'stale'
      const active = now <= lease.expiresAt && (ownerIsActive || now <= lease.handoffGraceUntil)

      if (active) {
        state = 'active'

        continue
      }

      if (!removeArtifactIfExact(candidate, snapshot.raw, marker)) {
        return 'unreadable'
      }

      continue
    }

    if (markerMtimeIsFresh(candidate, now)) {
      return 'unreadable'
    }

    if (!removeArtifactIfExact(candidate, snapshot.raw, marker)) {
      return 'unreadable'
    }
  }

  return state
}

/** Read the canonical document, or a visible valid CAS recovery generation. */
export function readMcpBridgeQuiesceLease(hermesHome: string): McpBridgeQuiesceLease | null {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const primary = readRaw(marker)

    if (primary.kind === 'present') {
      const lease = parseLease(primary.raw)

      if (lease) {
        return bindLeaseGeneration(lease, primary.raw)
      }
    }

    const artifacts = listCasArtifacts(marker)

    if (artifacts === null) {
      return null
    }

    for (const artifact of artifacts) {
      const snapshot = readRaw(artifact)

      if (snapshot.kind !== 'present') {
        continue
      }

      const lease = parseLease(snapshot.raw)
      const purpose = casPurpose(marker, artifact)

      if (
        lease &&
        (purpose !== 'emergency' || lease.expiresAt - lease.createdAt <= MCP_BRIDGE_EMERGENCY_LEASE_SECONDS)
      ) {
        return lease
      }
    }

    const after = readRaw(marker)

    if (after.kind === 'present') {
      const lease = parseLease(after.raw)

      if (lease) {
        return bindLeaseGeneration(lease, after.raw)
      }
    }

    if (artifacts.length === 0) {
      return null
    }
  }

  return null
}

function readPrimaryLease(marker: string): { lease: McpBridgeQuiesceLease; raw: Buffer } | null {
  const snapshot = readRaw(marker)

  if (snapshot.kind !== 'present') {
    return null
  }

  const lease = parseLease(snapshot.raw)

  return lease ? { lease, raw: snapshot.raw } : null
}

function canonicalRoot(root: string): string | null {
  try {
    const resolved = fs.realpathSync.native(root)

    return process.platform === 'win32' ? resolved.toLowerCase() : resolved
  } catch {
    return null
  }
}

function rootsMatch(left: string, right: string): boolean {
  const canonicalLeft = canonicalRoot(left)
  const canonicalRight = canonicalRoot(right)

  return canonicalLeft !== null && canonicalRight !== null && canonicalLeft === canonicalRight
}

function leaseIsWithinBounds(lease: McpBridgeQuiesceLease, now: number, installRoot: string): boolean {
  return (
    rootsMatch(lease.installRoot, installRoot) &&
    lease.createdAt <= now + MCP_BRIDGE_CLOCK_SKEW_SECONDS &&
    lease.expiresAt - lease.createdAt <= MCP_BRIDGE_LEASE_SECONDS &&
    lease.handoffGraceUntil - lease.createdAt <= MCP_BRIDGE_MAX_HANDOFF_GRACE_SECONDS &&
    now <= lease.expiresAt
  )
}

function markerIsActive(
  marker: string,
  raw: Buffer,
  now: number,
  installRoot: string,
  deps: IdentityDeps
): boolean {
  const lease = parseLease(raw)

  if (lease) {
    if (!leaseIsWithinBounds(lease, now, installRoot)) {
      return false
    }

    const identity = syncPidIdentity(lease.ownerPid, lease.createdAt, deps)

    return identity !== 'stale' || now <= lease.handoffGraceUntil
  }

  const legacy = parseLegacyLease(raw)
  const legacyRoot = path.join(path.dirname(marker), 'hermes-agent')

  return Boolean(
    legacy &&
      rootsMatch(installRoot, legacyRoot) &&
      now - legacy.createdAt <= MCP_BRIDGE_LEASE_SECONDS &&
      now - legacy.createdAt >= -MCP_BRIDGE_CLOCK_SKEW_SECONDS &&
      syncPidIdentity(legacy.ownerPid, legacy.createdAt, deps) !== 'stale'
  )
}

function renewedLease(
  current: McpBridgeQuiesceLease,
  ownerPid: number,
  now: number
): McpBridgeQuiesceLease {
  return {
    ...current,
    ownerPid,
    createdAt: now,
    expiresAt: now + MCP_BRIDGE_LEASE_SECONDS,
    handoffGraceUntil: now + MCP_BRIDGE_HANDOFF_GRACE_SECONDS
  }
}

export function acquireMcpBridgeQuiesceLease(
  hermesHome: string,
  installRoot: string,
  deps: LeaseDeps = {}
): McpBridgeQuiesceLease | null {
  const now = Math.floor((deps.now ?? epochSeconds)())
  const ownerPid = deps.ownerPid ?? process.pid
  const resolvedRoot = canonicalRoot(installRoot)

  if (!resolvedRoot || !Number.isSafeInteger(ownerPid) || ownerPid <= 0 || !Number.isSafeInteger(now) || now <= 0) {
    return null
  }

  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)

  if (inspectRecoveryArtifacts(marker, now, deps) !== 'clear') {
    return null
  }

  const existing = readRaw(marker)

  if (existing.kind === 'unreadable') {
    return null
  }

  if (existing.kind === 'present') {
    const parsed = parseLease(existing.raw)

    if (parsed && !rootsMatch(parsed.installRoot, resolvedRoot) && now <= parsed.expiresAt) {
      return null
    }

    if (markerIsActive(marker, existing.raw, now, resolvedRoot, deps)) {
      return null
    }

    if (!parsed && !parseLegacyLease(existing.raw) && markerMtimeIsFresh(marker, now)) {
      return null
    }

    if (!removePathIfExact(marker, existing.raw, marker, 'displaced')) {
      return null
    }
  }

  const lease: McpBridgeQuiesceLease = {
    schemaVersion: 1,
    leaseId: (deps.randomId ?? randomUUID)(),
    ownerPid,
    createdAt: now,
    expiresAt: now + MCP_BRIDGE_LEASE_SECONDS,
    handoffGraceUntil: now,
    installRoot: fs.realpathSync.native(installRoot)
  }

  const raw = serializeLease(lease)

  if (!LEASE_ID_PATTERN.test(lease.leaseId) || !publishExclusivePrimary(marker, raw)) {
    return null
  }

  return bindLeaseGeneration(lease, raw)
}

export function markMcpBridgeQuiesceLeaseForHandoff(
  hermesHome: string,
  expected: McpBridgeQuiesceLease,
  deps: HandoffDeps = {}
): McpBridgeQuiesceLease | null {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const now = Math.floor((deps.now ?? epochSeconds)())
  const expectedRaw = leaseGeneration(expected)

  if (
    !expectedRaw ||
    !Number.isSafeInteger(now) ||
    now <= 0 ||
    inspectRecoveryArtifacts(marker, now, deps) !== 'clear'
  ) {
    return null
  }

  const snapshot = readPrimaryLease(marker)

  if (
    !snapshot ||
    !snapshot.raw.equals(expectedRaw) ||
    !leaseIsWithinBounds(snapshot.lease, now, snapshot.lease.installRoot)
  ) {
    return null
  }

  const updated = renewedLease(snapshot.lease, snapshot.lease.ownerPid, now)
  const updatedRaw = serializeLease(updated)

  return replaceMarkerIfExact(marker, expectedRaw, updatedRaw) ? bindLeaseGeneration(updated, updatedRaw) : null
}

export function transferMcpBridgeQuiesceLease(
  hermesHome: string,
  expected: McpBridgeQuiesceLease,
  ownerPid: number,
  deps: HandoffDeps = {}
): McpBridgeQuiesceLease | null {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const now = Math.floor((deps.now ?? epochSeconds)())
  const expectedRaw = leaseGeneration(expected)

  if (
    !expectedRaw ||
    !Number.isSafeInteger(now) ||
    now <= 0 ||
    !Number.isSafeInteger(ownerPid) ||
    ownerPid <= 0 ||
    inspectRecoveryArtifacts(marker, now, deps) !== 'clear' ||
    syncPidIdentity(ownerPid, now, deps) !== 'matching'
  ) {
    return null
  }

  const snapshot = readPrimaryLease(marker)

  if (
    !snapshot ||
    !snapshot.raw.equals(expectedRaw) ||
    !leaseIsWithinBounds(snapshot.lease, now, snapshot.lease.installRoot)
  ) {
    return null
  }

  const updated = renewedLease(snapshot.lease, ownerPid, now)
  const updatedRaw = serializeLease(updated)

  return replaceMarkerIfExact(marker, expectedRaw, updatedRaw) ? bindLeaseGeneration(updated, updatedRaw) : null
}

export async function waitForMcpBridgeQuiesceLeaseAdoption(
  hermesHome: string,
  expected: McpBridgeQuiesceLease,
  deps: AdoptionDeps
): Promise<McpBridgeQuiesceLease | null> {
  const timeoutMs = deps.timeoutMs ?? 10_000
  const pollMs = deps.pollMs ?? 200
  const nowMs = deps.nowMs ?? Date.now
  const sleep = deps.wait ?? (delay => new Promise(resolve => setTimeout(resolve, delay)))
  const excluded = new Set([expected.ownerPid, ...(deps.excludedOwnerPids ?? [])])
  const deadline = nowMs() + timeoutMs
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)

  while (nowMs() <= deadline) {
    if (deps.requiredOwnerPid !== undefined && !pidAppearsAlive(deps.requiredOwnerPid, deps.isPidAlive)) {
      return null
    }

    const now = Math.floor((deps.now ?? epochSeconds)())
    const recovery = inspectRecoveryArtifacts(marker, now, { isPidAlive: deps.isPidAlive })

    if (recovery !== 'clear') {
      await sleep(pollMs)

      continue
    }

    const currentSnapshot = readPrimaryLease(marker)
    const current = currentSnapshot?.lease ?? null

    if (current && current.leaseId !== expected.leaseId) {
      return null
    }

    const updateOwner = readUpdateOwnerClaim(deps.readUpdateOwner)

    if (
      current &&
      currentSnapshot &&
      leaseIsWithinBounds(current, now, expected.installRoot) &&
      !excluded.has(current.ownerPid) &&
      (deps.requiredOwnerPid === undefined || current.ownerPid === deps.requiredOwnerPid) &&
      updateOwner?.pid === current.ownerPid
    ) {
      const processCreatedAt = await resolveProcessCreatedAt(current.ownerPid, deps)

      if (
        resolvedPidIdentity(current.ownerPid, current.createdAt, processCreatedAt, deps) === 'matching' &&
        resolvedPidIdentity(current.ownerPid, updateOwner.startedAt, processCreatedAt, deps) === 'matching'
      ) {
        return bindLeaseGeneration(current, currentSnapshot.raw)
      }
    }

    await sleep(pollMs)
  }

  return null
}

export async function handOffMcpBridgeLeaseToStagedUpdater(
  hermesHome: string,
  expected: McpBridgeQuiesceLease,
  updaterPid: number,
  deps: StagedHandoffDeps
): Promise<StagedMcpBridgeLeaseHandoff> {
  const expectedRaw = leaseGeneration(expected)
  const expectedLease = expectedRaw ? parseLease(expectedRaw) : null

  if (
    !expectedRaw ||
    !expectedLease ||
    !Number.isSafeInteger(updaterPid) ||
    updaterPid <= 0 ||
    !Number.isSafeInteger(deps.requiredOwnerStartedAt) ||
    deps.requiredOwnerStartedAt <= 0 ||
    typeof deps.verifyRequiredOwnerGeneration !== 'function'
  ) {
    return { kind: 'failed' }
  }

  const nowMs = deps.nowMs ?? Date.now
  const sleep = deps.wait ?? (delay => new Promise(resolve => setTimeout(resolve, delay)))
  const pollMs = deps.pollMs ?? 100
  const deadline = nowMs() + (deps.handoffTimeoutMs ?? 10_000)

  while (nowMs() <= deadline) {
    if (!pidAppearsAlive(updaterPid, deps.isPidAlive)) {
      return { kind: 'failed' }
    }

    if (readUpdateOwnerClaim(deps.readUpdateOwner)?.pid === updaterPid) {
      break
    }

    await sleep(pollMs)
  }

  if (
    !pidAppearsAlive(updaterPid, deps.isPidAlive) ||
    readUpdateOwnerClaim(deps.readUpdateOwner)?.pid !== updaterPid
  ) {
    return { kind: 'failed' }
  }

  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const settleDeadline = nowMs() + 1_000

  do {
    const now = Math.floor((deps.now ?? epochSeconds)())

    if (inspectRecoveryArtifacts(marker, now, { isPidAlive: deps.isPidAlive }) !== 'clear') {
      await sleep(pollMs)

      continue
    }

    const currentSnapshot = readPrimaryLease(marker)
    const current = currentSnapshot?.lease ?? null

    if (!current || !currentSnapshot) {
      await sleep(pollMs)

      continue
    }

    if (current.leaseId !== expectedLease.leaseId) {
      return { kind: 'failed' }
    }

    const updateOwner = readUpdateOwnerClaim(deps.readUpdateOwner)

    if (updateOwner?.pid !== updaterPid) {
      return { kind: 'failed' }
    }

    const processCreatedAt = await resolveProcessCreatedAt(updaterPid, deps)

    if (
      processCreatedAt !== deps.requiredOwnerStartedAt ||
      resolvedPidIdentity(updaterPid, current.createdAt, processCreatedAt, deps) !== 'matching' ||
      resolvedPidIdentity(updaterPid, updateOwner.startedAt, processCreatedAt, deps) !== 'matching'
    ) {
      return { kind: 'failed' }
    }

    if (
      current.ownerPid === updaterPid &&
      leaseIsWithinBounds(current, now, expectedLease.installRoot)
    ) {
      if (!(await verifyRequiredOwnerGeneration(deps))) {
        return { kind: 'failed' }
      }

      return { kind: 'adopted', lease: bindLeaseGeneration(current, currentSnapshot.raw) }
    }

    // The staged updater must publish its own exact lease adoption. Its update
    // marker proves process identity, not possession of the one-shot lease.
    if (!currentSnapshot.raw.equals(expectedRaw)) {
      return { kind: 'failed' }
    }

    await sleep(pollMs)
  } while (nowMs() <= settleDeadline)

  return { kind: 'failed' }
}

export function pruneInactiveMcpBridgeQuiesceLease(
  hermesHome: string,
  deps: {
    getProcessCreatedAt?: ProcessCreateTimeProbe
    installRoot?: string
    isPidAlive?: (pid: number) => boolean
    now?: () => number
  } = {}
): 'absent' | 'active' | 'removed' | 'unreadable' {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const now = Math.floor((deps.now ?? epochSeconds)())
  const recovery = inspectRecoveryArtifacts(marker, now, deps)

  if (recovery === 'active') {
    return 'active'
  }

  if (recovery === 'unreadable') {
    return 'unreadable'
  }

  const snapshot = readRaw(marker)

  if (snapshot.kind === 'absent') {
    return 'absent'
  }

  if (snapshot.kind === 'unreadable') {
    return 'unreadable'
  }

  const current = parseLease(snapshot.raw)

  if (!current) {
    return 'unreadable'
  }

  const installRoot = deps.installRoot ?? current.installRoot

  if (markerIsActive(marker, snapshot.raw, now, installRoot, deps)) {
    return 'active'
  }

  return removePathIfExact(marker, snapshot.raw, marker, 'displaced') ? 'removed' : 'unreadable'
}

export function clearMcpBridgeQuiesceLease(
  hermesHome: string,
  expected: McpBridgeQuiesceLease
): boolean {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const expectedRaw = leaseGeneration(expected)

  if (!expectedRaw || inspectRecoveryArtifacts(marker, epochSeconds()) !== 'clear') {
    return false
  }

  const snapshot = readPrimaryLease(marker)

  if (!snapshot || !snapshot.raw.equals(expectedRaw)) {
    return false
  }

  return removePathIfExact(marker, expectedRaw, marker, 'release')
}

/**
 * Revoke one handoff capability across an owner transfer.
 *
 * The release artifact is published before the primary is touched. Both the
 * TypeScript and PowerShell lease writers treat any CAS artifact as a closed
 * gate, so a writer that already sampled the old generation can finish at
 * most one CAS before this bounded sweep removes it. The short-lived artifact
 * remains as durable revocation proof and follows the normal 90/91-second
 * recovery rule.
 */
export function revokeMcpBridgeQuiesceLease(
  hermesHome: string,
  expected: McpBridgeQuiesceLease,
  deps: RevocationDeps = {}
): McpBridgeQuiesceRevocationResult {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  const now = Math.floor((deps.now ?? epochSeconds)())
  const ownerPid = deps.ownerPid ?? process.pid

  if (
    !LEASE_ID_PATTERN.test(expected.leaseId) ||
    !canonicalRoot(expected.installRoot) ||
    !Number.isSafeInteger(now) ||
    now <= 0 ||
    !Number.isSafeInteger(ownerPid) ||
    ownerPid <= 0
  ) {
    return 'unproven'
  }

  const revocationRaw = serializeLease({
    ...expected,
    ownerPid,
    createdAt: now,
    expiresAt: now + MCP_BRIDGE_HANDOFF_GRACE_SECONDS,
    handoffGraceUntil: now + MCP_BRIDGE_HANDOFF_GRACE_SECONDS
  })
  const revocation = publishCasArtifact(marker, 'release', revocationRaw)

  if (!revocation) {
    return 'unproven'
  }

  const matchesCapability = (lease: McpBridgeQuiesceLease | null): boolean =>
    Boolean(
      lease && lease.leaseId === expected.leaseId && rootsMatch(lease.installRoot, expected.installRoot)
    )

  for (let attempt = 0; attempt < (deps.maxAttempts ?? 8); attempt += 1) {
    const primary = readRaw(marker)

    if (primary.kind === 'unreadable') {
      return 'unproven'
    }

    if (primary.kind === 'present') {
      if (!matchesCapability(parseLease(primary.raw))) {
        return 'unproven'
      }

      if (!removePathIfExact(marker, primary.raw, marker, 'release')) {
        continue
      }
    }

    const artifacts = listCasArtifacts(marker)

    if (artifacts === null) {
      return 'unproven'
    }

    let retry = false

    for (const artifact of artifacts) {
      if (artifact === revocation) {
        continue
      }

      const snapshot = readRaw(artifact)

      if (snapshot.kind === 'absent') {
        retry = true

        continue
      }

      if (snapshot.kind === 'unreadable' || !matchesCapability(parseLease(snapshot.raw))) {
        return 'unproven'
      }

      if (!removeArtifactIfExact(artifact, snapshot.raw, marker)) {
        retry = true
      }
    }

    if (retry) {
      continue
    }

    const finalPrimary = readRaw(marker)
    const finalArtifacts = listCasArtifacts(marker)
    const finalRevocation = readRaw(revocation)

    if (
      finalPrimary.kind === 'absent' &&
      finalArtifacts?.length === 1 &&
      finalArtifacts[0] === revocation &&
      finalRevocation.kind === 'present' &&
      finalRevocation.raw.equals(revocationRaw)
    ) {
      return 'revoked'
    }
  }

  return 'unproven'
}
