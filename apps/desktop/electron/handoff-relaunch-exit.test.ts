import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  HANDOFF_RELAUNCH_RESULT_PUBLICATION_GRACE_MS,
  handoffRelaunchExitAckPath,
  handoffRelaunchRequestPath,
  hasHandoffRelaunchRequest,
  inspectHandoffRelaunchExit
} from './handoff-relaunch-exit'
import { handoffResultPath } from './handoff-result'

const NOW_SECONDS = 1_700_000_000
const NOW_MS = NOW_SECONDS * 1_000
const ATTEMPT_ID = 'attempt-id-1234567890'
const SECOND_ATTEMPT_ID = 'attempt-id-0987654321'
const INVOCATION_ID = 'invocation-id-123456'
const LEASE_ID = 'bridge-lease-id-123456'
const PID = 4242
const BUILD_ID = 'a'.repeat(40)

const HEALTH = {
  critical_syntax: true,
  critical_imports: true,
  dependencies: true,
  node_dependencies: true
}

interface Fixture {
  executable: string
  home: string
  root: string
}

interface RequestWire {
  schema_version: 1
  attempt_id: string
  root: string
  executable: string
  requested_at: number
  expires_at: number
}

function fixture(tag: string): Fixture {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `handoff-relaunch-exit-${tag}-`))
  const root = path.join(home, 'hermes-agent')
  const executableDirectory = path.join(home, 'packaged-desktop')
  const executable = path.join(executableDirectory, 'Hermes.exe')
  fs.mkdirSync(root)
  fs.mkdirSync(executableDirectory)
  fs.writeFileSync(executable, 'test executable')

  return {
    home: fs.realpathSync.native(home),
    root: fs.realpathSync.native(root),
    executable: fs.realpathSync.native(executable)
  }
}

function requestWire(
  where: Fixture,
  {
    attemptId = ATTEMPT_ID,
    requestedAt = NOW_SECONDS - 2,
    expiresAt = requestedAt + 120
  }: { attemptId?: string; requestedAt?: number; expiresAt?: number } = {}
): RequestWire {
  return {
    schema_version: 1,
    attempt_id: attemptId,
    root: where.root,
    executable: where.executable,
    requested_at: requestedAt,
    expires_at: expiresAt
  }
}

function writeRequest(where: Fixture, request: RequestWire | Record<string, unknown>): string {
  const attemptId = String(request.attempt_id)
  const file = handoffRelaunchRequestPath(where.home, attemptId)
  fs.writeFileSync(file, JSON.stringify(request))

  return file
}

function pendingResultWire(
  where: Fixture,
  request: RequestWire,
  {
    pid = PID,
    processStartedAt = request.requested_at + 1,
    resultRequestedAt = request.requested_at
  } = {}
): Record<string, unknown> {
  return {
    schema_version: 2,
    attempt_id: request.attempt_id,
    state: 'pending',
    ok: false,
    exit_code: null,
    message: 'Update applied; awaiting Desktop acknowledgement.',
    branch: 'main',
    invocation_id: INVOCATION_ID,
    lease_id: LEASE_ID,
    root: where.root,
    receipt: {
      schema_version: 1,
      invocation_id: INVOCATION_ID,
      lease_id: LEASE_ID,
      mode: 'git',
      root: where.root,
      remote: 'origin',
      branch: 'main',
      target_ref: 'refs/remotes/origin/main',
      target_sha: BUILD_ID,
      resulting_head: BUILD_ID,
      archive_sha: null,
      timestamp: resultRequestedAt,
      success: true,
      gateway_resume_deferred: true,
      health: { ...HEALTH }
    },
    cleanup: { update_marker_released: true, bridge_lease_released: true },
    runtime_health: { ...HEALTH },
    relaunch: {
      state: 'pending',
      pid,
      process_started_at: processStartedAt,
      executable: where.executable,
      requested_at: resultRequestedAt,
      acknowledged_at: null
    },
    desktop: {
      build_id: null,
      build_source: null,
      root: null,
      backend_ready: false,
      backend_mode: null
    },
    finished_at: null
  }
}

