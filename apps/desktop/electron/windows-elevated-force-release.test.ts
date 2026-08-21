import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import {
  buildForceReleaseRequest,
  canonicalForceReleasePayload,
  parseForceReleaseResponse,
  verifyForceReleaseRequest
} from './windows-elevated-force-release'
import { parseTerminateScriptOutput } from './windows-process-terminate'
import { parseRestartManagerOutput } from './windows-restart-manager'

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

  it('rejects response nonce mismatch', () => {
    const raw = JSON.stringify({ schemaVersion: 1, nonce: 'other', ok: true, cleared: true })
    assert.equal(parseForceReleaseResponse(raw, 'expected'), null)
  })
})

describe('terminate script output parser', () => {
  it('classifies create-time mismatch and access denied', () => {
    assert.deepEqual(parseTerminateScriptOutput('CREATE_TIME_MISMATCH actual=1 expected=2', 3), {
      kind: 'create-time-mismatch'
    })
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED', 5), {
      kind: 'access-denied',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('TERMINATED', 0), { kind: 'terminated' })
    assert.deepEqual(parseTerminateScriptOutput('ALREADY_GONE', 0), { kind: 'already-gone' })
  })
})

describe('restart manager output parser', () => {
  it('maps RM rows into force-release holders', () => {
    const holders = parseRestartManagerOutput(
      JSON.stringify([{ pid: 12, createdAt: 34, name: 'python.exe' }]),
      ['C:\\h\\venv\\Scripts\\hermes.exe']
    )
    assert.equal(holders.length, 1)
    assert.equal(holders[0]?.source, 'restart-manager')
    assert.equal(holders[0]?.pid, 12)
    assert.match(String(holders[0]?.resource), /hermes\.exe/)
  })
})
