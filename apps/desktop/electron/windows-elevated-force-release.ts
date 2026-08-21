/**
 * Elevated (UAC) force-release path for Windows updates.
 *
 * Passes only an authenticated, nonce-scoped request file containing the
 * canonical install identity and exact PID/create-time/resource claims.
 * Never accepts arbitrary command text.
 */

import { createHash, randomBytes } from 'node:crypto'
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import type { ForceReleaseHolder } from './windows-update-force-release'

const execFileAsync = promisify(execFile)

export const FORCE_RELEASE_REQUEST_SCHEMA = 1 as const

export type ForceReleaseRequest = {
  schemaVersion: typeof FORCE_RELEASE_REQUEST_SCHEMA
  nonce: string
  issuedAt: number
  expiresAt: number
  installRoot: string
  installRootHash: string
  holders: Array<{
    pid: number
    createdAt: number
    name: string
    resource?: string
  }>
  /** HMAC-like integrity over the body using a one-shot secret written beside the request. */
  requestMac: string
}

export type ForceReleaseSurvivor = {
  pid: number
  detail: string
  resource?: string
  win32Error?: number
}

export type ForceReleaseResponse = {
  schemaVersion: typeof FORCE_RELEASE_REQUEST_SCHEMA
  nonce: string
  ok: boolean
  cleared: boolean
  cancelled?: boolean
  error?: string
  terminated?: number[]
  survivors?: ForceReleaseSurvivor[]
}

export function hashInstallRoot(installRoot: string): string {
  return createHash('sha256').update(path.resolve(installRoot)).digest('hex')
}

/**
 * Canonical numeric token shared by Electron and the elevated PowerShell helper.
 * Integers stay decimal integers; non-integers use JS/ECMA round-trip text, which
 * matches .NET `double.ToString("R", InvariantCulture)`.
 */
export function canonicalNumericToken(value: number): string {
  if (!Number.isFinite(value)) return '0'
  if (Object.is(value, -0)) return '0'
  if (Number.isInteger(value)) return String(value)
  return String(value)
}

export function canonicalForceReleasePayload(input: {
  schemaVersion: number
  nonce: string
  issuedAt: number
  expiresAt: number
  installRoot: string
  installRootHash: string
  holders: ReadonlyArray<{ pid: number; createdAt: number; name: string; resource?: string }>
}): string {
  const holderLines = input.holders
    .map(
      holder =>
        `${holder.pid}\t${canonicalNumericToken(holder.createdAt)}\t${holder.name}\t${holder.resource ?? ''}`
    )
    .join('\n')
  return [
    String(input.schemaVersion),
    input.nonce,
    canonicalNumericToken(input.issuedAt),
    canonicalNumericToken(input.expiresAt),
    input.installRoot,
    input.installRootHash,
    holderLines
  ].join('\n')
}

export function buildForceReleaseRequest(input: {
  installRoot: string
  holders: readonly ForceReleaseHolder[]
  now?: number
  ttlMs?: number
  nonce?: string
  secret: string
}): ForceReleaseRequest {
  const now = input.now ?? Date.now()
  const ttlMs = input.ttlMs ?? 120_000
  const nonce = input.nonce ?? randomBytes(16).toString('hex')
  const installRoot = path.resolve(input.installRoot)
  const holders = input.holders.map(holder => ({
    pid: holder.pid,
    createdAt: holder.createdAt,
    name: holder.name,
    ...(holder.resource ? { resource: holder.resource } : {})
  }))
  const body = {
    schemaVersion: FORCE_RELEASE_REQUEST_SCHEMA,
    nonce,
    issuedAt: now,
    expiresAt: now + ttlMs,
    installRoot,
    installRootHash: hashInstallRoot(installRoot),
    holders
  }
  const requestMac = createHash('sha256')
    .update(input.secret)
    .update('\n')
    .update(canonicalForceReleasePayload(body))
    .digest('hex')

  return { ...body, requestMac }
}

export function verifyForceReleaseRequest(
  request: ForceReleaseRequest,
  secret: string,
  expectedInstallRoot: string,
  now = Date.now()
): { ok: true } | { ok: false; reason: string } {
  if (request.schemaVersion !== FORCE_RELEASE_REQUEST_SCHEMA) {
    return { ok: false, reason: 'schema' }
  }
  if (!request.nonce || typeof request.nonce !== 'string') {
    return { ok: false, reason: 'nonce' }
  }
  if (now > request.expiresAt) {
    return { ok: false, reason: 'expired' }
  }
  if (path.resolve(request.installRoot) !== path.resolve(expectedInstallRoot)) {
    return { ok: false, reason: 'install-root-mismatch' }
  }
  if (request.installRootHash !== hashInstallRoot(expectedInstallRoot)) {
    return { ok: false, reason: 'install-root-hash-mismatch' }
  }
  const { requestMac, ...body } = request
  const expectedMac = createHash('sha256')
    .update(secret)
    .update('\n')
    .update(canonicalForceReleasePayload(body))
    .digest('hex')
  if (requestMac !== expectedMac) {
    return { ok: false, reason: 'mac-mismatch' }
  }
  if (!Array.isArray(request.holders) || request.holders.length === 0) {
    return { ok: false, reason: 'holders' }
  }
  for (const holder of request.holders) {
    if (!Number.isInteger(holder.pid) || holder.pid <= 0) {
      return { ok: false, reason: 'holder-pid' }
    }
    if (!Number.isFinite(holder.createdAt) || holder.createdAt <= 0) {
      return { ok: false, reason: 'holder-created-at' }
    }
  }
  return { ok: true }
}