function completeResultWire(
  where: Fixture,
  request: RequestWire,
  processStartedAt: number,
  resultRequestedAt: number
): Record<string, unknown> {
  const pending = pendingResultWire(where, request, { processStartedAt, resultRequestedAt }) as any

  return {
    ...pending,
    state: 'complete',
    ok: true,
    exit_code: 0,
    message: 'Update complete.',
    relaunch: {
      ...pending.relaunch,
      state: 'acknowledged',
      acknowledged_at: NOW_SECONDS
    },
    desktop: {
      build_id: BUILD_ID,
      build_source: 'install-stamp',
      root: where.root,
      backend_ready: true,
      backend_mode: 'local'
    },
    finished_at: NOW_SECONDS
  }
}

function inspect(
  where: Fixture,
  processStartedAt: number,
  over: Partial<Parameters<typeof inspectHandoffRelaunchExit>[1]> = {}
) {
  return inspectHandoffRelaunchExit(where.home, {
    currentPid: PID,
    currentRoot: where.root,
    currentExecutable: where.executable,
    getProcessStartedAt: async () => processStartedAt,
    now: () => NOW_MS,
    ...over
  })
}

function gate(
  where: Fixture,
  nowMs = NOW_MS,
  authorization: Parameters<typeof hasHandoffRelaunchRequest>[1]['authorization'] = null
): boolean {
  return hasHandoffRelaunchRequest(where.home, {
    authorization,
    currentRoot: where.root,
    currentExecutable: where.executable,
    now: () => nowMs
  })
}

test('no relaunch request returns none', async () => {
  const where = fixture('none')

  assert.equal(gate(where), false)
  assert.deepEqual(await inspect(where, NOW_SECONDS - 10), { kind: 'none' })
})

test('the synchronous request gate blocks for artifacts and directory-read uncertainty', () => {
  const where = fixture('sync-gate')
  const requestFile = handoffRelaunchRequestPath(where.home, ATTEMPT_ID)
  fs.writeFileSync(requestFile, JSON.stringify(requestWire(where)))
  assert.equal(gate(where), true, 'an active strict request blocks')

  fs.writeFileSync(
    requestFile,
    JSON.stringify(requestWire(where, { requestedAt: NOW_SECONDS - 121, expiresAt: NOW_SECONDS - 1 }))
  )
  assert.equal(gate(where), false, 'an expired strict request cannot permanently park backends')

  fs.writeFileSync(requestFile, 'not-yet-readable JSON')
  assert.equal(gate(where), true, 'a malformed request blocks')

  fs.unlinkSync(requestFile)
  const originalReadDirectory = fs.readdirSync
  fs.readdirSync = ((candidate, ...args) => {
    if (path.resolve(String(candidate)) === path.resolve(where.home)) {
      const error = new Error('sharing violation')

      ;(error as NodeJS.ErrnoException).code = 'EACCES'
      throw error
    }

    return (originalReadDirectory as any)(candidate, ...args)
  }) as typeof fs.readdirSync

  try {
    assert.equal(gate(where), true)
  } finally {
    fs.readdirSync = originalReadDirectory
  }

  assert.equal(
    hasHandoffRelaunchRequest(path.join(where.home, 'missing-home'), {
      currentRoot: where.root,
      currentExecutable: where.executable,
      now: () => NOW_MS
    }),
    false
  )
})

test('a no-gap request recovery artifact blocks both the sync gate and async inspector', async () => {
  const where = fixture('cas-artifact')
  const artifact = `${handoffRelaunchRequestPath(where.home, ATTEMPT_ID)}.cas-shadow-999-${'a'.repeat(32)}`
  fs.writeFileSync(artifact, JSON.stringify(requestWire(where)))

  assert.equal(gate(where), true)
  assert.deepEqual(await inspect(where, NOW_SECONDS - 100), {
    kind: 'blocked',
    reason: 'request-recovery-artifact'
  })
})

