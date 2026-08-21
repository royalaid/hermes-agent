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

import type { ForceReleaseHolder } from './windows-update-force-release'

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
  /** PIDs the elevated helper must never terminate (Desktop main, updater helper). */
  excludePids?: number[]
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
  excludePids?: readonly number[]
}): string {
  const holderLines = input.holders
    .map(
      holder =>
        `${holder.pid}\t${canonicalNumericToken(holder.createdAt)}\t${holder.name}\t${holder.resource ?? ''}`
    )
    .join('\n')
  const excludeLine = (input.excludePids ?? [])
    .filter(pid => Number.isInteger(pid) && pid > 0)
    .slice()
    .sort((a, b) => a - b)
    .join(',')
  return [
    String(input.schemaVersion),
    input.nonce,
    canonicalNumericToken(input.issuedAt),
    canonicalNumericToken(input.expiresAt),
    input.installRoot,
    input.installRootHash,
    holderLines,
    excludeLine
  ].join('\n')
}

export function buildForceReleaseRequest(input: {
  installRoot: string
  holders: readonly ForceReleaseHolder[]
  now?: number
  ttlMs?: number
  nonce?: string
  secret: string
  excludePids?: readonly number[]
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
  const excludePids = Array.from(
    new Set((input.excludePids ?? []).filter(pid => Number.isInteger(pid) && pid > 0))
  ).sort((a, b) => a - b)
  const body = {
    schemaVersion: FORCE_RELEASE_REQUEST_SCHEMA,
    nonce,
    issuedAt: now,
    expiresAt: now + ttlMs,
    installRoot,
    installRootHash: hashInstallRoot(installRoot),
    holders,
    ...(excludePids.length > 0 ? { excludePids } : {})
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

export type ForceReleaseRequestFiles = {
  directory: string
  request: ForceReleaseRequest
  /** Present only until cleanup; never log or return this after launch. */
  secret: string
  requestPath: string
  secretPath: string
  responsePath: string
  /** Exact helper response temp path once the elevated helper PID is known. */
  responseTempPath?: string
  /** True when writeForceReleaseRequestFiles created the directory via mkdtemp. */
  ownedDirectory: boolean
}

export async function writeForceReleaseRequestFiles(input: {
  installRoot: string
  holders: readonly ForceReleaseHolder[]
  directory?: string
  /** PIDs the elevated helper must never target (Desktop main, updater helper, etc.). */
  excludePids?: readonly number[]
}): Promise<ForceReleaseRequestFiles> {
  const ownedDirectory = input.directory == null
  const directory = input.directory ?? fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-force-release-'))
  const secret = randomBytes(32).toString('hex')
  const request = buildForceReleaseRequest({
    installRoot: input.installRoot,
    holders: input.holders,
    secret,
    excludePids: input.excludePids
  })
  const paths = forceReleasePaths(directory, request.nonce)
  try {
    fs.writeFileSync(paths.secretPath, secret, { encoding: 'utf8', mode: 0o600 })
    fs.writeFileSync(paths.requestPath, JSON.stringify(request, null, 2), { encoding: 'utf8', mode: 0o600 })
  } catch (error) {
    // A failed second write can otherwise strand the freshly-created secret or
    // owned mkdtemp directory before the caller receives a return value.
    cleanupForceReleaseArtifacts({
      directory,
      requestPath: paths.requestPath,
      secretPath: paths.secretPath,
      responsePath: paths.responsePath,
      ownedDirectory
    })
    throw error
  }
  return {
    directory,
    request,
    secret,
    ownedDirectory,
    ...paths
  }
}

/**
 * Idempotent cleanup of nonce request/secret/response artifacts.
 * Removes only the exact owned files. Never recursive-deletes a directory.
 * If the directory was owned (mkdtemp) and is empty after file removal, remove
 * the empty directory non-recursively; otherwise leave it (and any unexpected
 * sentinel/reparse entries) intact.
 */
export function cleanupForceReleaseArtifacts(files: {
  directory?: string
  requestPath?: string
  secretPath?: string
  responsePath?: string
  responseTempPath?: string
  ownedDirectory?: boolean
}): void {
  for (const filePath of [files.requestPath, files.secretPath, files.responsePath, files.responseTempPath]) {
    if (!filePath) continue
    try {
      fs.rmSync(filePath, { force: true })
    } catch {
      void 0
    }
  }

  if (files.ownedDirectory && files.directory) {
    try {
      const remaining = fs.readdirSync(files.directory)
      if (remaining.length === 0) {
        fs.rmdirSync(files.directory)
      }
    } catch {
      void 0
    }
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
 * Environment variable names used by the constant elevated launcher.
 * Dynamic filesystem paths travel ONLY through these env vars — never as
 * PowerShell source text — so metacharacters cannot be evaluated.
 */
export const ELEVATED_FORCE_RELEASE_LAUNCH_ENV = {
  helper: 'HERMES_FORCE_RELEASE_HELPER',
  request: 'HERMES_FORCE_RELEASE_REQUEST',
  response: 'HERMES_FORCE_RELEASE_RESPONSE'
} as const

/**
 * Windows PowerShell's Start-Process joins -ArgumentList elements into one
 * command line. Quote values at that final Windows command-line boundary, not
 * in the outer PowerShell source. This is the standard CommandLineToArgvW
 * quoting rule, including doubled trailing backslashes and embedded quotes.
 */
export const WINDOWS_ARGUMENT_QUOTER_FUNCTION = [
  'function ConvertTo-WindowsArgument([string]$Value) {',
  "  if ($null -eq $Value -or $Value.IndexOf([char]0) -ge 0) { throw 'invalid force-release process argument' }",
  '  $builder = New-Object System.Text.StringBuilder',
  '  [void]$builder.Append([char]34)',
  '  $slashes = 0',
  '  foreach ($current in $Value.ToCharArray()) {',
  "    if ($current -eq [char]92) { $slashes++; continue }",
  "    if ($current -eq [char]34) { [void]$builder.Append((('\\' * ($slashes * 2 + 1)) -join '')); [void]$builder.Append([char]34); $slashes = 0; continue }",
  "    if ($slashes -gt 0) { [void]$builder.Append((('\\' * $slashes) -join '')); $slashes = 0 }",
  '    [void]$builder.Append($current)',
  '  }',
  "  if ($slashes -gt 0) { [void]$builder.Append((('\\' * ($slashes * 2)) -join '')) }",
  '  [void]$builder.Append([char]34)',
  '  return $builder.ToString()',
  '}'
].join('; ')
export const ELEVATED_FORCE_RELEASE_ARGUMENT_QUOTER = WINDOWS_ARGUMENT_QUOTER_FUNCTION

/**
 * Constant outer launcher. Paths are read from env and passed as ArgumentList
 * array elements (data), never interpolated into PowerShell source.
 */
export const ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND = [
  "$ErrorActionPreference = 'Stop'",
  `$helper = [Environment]::GetEnvironmentVariable('${ELEVATED_FORCE_RELEASE_LAUNCH_ENV.helper}')`,
  `$request = [Environment]::GetEnvironmentVariable('${ELEVATED_FORCE_RELEASE_LAUNCH_ENV.request}')`,
  `$response = [Environment]::GetEnvironmentVariable('${ELEVATED_FORCE_RELEASE_LAUNCH_ENV.response}')`,
  "if ([string]::IsNullOrWhiteSpace($helper) -or [string]::IsNullOrWhiteSpace($request) -or [string]::IsNullOrWhiteSpace($response)) { throw 'missing force-release launch env' }",
  "$ps = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'",
  WINDOWS_ARGUMENT_QUOTER_FUNCTION,
  "$argList = @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(ConvertTo-WindowsArgument $helper),'-RequestPath',(ConvertTo-WindowsArgument $request),'-ResponsePath',(ConvertTo-WindowsArgument $response))",
  '$p = Start-Process -FilePath $ps -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList $argList',
  'if ($null -eq $p) { exit 1223 }',
  'Write-Output ("HERMES_ELEVATED_PID=" + $p.Id)',
  'exit $p.ExitCode'
].join('; ')

export type ElevatedForceReleaseRun = (
  command: string,
  args: string[],
  options?: { env?: NodeJS.ProcessEnv }
) => Promise<{ code: number; stdout?: string }>

/**
 * Launch the elevated helper via ShellExecuteEx runas. The helper path must be
 * a repo-owned script. Dynamic paths travel only via environment variables into
 * a constant launcher; they never become PowerShell source.
 */
export async function launchElevatedForceReleaseHelper(input: {
  helperScriptPath: string
  requestPath: string
  responsePath: string
  platform?: NodeJS.Platform
  run?: ElevatedForceReleaseRun
}): Promise<
  | { kind: 'launched'; responseTempPath?: string }
  | { kind: 'cancelled'; responseTempPath?: string }
  | { kind: 'failed'; detail: string; responseTempPath?: string }
> {
  const platform = input.platform ?? process.platform
  if (platform !== 'win32') {
    return { kind: 'failed', detail: 'windows-only' }
  }

  const run: ElevatedForceReleaseRun =
    input.run ??
    (async (command, args, options) =>
      await new Promise(resolve => {
        execFile(
          command,
          args,
          {
            windowsHide: true,
            timeout: 180_000,
            env: options?.env,
            encoding: 'utf8'
          },
          (error: any, stdout: string) => {
            const code = typeof error?.code === 'number' ? error.code : error ? 1 : 0
            // 1223 = ERROR_CANCELLED (UAC denied)
            if (code === 1223 || /canceled|cancelled/i.test(String(error?.message ?? ''))) {
              resolve({ code: 1223, stdout: String(stdout ?? '') })
              return
            }
            resolve({ code, stdout: String(stdout ?? '') })
          }
        )
      }))

  const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.helper]: input.helperScriptPath,
    [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.request]: input.requestPath,
    [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.response]: input.responsePath
  }

  const args = ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND]
  const result = await run(ps, args, { env })
  const helperPid = String(result.stdout ?? '').match(/HERMES_ELEVATED_PID=(\d+)/i)?.[1]
  const responseTempPath = helperPid ? `${input.responsePath}.${helperPid}.tmp` : undefined
  if (result.code === 1223) return { kind: 'cancelled', responseTempPath }
  if (result.code === 0) return { kind: 'launched', responseTempPath }
  return { kind: 'failed', detail: `elevated helper exit ${result.code}`, responseTempPath }
}

/** Pure helper for tests: capture argv + env without launching. */
export function buildElevatedForceReleaseLaunchInvocation(input: {
  helperScriptPath: string
  requestPath: string
  responsePath: string
}): { args: string[]; env: Record<string, string>; command: string } {
  return {
    command: ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND,
    args: ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND],
    env: {
      [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.helper]: input.helperScriptPath,
      [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.request]: input.requestPath,
      [ELEVATED_FORCE_RELEASE_LAUNCH_ENV.response]: input.responsePath
    }
  }
}
