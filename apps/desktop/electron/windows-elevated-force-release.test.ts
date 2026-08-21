import assert from 'node:assert/strict'
import path from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

import { describe, it } from 'vitest'

import {
  buildForceReleaseRequest,
  canonicalForceReleasePayload,
  canonicalNumericToken,
  formatElevatedForceReleaseFailure,
  parseForceReleaseResponse,
  verifyForceReleaseRequest
} from './windows-elevated-force-release'
import { parseTerminateScriptOutput } from './windows-process-terminate'
import {
  buildRestartManagerScript,
  parseRestartManagerOutput,
  RESTART_MANAGER_ROW_SPLIT_EXPRESSION
} from './windows-restart-manager'

const execFileAsync = promisify(execFile)

describe('elevated force-release request contract', () => {
  it('binds request MAC to install root + exact holder claims', () => {
    const secret = 's'.repeat(32)
    const request = buildForceReleaseRequest({
      installRoot: 'C:\\Users\\gwmai\\AppData\\Local\\hermes',
      holders: [{ pid: 9, createdAt: 100, name: 'hermes.exe', cmdline: 'hermes.exe tools', source: 'scanner' }],
      secret,
      now: 1_000,
      ttlMs: 60_000,
      nonce: 'abc123'
    })

    assert.equal(request.nonce, 'abc123')
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\hermes', 1_500).ok,
      true
    )
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\other', 1_500).ok,
      false
    )
    assert.equal(verifyForceReleaseRequest(request, 'wrong', request.installRoot, 1_500).ok, false)
    assert.equal(verifyForceReleaseRequest(request, secret, request.installRoot, 100_000).ok, false)
  })

  it('canonical payload is stable for helper MAC verification', () => {
    const payload = canonicalForceReleasePayload({
      schemaVersion: 1,
      nonce: 'n',
      issuedAt: 1,
      expiresAt: 2,
      installRoot: 'C:\\h',
      installRootHash: 'abc',
      holders: [{ pid: 1, createdAt: 2, name: 'x', resource: 'y' }]
    })
    assert.equal(payload, ['1', 'n', '1', '2', 'C:\\h', 'abc', '1\t2\tx\ty'].join('\n'))
  })

  it('keeps TS/PowerShell MAC numeric parity for fractional create times', async () => {
    const createdAt = 1755738237.4531252
    const body = {
      schemaVersion: 1,
      nonce: 'n',
      issuedAt: 10,
      expiresAt: 20,
      installRoot: 'C:\\h',
      installRootHash: 'abc',
      holders: [{ pid: 1, createdAt, name: 'x', resource: 'y' }]
    }
    const canonical = canonicalForceReleasePayload(body)
    assert.match(canonical, new RegExp(`1\\t${canonicalNumericToken(createdAt)}\\tx\\ty`))

    if (process.platform !== 'win32') return
    const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const script = `
function Format-CanonicalNumber([double]$Value) {
  if ([double]::IsNaN($Value) -or [double]::IsInfinity($Value)) { return '0' }
  if ($Value -eq 0) { return '0' }
  $truncated = [math]::Truncate($Value)
  if ($Value -eq $truncated -and [math]::Abs($Value) -lt 9007199254740991) {
    return [string][int64]$truncated
  }
  return $Value.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture)
}
$created = [double]1755738237.4531252
$line = ("{0}\`t{1}\`t{2}\`t{3}" -f 1, (Format-CanonicalNumber $created), 'x', 'y')
$canonical = @('1','n','10','20','C:\\h','abc',$line) -join "\`n"
$expected = @'
1
n
10
20
C:\\h
abc
1\t1755738237.4531252\tx\ty
'@.Trim()
if ($canonical -ne $expected) {
  Write-Output 'CANON_MISMATCH'
  Write-Output $canonical
  exit 2
}
Write-Output 'mac-parity-ok'
exit 0
`.trim()
    const { stdout } = await execFileAsync(
      ps,
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', windowsHide: true, timeout: 10_000 }
    )
    assert.match(String(stdout), /mac-parity-ok/)
  })

  it('rejects response nonce mismatch', () => {
    const raw = JSON.stringify({ schemaVersion: 1, nonce: 'other', ok: true, cleared: true })
    assert.equal(parseForceReleaseResponse(raw, 'expected'), null)
  })

  it('formats elevated survivor failures with pid/resource/win32', () => {
    const failure = formatElevatedForceReleaseFailure({
      schemaVersion: 1,
      nonce: 'n',
      ok: true,
      cleared: false,
      survivors: [{ pid: 9, detail: 'win32=5', resource: 'C:\\h\\venv\\Scripts\\hermes.exe', win32Error: 5 }]
    })
    assert.match(failure.message, /PID 9/)
    assert.match(failure.message, /hermes\.exe/)
    assert.equal(failure.protectedHolders, true)
  })
})

describe('terminate script output parser', () => {
  it('classifies create-time mismatch, access denied, and protected', () => {
    assert.deepEqual(parseTerminateScriptOutput('CREATE_TIME_MISMATCH actual=1 expected=2', 3), {
      kind: 'create-time-mismatch'
    })
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED', 5), {
      kind: 'access-denied',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('PROTECTED win32=5', 5), {
      kind: 'protected',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('TERMINATED', 0), { kind: 'terminated' })
    assert.deepEqual(parseTerminateScriptOutput('ALREADY_GONE', 0), { kind: 'already-gone' })
    assert.equal(parseTerminateScriptOutput('FAILED win32=87', 1).kind, 'failed')
  })
})

describe('restart manager output parser', () => {
  it('maps RM rows into force-release holders and emits safe split expression', () => {
    const holders = parseRestartManagerOutput(
      JSON.stringify([{ pid: 12, createdAt: 34, name: 'python.exe' }]),
      ['C:\\h\\venv\\Scripts\\hermes.exe']
    )
    assert.equal(holders.length, 1)
    assert.equal(holders[0]?.source, 'restart-manager')
    assert.equal(holders[0]?.pid, 12)
    assert.match(String(holders[0]?.resource), /hermes\.exe/)
    assert.equal(RESTART_MANAGER_ROW_SPLIT_EXPRESSION, "$part.Split([char]'|', 3)")
    assert.match(buildRestartManagerScript(['C:\\a']), /\$part\.Split\(\[char\]'\|', 3\)/)
  })
})