test('an unrelated filename containing the CAS substring is not a recovery artifact', async () => {
  const where = fixture('cas-substring')
  const unrelated = `${handoffRelaunchRequestPath(where.home, ATTEMPT_ID)}.cas-not-a-generation.txt`
  fs.writeFileSync(unrelated, 'unrelated')

  assert.equal(gate(where), false)
  assert.deepEqual(await inspect(where, NOW_SECONDS - 100), { kind: 'none' })
})

test('an existing Desktop survivor publishes the exact quit ACK promptly', async () => {
  const where = fixture('survivor')
  const request = requestWire(where)
  const requestFile = writeRequest(where, request)

  const outcome = await inspect(where, request.requested_at - 100)

  assert.equal(outcome.kind, 'quit-acknowledged')

  if (outcome.kind !== 'quit-acknowledged') {
    return
  }

  assert.equal(outcome.attemptId, ATTEMPT_ID)
  assert.equal(outcome.ack.pid, PID)
  assert.equal(outcome.ack.processStartedAt, request.requested_at - 100)
  const ackFile = handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)
  const wire = JSON.parse(fs.readFileSync(ackFile, 'utf8'))
  assert.deepEqual(Object.keys(wire).sort(), [
    'acknowledged_at',
    'action',
    'attempt_id',
    'executable',
    'pid',
    'process_started_at',
    'root',
    'schema_version'
  ])
  assert.deepEqual(wire, {
    schema_version: 1,
    attempt_id: ATTEMPT_ID,
    pid: PID,
    process_started_at: request.requested_at - 100,
    root: where.root,
    executable: where.executable,
    acknowledged_at: NOW_SECONDS,
    action: 'quit'
  })
  assert.equal(fs.readFileSync(requestFile, 'utf8'), JSON.stringify(request), 'PowerShell owns request cleanup')
})

test('equivalent absolute wire spellings resolve to the same root and packaged executable', async () => {
  const where = fixture('canonical-wire-alias')

  const request = {
    ...requestWire(where),
    root: `${where.root}${path.sep}.`,
    executable: `${path.dirname(where.executable)}${path.sep}.${path.sep}${path.basename(where.executable)}`
  }

  writeRequest(where, request)

  const outcome = await inspect(where, request.requested_at - 100)

  assert.equal(outcome.kind, 'quit-acknowledged')

  if (outcome.kind !== 'quit-acknowledged') {
    return
  }

  assert.equal(outcome.ack.root, where.root)
  assert.equal(outcome.ack.executable, where.executable)
})

test('an exact new pending relaunch is authorized without publishing a quit ACK', async () => {
  const where = fixture('authorized-pending')
  const request = requestWire(where, { requestedAt: NOW_SECONDS - 60 })
  const processStartedAt = NOW_SECONDS - 1
  assert.equal(path.relative(where.root, where.executable).startsWith('..'), true)
  writeRequest(where, request)
  fs.writeFileSync(
    handoffResultPath(where.home),
    JSON.stringify(
      pendingResultWire(where, request, {
        processStartedAt,
        resultRequestedAt: NOW_SECONDS - 1
      })
    )
  )

  const outcome = await inspect(where, processStartedAt)

  assert.equal(outcome.kind, 'authorized-relaunch')

  if (outcome.kind !== 'authorized-relaunch') {return}
  assert.equal(outcome.attemptId, ATTEMPT_ID)
  assert.equal(outcome.resultState, 'pending')
  assert.equal(outcome.authorization.attemptId, ATTEMPT_ID)
  assert.equal(outcome.authorization.processStartedAt, processStartedAt)
  assert.match(outcome.authorization.requestFingerprint, /^[a-f0-9]{64}$/)
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)

  fs.unlinkSync(handoffResultPath(where.home))
  assert.equal(gate(where, NOW_MS, outcome.authorization), false)
  assert.deepEqual(await inspect(where, processStartedAt, { authorization: outcome.authorization }), {
    kind: 'authorized-relaunch',
    attemptId: ATTEMPT_ID,
    resultState: 'cached',
    authorization: outcome.authorization
  })
  assert.equal(
    fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)),
    false,
    'consuming terminal result before producer request cleanup must not make the exact relaunch quit'
  )

  writeRequest(where, requestWire(where, { attemptId: SECOND_ATTEMPT_ID }))
  assert.equal(gate(where, NOW_MS, outcome.authorization), true)
  assert.deepEqual(await inspect(where, processStartedAt, { authorization: outcome.authorization }), {
    kind: 'blocked',
    reason: 'multiple-active-requests'
  })
})