export function forceReleasePaths(dir: string, nonce: string) {
  return {
    requestPath: path.join(dir, `force-release-${nonce}.request.json`),
    secretPath: path.join(dir, `force-release-${nonce}.secret`),
    responsePath: path.join(dir, `force-release-${nonce}.response.json`)
  }
}

export async function writeForceReleaseRequestFiles(input: {
  installRoot: string
  holders: readonly ForceReleaseHolder[]
  directory?: string
}): Promise<{
  directory: string
  request: ForceReleaseRequest
  secret: string
  requestPath: string
  secretPath: string
  responsePath: string
}> {
  const directory = input.directory ?? fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-force-release-'))
  const secret = randomBytes(32).toString('hex')
  const request = buildForceReleaseRequest({
    installRoot: input.installRoot,
    holders: input.holders,
    secret
  })
  const paths = forceReleasePaths(directory, request.nonce)
  fs.writeFileSync(paths.secretPath, secret, { encoding: 'utf8', mode: 0o600 })
  fs.writeFileSync(paths.requestPath, JSON.stringify(request, null, 2), { encoding: 'utf8', mode: 0o600 })
  return {
    directory,
    request,
    secret,
    ...paths
  }
}

export function parseForceReleaseResponse(raw: string, expectedNonce: string): ForceReleaseResponse | null {
  try {
    const parsed = JSON.parse(raw)
    if (parsed?.schemaVersion !== FORCE_RELEASE_REQUEST_SCHEMA) return null
    if (parsed?.nonce !== expectedNonce) return null
    if (typeof parsed.ok !== 'boolean' || typeof parsed.cleared !== 'boolean') return null
    return parsed as ForceReleaseResponse
  } catch {
    return null
  }
}

export function formatElevatedForceReleaseFailure(response: ForceReleaseResponse | null): {
  message: string
  protectedHolders: boolean
} {
  const survivors = Array.isArray(response?.survivors) ? response!.survivors! : []
  const survivorText = survivors
    .slice(0, 8)
    .map(entry => {
      const resource = entry.resource ? ` resource=${entry.resource}` : ''
      const win32 =
        typeof entry.win32Error === 'number'
          ? ` win32=${entry.win32Error}`
          : /win32=(\d+)/i.test(entry.detail || '')
            ? ''
            : ''
      return `PID ${entry.pid}${resource} ${entry.detail || 'survived'}${win32}`.trim()
    })
    .join('; ')

  const protectedHolders = survivors.some(entry =>
    /protected|unkillable|win32=5/i.test(`${entry.detail || ''} ${entry.win32Error ?? ''}`)
  )

  if (survivorText) {
    return {
      message:
        `Update aborted: elevated force-release could not clear install file locks (${survivorText}). ` +
        'The virtual environment was not modified.',
      protectedHolders
    }
  }

  return {
    message:
      response?.error ||
      'Update aborted: elevated force-release could not clear install file locks. The virtual environment was not modified.',
    protectedHolders
  }
}

/**
 * Launch the elevated helper via ShellExecuteEx runas. The helper path must be
 * a repo-owned script; only the request file path is passed on the command line.
 */
export async function launchElevatedForceReleaseHelper(input: {
  helperScriptPath: string
  requestPath: string
  responsePath: string
  platform?: NodeJS.Platform
  run?: (command: string, args: string[]) => Promise<{ code: number }>
}): Promise<{ kind: 'launched' } | { kind: 'cancelled' } | { kind: 'failed'; detail: string }> {
  const platform = input.platform ?? process.platform
  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }

  const run =
    input.run ??
    (async (command: string, args: string[]) => {
      try {
        await execFileAsync(command, args, { windowsHide: true, timeout: 180_000 })
        return { code: 0 }
      } catch (error: any) {
        const code = typeof error?.code === 'number' ? error.code : 1
        // 1223 = ERROR_CANCELLED (UAC denied)
        if (code === 1223 || /canceled|cancelled/i.test(String(error?.message ?? ''))) {
          return { code: 1223 }
        }
        return { code }
      }
    })

  // powershell Start-Process -Verb RunAs waits for elevation consent and the child.
  const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  const script = `
$p = Start-Process -FilePath ${JSON.stringify(ps)} -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',${JSON.stringify(input.helperScriptPath)},'-RequestPath',${JSON.stringify(input.requestPath)},'-ResponsePath',${JSON.stringify(input.responsePath)})
if ($null -eq $p) { exit 1223 }
exit $p.ExitCode
`.trim()

  const result = await run(ps, ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script])
  if (result.code === 1223) return { kind: 'cancelled' }
  if (result.code === 0) return { kind: 'launched' }
  return { kind: 'failed', detail: `elevated helper exit ${result.code}` }
}