test('a different canonical pending executable cannot authorize the packaged Desktop', async () => {
  const where = fixture('pending-executable-mismatch')
  const request = requestWire(where)
  const processStartedAt = request.requested_at + 1
  const foreignExecutable = path.join(where.home, 'other-desktop.exe')
  fs.writeFileSync(foreignExecutable, 'other executable')
  writeRequest(where, request)
  const result = pendingResultWire(where, request, { processStartedAt }) as any
  result.relaunch.executable = fs.realpathSync.native(foreignExecutable)
  fs.writeFileSync(handoffResultPath(where.home), JSON.stringify(result))

  const outcome = await inspect(where, processStartedAt)

  assert.equal(outcome.kind, 'wait-for-result')
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)
})

test('an exact complete v2 result also authorizes the new relaunch', async () => {
  const where = fixture('authorized-complete')
  const request = requestWire(where, { requestedAt: NOW_SECONDS - 60 })
  const processStartedAt = NOW_SECONDS - 1
  writeRequest(where, request)
  fs.writeFileSync(
    handoffResultPath(where.home),
    JSON.stringify(completeResultWire(where, request, processStartedAt, NOW_SECONDS - 1))
  )

  const outcome = await inspect(where, processStartedAt)
  assert.equal(outcome.kind, 'authorized-relaunch')

  if (outcome.kind !== 'authorized-relaunch') {return}
  assert.equal(outcome.attemptId, ATTEMPT_ID)
  assert.equal(outcome.resultState, 'complete')
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)
})

test('a new process waits during bounded result-publication grace', async () => {
  const where = fixture('wait')
  const request = requestWire(where, { requestedAt: NOW_SECONDS - 60 })
  const processStartedAt = NOW_SECONDS - 1
  writeRequest(where, request)

  const outcome = await inspect(where, processStartedAt)

  assert.deepEqual(outcome, {
    kind: 'wait-for-result',
    attemptId: ATTEMPT_ID,
    retryAtMs: processStartedAt * 1_000 + HANDOFF_RELAUNCH_RESULT_PUBLICATION_GRACE_MS
  })
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)
})

test('a late manually started mismatch publishes a quit ACK after grace', async () => {
  const where = fixture('late-mismatch')
  const request = requestWire(where, { requestedAt: NOW_SECONDS - 10 })
  const processStartedAt = request.requested_at + 1
  writeRequest(where, request)
  fs.writeFileSync(
    handoffResultPath(where.home),
    JSON.stringify(pendingResultWire(where, request, { pid: PID + 1, processStartedAt }))
  )

  const outcome = await inspect(where, processStartedAt)

  assert.equal(outcome.kind, 'quit-acknowledged')
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), true)
})

test('a request rewrite before ACK blocks without mutating the request', async () => {
  const where = fixture('rewrite')
  const request = requestWire(where)
  const requestFile = writeRequest(where, request)
  const replacement = JSON.stringify({ ...request, expires_at: request.expires_at - 1 })
  const originalRead = fs.readFileSync
  let reads = 0

  fs.readFileSync = ((candidate, ...args) => {
    if (path.resolve(String(candidate)) === path.resolve(requestFile)) {
      reads += 1

      if (reads === 2) {
        fs.writeFileSync(requestFile, replacement)
      }
    }

    return (originalRead as any)(candidate, ...args)
  }) as typeof fs.readFileSync

  try {
    const outcome = await inspect(where, request.requested_at - 100)
    assert.deepEqual(outcome, { kind: 'blocked', reason: 'request-changed' })
  } finally {
    fs.readFileSync = originalRead
  }

  assert.equal(fs.readFileSync(requestFile, 'utf8'), replacement)
  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)
})

test('a second active request appearing before ACK makes the final rescan block', async () => {
  const where = fixture('second-request-race')
  const request = requestWire(where)
  writeRequest(where, request)
  const originalReadDirectory = fs.readdirSync
  let scans = 0

  fs.readdirSync = ((candidate, ...args) => {
    if (path.resolve(String(candidate)) === path.resolve(where.home)) {
      scans += 1

      if (scans === 2) {
        writeRequest(where, requestWire(where, { attemptId: SECOND_ATTEMPT_ID }))
      }
    }

    return (originalReadDirectory as any)(candidate, ...args)
  }) as typeof fs.readdirSync

  try {
    assert.deepEqual(await inspect(where, request.requested_at - 100), {
      kind: 'blocked',
      reason: 'request-changed'
    })
  } finally {
    fs.readdirSync = originalReadDirectory
  }

  assert.equal(fs.existsSync(handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)), false)
})

test('a slow result read refreshes the clock before applying publication grace', async () => {
  const where = fixture('slow-result-reader')
  const request = requestWire(where, { requestedAt: NOW_SECONDS - 60 })
  const processStartedAt = NOW_SECONDS - 1
  let clock = NOW_MS
  writeRequest(where, request)

  const outcome = await inspect(where, processStartedAt, {
    now: () => clock,
    readResult: async () => {
      clock += HANDOFF_RELAUNCH_RESULT_PUBLICATION_GRACE_MS + 1_000

      return null
    }
  })

  assert.equal(outcome.kind, 'quit-acknowledged')
})

test('malformed, invalid UTF-8, future, and unreadable active requests fail closed', async () => {
  const malformed = fixture('malformed')
  writeRequest(malformed, { ...requestWire(malformed), foreign: true })
  assert.equal((await inspect(malformed, NOW_SECONDS - 100)).kind, 'blocked')

  const invalidUtf8 = fixture('invalid-utf8')
  fs.writeFileSync(handoffRelaunchRequestPath(invalidUtf8.home, ATTEMPT_ID), Buffer.from([0xc3, 0x28]))
  assert.equal((await inspect(invalidUtf8, NOW_SECONDS - 100)).kind, 'blocked')

  const future = fixture('future')
  writeRequest(future, requestWire(future, { requestedAt: NOW_SECONDS + 6 }))
  assert.equal((await inspect(future, NOW_SECONDS - 100)).kind, 'blocked')

  const unreadable = fixture('unreadable')
  const unreadableFile = writeRequest(unreadable, requestWire(unreadable))
  const originalRead = fs.readFileSync
  fs.readFileSync = ((candidate, ...args) => {
    if (path.resolve(String(candidate)) === path.resolve(unreadableFile)) {
      const error = new Error('sharing violation')

      ;(error as NodeJS.ErrnoException).code = 'EACCES'
      throw error
    }

    return (originalRead as any)(candidate, ...args)
  }) as typeof fs.readFileSync

  try {
    assert.equal((await inspect(unreadable, NOW_SECONDS - 100)).kind, 'blocked')
  } finally {
    fs.readFileSync = originalRead
  }
})

test('request epochs, TTL, future skew, and capability boundaries are strict', async () => {
  for (const [tag, mutate] of [
    ['fractional-requested-at', (wire: any) => (wire.requested_at += 0.5)],
    ['fractional-expires-at', (wire: any) => (wire.expires_at += 0.5)],
    ['zero-ttl', (wire: any) => (wire.expires_at = wire.requested_at)],
    ['overlong-ttl', (wire: any) => (wire.expires_at = wire.requested_at + 121)]
  ] as const) {
    const where = fixture(tag)
    const wire: any = requestWire(where)
    mutate(wire)
    writeRequest(where, wire)
    assert.equal((await inspect(where, NOW_SECONDS - 100)).kind, 'blocked', tag)
  }

  const atSkewBoundary = fixture('future-skew-boundary')
  const boundaryRequest = requestWire(atSkewBoundary, { requestedAt: NOW_SECONDS + 5 })
  writeRequest(atSkewBoundary, boundaryRequest)
  assert.equal(
    (await inspect(atSkewBoundary, NOW_SECONDS - 100)).kind,
    'quit-acknowledged',
    'exactly five seconds of future skew remains valid'
  )

  assert.doesNotThrow(() => handoffRelaunchRequestPath(atSkewBoundary.home, 'a'.repeat(16)))
  assert.throws(() => handoffRelaunchRequestPath(atSkewBoundary.home, 'a'.repeat(15)), /attempt id/i)
  assert.throws(() => handoffRelaunchRequestPath(atSkewBoundary.home, 'a'.repeat(129)), /attempt id/i)

  const dottedCapability = fixture('dotted-capability')
  const dottedAttemptId = 'aaaaaaaaaaaaaaaa.json.cas-shadow-1-x'
  const dottedRequest = requestWire(dottedCapability, { attemptId: dottedAttemptId })
  writeRequest(dottedCapability, dottedRequest)
  assert.equal(gate(dottedCapability), true)
  assert.equal(
    (await inspect(dottedCapability, dottedRequest.requested_at - 100)).kind,
    'quit-acknowledged',
    'a legal capability containing the CAS delimiter remains a canonical request'
  )
})

test('expired requests are ignored while multiple active requests block', async () => {
  const expired = fixture('expired')

  const expiredRequest = requestWire(expired, {
    requestedAt: NOW_SECONDS - 121,
    expiresAt: NOW_SECONDS - 1
  })

  const expiredFile = writeRequest(expired, expiredRequest)
  assert.deepEqual(await inspect(expired, NOW_SECONDS - 200), { kind: 'none' })
  assert.equal(gate(expired), false)
  assert.equal(fs.existsSync(expiredFile), true, 'expired request cleanup remains producer-owned')

  const retiredExecutable = fixture('expired-retired-executable')
  const oldExecutable = path.join(retiredExecutable.home, 'retired-Hermes.exe')
  fs.writeFileSync(oldExecutable, 'retired executable')
  const canonicalOldExecutable = fs.realpathSync.native(oldExecutable)
  fs.unlinkSync(oldExecutable)

  const retiredRequest = {
    ...requestWire(retiredExecutable, {
      requestedAt: NOW_SECONDS - 121,
      expiresAt: NOW_SECONDS - 1
    }),
    executable: canonicalOldExecutable
  }

  writeRequest(retiredExecutable, retiredRequest)
  assert.equal(gate(retiredExecutable), false)
  assert.deepEqual(await inspect(retiredExecutable, NOW_SECONDS - 200), { kind: 'none' })

  const multiple = fixture('multiple')
  writeRequest(multiple, requestWire(multiple))
  writeRequest(multiple, requestWire(multiple, { attemptId: SECOND_ATTEMPT_ID }))
  assert.deepEqual(await inspect(multiple, NOW_SECONDS - 100), {
    kind: 'blocked',
    reason: 'multiple-active-requests'
  })
})

test('a foreign ACK winner is preserved and the process remains blocked', async () => {
  const where = fixture('foreign-ack')
  const request = requestWire(where)
  writeRequest(where, request)
  const ackFile = handoffRelaunchExitAckPath(where.home, ATTEMPT_ID, PID)
  const foreign = '{"foreign":true}'
  const originalLink = fs.linkSync

  fs.linkSync = ((existingPath, newPath) => {
    if (path.resolve(String(newPath)) === path.resolve(ackFile)) {
      fs.writeFileSync(ackFile, foreign)

      const error = new Error('foreign ACK already exists')

      ;(error as NodeJS.ErrnoException).code = 'EEXIST'
      throw error
    }

    return originalLink(existingPath, newPath)
  }) as typeof fs.linkSync

  try {
    const outcome = await inspect(where, request.requested_at - 100)
    assert.deepEqual(outcome, { kind: 'blocked', reason: 'ack-publish-failed' })
  } finally {
    fs.linkSync = originalLink
  }

  assert.equal(fs.readFileSync(ackFile, 'utf8'), foreign)
})
