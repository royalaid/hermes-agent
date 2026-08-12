import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test, vi } from 'vitest'

import {
  consumeLegacyHandoffResult,
  consumePosixHandoffResult,
  HANDOFF_RECEIPT_TO_RELAUNCH_MAX_AGE_MS,
  HANDOFF_RESULT_CLOCK_SKEW_MS,
  HANDOFF_RESULT_MAX_AGE_MS,
  handoffAckPath,
  handoffResultPath,
  readHandoffResult,
  waitForTerminalHandoffResult,
  writeHandoffAck
} from './handoff-result'

const NOW_SECONDS = 1_700_000_000
const NOW_MS = NOW_SECONDS * 1_000
const ATTEMPT_ID = 'attempt-id-1234567890'
const INVOCATION_ID = 'invocation-id-123456'
const LEASE_ID = 'bridge-lease-id-123456'
const PID = 4242
const PROCESS_STARTED_AT = NOW_SECONDS - 10
const REQUESTED_AT = NOW_SECONDS - 5
const BUILD_ID = 'a'.repeat(40)

const HEALTH = {
  critical_syntax: true,
  critical_imports: true,
  dependencies: true,
  node_dependencies: true
}

const EMPTY_DESKTOP = {
  build_id: null,
  build_source: null,
  root: null,
  backend_ready: false,
  backend_mode: null
}

interface Fixture {
  executable: string
  home: string
  root: string
}

function fixture(): Fixture {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'handoff-result-v2-'))
  const root = path.join(home, 'hermes-agent')
  const executable = path.join(root, 'Hermes.exe')
  fs.mkdirSync(root)
  fs.writeFileSync(executable, 'test executable')

  return {
    home,
    root: fs.realpathSync.native(root),
    executable: fs.realpathSync.native(executable)
  }
}

function receipt(root: string, over: Record<string, unknown> = {}): any {
  return {
    schema_version: 1,
    invocation_id: INVOCATION_ID,
    lease_id: LEASE_ID,
    mode: 'git',
    root,
    remote: 'origin',
    branch: 'main',
    target_ref: 'refs/remotes/origin/main',
    target_sha: BUILD_ID,
    resulting_head: BUILD_ID,
    archive_sha: null,
    timestamp: REQUESTED_AT - 2,
    success: true,
    gateway_resume_deferred: true,
    health: { ...HEALTH },
    ...over
  }
}

function pendingResult({ root, executable }: Fixture, over: Record<string, unknown> = {}): any {
  return {
    schema_version: 2,
    attempt_id: ATTEMPT_ID,
    state: 'pending',
    ok: false,
    exit_code: null,
    message: 'Update applied; awaiting Desktop acknowledgement.',
    branch: 'main',
    invocation_id: INVOCATION_ID,
    lease_id: LEASE_ID,
    root,
    receipt: receipt(root),
    cleanup: { update_marker_released: true, bridge_lease_released: true },
    runtime_health: { ...HEALTH },
    relaunch: {
      state: 'pending',
      pid: PID,
      process_started_at: PROCESS_STARTED_AT,
      executable,
      requested_at: REQUESTED_AT,
      acknowledged_at: null
    },
    desktop: { ...EMPTY_DESKTOP },
    finished_at: null,
    ...over
  }
}

function completeResult(where: Fixture, over: Record<string, unknown> = {}): any {
  return pendingResult(where, {
    state: 'complete',
    ok: true,
    exit_code: 0,
    message: 'Update complete.',
    relaunch: {
      state: 'acknowledged',
      pid: PID,
      process_started_at: PROCESS_STARTED_AT,
      executable: where.executable,
      requested_at: REQUESTED_AT,
      acknowledged_at: NOW_SECONDS - 1
    },
    desktop: {
      build_id: BUILD_ID,
      build_source: 'install-stamp',
      root: where.root,
      backend_ready: true,
      backend_mode: 'local'
    },
    finished_at: NOW_SECONDS,
    ...over
  })
}

function failedResult(where: Fixture, over: Record<string, unknown> = {}): any {
  return pendingResult(where, {
    state: 'failed',
    ok: false,
    exit_code: 8,
    message: 'Desktop relaunch failed before spawn.',
    invocation_id: null,
    lease_id: null,
    receipt: null,
    cleanup: { update_marker_released: false, bridge_lease_released: false },
    runtime_health: null,
    relaunch: {
      state: 'failed',
      pid: null,
      process_started_at: null,
      executable: null,
      requested_at: REQUESTED_AT,
      acknowledged_at: null
    },
    desktop: { ...EMPTY_DESKTOP },
    finished_at: NOW_SECONDS,
    ...over
  })
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function writeResult(home: string, body: unknown): void {
  fs.writeFileSync(handoffResultPath(home), typeof body === 'string' ? body : JSON.stringify(body))
}

function legacyResult(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: false,
    exit_code: 6,
    message: 'legacy update failed',
    branch: 'main',
    finished_at: NOW_SECONDS,
    ...over
  }
}

function posixResult(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: true,
    exit_code: 0,
    manual: false,
    message: 'POSIX update complete',
    branch: 'main',
    finished_at: NOW_SECONDS,
    ...over
  }
}

function writeLegacy(home: string, body: unknown | Buffer): Buffer {
  const bytes = Buffer.isBuffer(body)
    ? body
    : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8')

  fs.writeFileSync(handoffResultPath(home), bytes)

  return bytes
}

function read(home: string, root: string) {
  return readHandoffResult(home, { expectedRoot: root, now: () => NOW_MS })
}

function ackProof(where: Fixture, over: Record<string, unknown> = {}) {
  return {
    currentPid: PID,
    processStartedAt: PROCESS_STARTED_AT,
    currentRoot: where.root,
    currentExecutable: where.executable,
    buildId: BUILD_ID,
    buildSource: 'install-stamp' as const,
    backendReady: true as const,
    backendMode: 'local' as const,
    now: () => NOW_MS,
    ...over
  }
}

test('reads strict pending state without consuming it and exposes immutable receipt identity', () => {
  const where = fixture()
  writeResult(where.home, pendingResult(where))

  const first = read(where.home, where.root)
  const second = read(where.home, where.root)

  assert.ok(first)
  assert.equal(first.state, 'pending')
  assert.equal(first.ok, false)
  assert.equal(first.exitCode, null)
  assert.equal(first.receipt?.mode, 'git')
  assert.equal(first.receipt?.resultingHead, BUILD_ID)
  assert.equal(first.receipt?.archiveSha, null)
  assert.equal(first.receipt?.gatewayResumeDeferred, true)
  assert.equal(first.relaunch.processStartedAt, PROCESS_STARTED_AT)
  assert.deepEqual(second, first)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
})

test('accepts archive receipt identity and exposes its immutable archive digest', () => {
  const where = fixture()
  const archiveSha = 'b'.repeat(64)
  writeResult(
    where.home,
    pendingResult(where, {
      receipt: receipt(where.root, {
        mode: 'archive',
        remote: null,
        target_ref: null,
        target_sha: null,
        resulting_head: null,
        archive_sha: archiveSha
      })
    })
  )

  const parsed = read(where.home, where.root)
  assert.ok(parsed)
  assert.equal(parsed.receipt?.mode, 'archive')
  assert.equal(parsed.receipt?.resultingHead, null)
  assert.equal(parsed.receipt?.archiveSha, archiveSha)
})

test('accepts a receipt that spans the supported rebuild and recovery budgets', () => {
  const where = fixture()
  const value = pendingResult(where)

  // The receipt is published before a rebuild that may take 30 minutes,
  // followed by a five-minute gateway recovery and one-minute relaunch handoff.
  value.receipt.timestamp = REQUESTED_AT - 36 * 60
  writeResult(where.home, value)

  assert.ok(read(where.home, where.root))
})

test('rejects genuinely stale or future receipts while result freshness stays independent', () => {
  const invalidReceiptTimestamps = [
    REQUESTED_AT - HANDOFF_RECEIPT_TO_RELAUNCH_MAX_AGE_MS / 1_000 - 1,
    REQUESTED_AT + HANDOFF_RESULT_CLOCK_SKEW_MS / 1_000 + 1
  ]

  for (const timestamp of invalidReceiptTimestamps) {
    const where = fixture()
    const value = pendingResult(where)
    value.receipt.timestamp = timestamp
    writeResult(where.home, value)

    assert.equal(read(where.home, where.root), null)
  }

  const staleResult = fixture()
  const staleRequestedAt = NOW_SECONDS - HANDOFF_RESULT_MAX_AGE_MS / 1_000 - 1
  const value = pendingResult(staleResult)
  value.relaunch.requested_at = staleRequestedAt
  value.relaunch.process_started_at = staleRequestedAt - 1
  value.receipt.timestamp = staleRequestedAt
  writeResult(staleResult.home, value)

  assert.equal(read(staleResult.home, staleResult.root), null)
})

test('rejects missing, extra, and wrongly typed result or nested keys without consuming', () => {
  const cases: Array<(value: any) => void> = [
    value => {
      delete value.attempt_id
    },
    value => {
      value.extra = true
    },
    value => {
      value.ok = 0
    },
    value => {
      value.cleanup.extra = false
    },
    value => {
      delete value.runtime_health.dependencies
    },
    value => {
      value.relaunch.extra = null
    },
    value => {
      value.desktop.backend_ready = 'true'
    },
    value => {
      value.receipt.extra = null
    },
    value => {
      delete value.receipt.gateway_resume_deferred
    },
    value => {
      value.receipt.gateway_resume_deferred = false
    }
  ]

  for (const mutate of cases) {
    const where = fixture()
    const value = pendingResult(where)
    mutate(value)
    writeResult(where.home, value)
    assert.equal(read(where.home, where.root), null)
    assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
  }
})

test('enforces pending, complete, and failed relational state tables', () => {
  const invalidCases: Array<(where: Fixture) => Record<string, unknown>> = [
    where => pendingResult(where, { ok: true }),
    where => pendingResult(where, { exit_code: 0 }),
    where => pendingResult(where, { finished_at: NOW_SECONDS }),
    where => pendingResult(where, { receipt: null }),
    where => pendingResult(where, { cleanup: { update_marker_released: false, bridge_lease_released: true } }),
    where => pendingResult(where, { desktop: { ...EMPTY_DESKTOP, backend_ready: true } }),
    where => pendingResult(where, { relaunch: { ...pendingResult(where).relaunch, state: 'acknowledged' } }),
    where => completeResult(where, { ok: false }),
    where => completeResult(where, { finished_at: null }),
    where => completeResult(where, { relaunch: { ...completeResult(where).relaunch, state: 'pending' } }),
    where => completeResult(where, { desktop: { ...completeResult(where).desktop, build_id: 'c'.repeat(40) } }),
    where => completeResult(where, { desktop: { ...completeResult(where).desktop, backend_ready: false } }),
    where => failedResult(where, { ok: true }),
    where => failedResult(where, { exit_code: 0 }),
    where => failedResult(where, { finished_at: null }),
    where => failedResult(where, { invocation_id: INVOCATION_ID })
  ]

  for (const makeValue of invalidCases) {
    const where = fixture()
    writeResult(where.home, makeValue(where))
    assert.equal(read(where.home, where.root), null)
  }

  const preSpawn = fixture()
  writeResult(preSpawn.home, failedResult(preSpawn))
  assert.equal(read(preSpawn.home, preSpawn.root)?.state, 'failed')

  const failedMutationRelaunch = fixture()
  writeResult(
    failedMutationRelaunch.home,
    failedResult(failedMutationRelaunch, {
      relaunch: {
        state: 'failed',
        pid: PID,
        process_started_at: PROCESS_STARTED_AT,
        executable: failedMutationRelaunch.executable,
        requested_at: REQUESTED_AT,
        acknowledged_at: null
      }
    })
  )
  assert.equal(
    read(failedMutationRelaunch.home, failedMutationRelaunch.root)?.state,
    'failed',
    'a real relaunch after a pre-receipt mutation failure must not be fabricated away'
  )

  const postSpawn = fixture()
  writeResult(
    postSpawn.home,
    failedResult(postSpawn, {
      message: 'Desktop readiness acknowledgement timed out.',
      invocation_id: INVOCATION_ID,
      lease_id: LEASE_ID,
      receipt: receipt(postSpawn.root),
      cleanup: { update_marker_released: true, bridge_lease_released: true },
      runtime_health: { ...HEALTH },
      relaunch: {
        state: 'failed',
        pid: PID,
        process_started_at: PROCESS_STARTED_AT,
        executable: postSpawn.executable,
        requested_at: REQUESTED_AT,
        acknowledged_at: null
      }
    })
  )
  assert.equal(read(postSpawn.home, postSpawn.root)?.state, 'failed')

  const unhealthyDesktop = fixture()
  writeResult(
    unhealthyDesktop.home,
    failedResult(unhealthyDesktop, {
      message: 'Relaunched Desktop backend did not become ready.',
      invocation_id: INVOCATION_ID,
      lease_id: LEASE_ID,
      receipt: receipt(unhealthyDesktop.root),
      cleanup: { update_marker_released: true, bridge_lease_released: true },
      runtime_health: { ...HEALTH },
      relaunch: {
        state: 'failed',
        pid: PID,
        process_started_at: PROCESS_STARTED_AT,
        executable: unhealthyDesktop.executable,
        requested_at: REQUESTED_AT,
        acknowledged_at: null
      },
      desktop: {
        build_id: BUILD_ID,
        build_source: 'install-stamp',
        root: unhealthyDesktop.root,
        backend_ready: false,
        backend_mode: 'local'
      }
    })
  )
  assert.equal(read(unhealthyDesktop.home, unhealthyDesktop.root)?.state, 'failed')

  for (const desktop of [
    {
      build_id: 'd'.repeat(40),
      build_source: 'install-stamp',
      root: unhealthyDesktop.root,
      backend_ready: false,
      backend_mode: 'local'
    },
    {
      build_id: BUILD_ID,
      build_source: 'install-stamp',
      root: null,
      backend_ready: false,
      backend_mode: 'local'
    }
  ]) {
    writeResult(
      unhealthyDesktop.home,
      failedResult(unhealthyDesktop, {
        invocation_id: INVOCATION_ID,
        lease_id: LEASE_ID,
        receipt: receipt(unhealthyDesktop.root),
        runtime_health: { ...HEALTH },
        relaunch: {
          state: 'failed',
          pid: PID,
          process_started_at: PROCESS_STARTED_AT,
          executable: unhealthyDesktop.executable,
          requested_at: REQUESTED_AT,
          acknowledged_at: null
        },
        desktop
      })
    )
    assert.equal(read(unhealthyDesktop.home, unhealthyDesktop.root), null)
  }
})

test('rejects wrong canonical roots and mismatched process-start identity', () => {
  const where = fixture()
  const other = fixture()

  writeResult(where.home, pendingResult(where))
  assert.equal(read(where.home, other.root), null)

  writeResult(
    where.home,
    pendingResult(where, {
      relaunch: { ...pendingResult(where).relaunch, process_started_at: null }
    })
  )
  assert.equal(read(where.home, where.root), null)
})

test('writes an exact correlated acknowledgement at the attempt-scoped path', () => {
  const where = fixture()
  writeResult(where.home, pendingResult(where))
  const pending = read(where.home, where.root)
  assert.ok(pending)

  const ack = writeHandoffAck(where.home, pending, ackProof(where))
  assert.ok(ack)
  assert.equal(ack.processStartedAt, PROCESS_STARTED_AT)
  assert.equal(ack.buildId, BUILD_ID)

  const raw = JSON.parse(fs.readFileSync(handoffAckPath(where.home, ATTEMPT_ID), 'utf8'))
  assert.deepEqual(Object.keys(raw).sort(), [
    'acknowledged_at',
    'attempt_id',
    'backend_mode',
    'backend_ready',
    'build_id',
    'build_source',
    'error',
    'executable',
    'invocation_id',
    'lease_id',
    'pid',
    'process_started_at',
    'root',
    'schema_version'
  ])
  assert.equal(raw.attempt_id, ATTEMPT_ID)
  assert.equal(raw.pid, PID)
  assert.equal(raw.process_started_at, PROCESS_STARTED_AT)
  assert.equal(raw.build_source, 'install-stamp')
  assert.equal(raw.backend_ready, true)
  assert.equal(raw.error, null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), true, 'ACK must not consume pending result')
})

test('refuses ACK publication when live identity, build, or readiness proof mismatches pending', () => {
  const mutations = [
    { currentPid: PID + 1 },
    { processStartedAt: PROCESS_STARTED_AT + 1 },
    { currentRoot: fixture().root },
    { currentExecutable: fixture().executable },
    { buildId: 'd'.repeat(40) },
    { buildSource: null },
    { backendReady: false },
    { backendMode: null }
  ]

  for (const mutation of mutations) {
    const where = fixture()
    writeResult(where.home, pendingResult(where))
    const pending = read(where.home, where.root)
    assert.ok(pending)
    assert.equal(writeHandoffAck(where.home, pending, ackProof(where, mutation) as any), null)
    assert.equal(fs.existsSync(handoffAckPath(where.home, ATTEMPT_ID)), false)
  }
})

test('ACK publication is atomic and exclusive instead of overwriting a prior proof', () => {
  const where = fixture()
  writeResult(where.home, pendingResult(where))
  const pending = read(where.home, where.root)
  assert.ok(pending)

  const first = writeHandoffAck(where.home, pending, ackProof(where))
  const ackPath = handoffAckPath(where.home, ATTEMPT_ID)
  const firstBytes = fs.readFileSync(ackPath)

  const second = writeHandoffAck(
    where.home,
    pending,
    ackProof(where, { backendMode: 'remote', now: () => NOW_MS + 1_000 })
  )

  assert.ok(first)
  assert.equal(second, null)
  assert.deepEqual(fs.readFileSync(ackPath), firstBytes)
  assert.equal(
    fs.readdirSync(where.home).some(name => name.includes('.ack-tmp-')),
    false
  )
})

test('bounded poll preserves pending, then consumes only its delayed correlated terminal result', async () => {
  const where = fixture()
  writeResult(where.home, pendingResult(where))
  const waits: number[] = []

  const terminal = await waitForTerminalHandoffResult(where.home, {
    attemptId: ATTEMPT_ID,
    invocationId: INVOCATION_ID,
    leaseId: LEASE_ID,
    expectedRoot: where.root,
    now: () => NOW_MS,
    pollMs: 100,
    timeoutMs: 500,
    wait: async delayMs => {
      waits.push(delayMs)
      assert.equal(read(where.home, where.root)?.state, 'pending')
      assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
      writeResult(where.home, completeResult(where))
    }
  })

  assert.ok(terminal)
  assert.equal(terminal.state, 'complete')
  assert.equal(terminal.ok, true)
  assert.deepEqual(waits, [100])
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('pending survives a bounded terminal poll timeout', async () => {
  const where = fixture()
  writeResult(where.home, pendingResult(where))
  const waits: number[] = []

  const terminal = await waitForTerminalHandoffResult(where.home, {
    attemptId: ATTEMPT_ID,
    expectedRoot: where.root,
    now: () => NOW_MS,
    pollMs: 100,
    timeoutMs: 250,
    wait: async delayMs => {
      waits.push(delayMs)
    }
  })

  assert.equal(terminal, null)
  assert.deepEqual(waits, [100, 100, 50])
  assert.equal(read(where.home, where.root)?.state, 'pending')
  assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
})

test('terminal polling leaves malformed and differently correlated records untouched', async () => {
  for (const body of [
    { ...completeResult(fixture()), extra: true },
    completeResult(fixture(), { attempt_id: 'different-attempt-123456' })
  ]) {
    const where = fixture()
    const normalized: any = clone(body)
    normalized.root = where.root

    if (normalized.receipt && typeof normalized.receipt === 'object') {
      normalized.receipt.root = where.root
    }

    if (normalized.relaunch && typeof normalized.relaunch === 'object') {
      normalized.relaunch.executable = where.executable
    }

    if (normalized.desktop && typeof normalized.desktop === 'object') {
      normalized.desktop.root = where.root
    }
    writeResult(where.home, normalized)

    const terminal = await waitForTerminalHandoffResult(where.home, {
      attemptId: ATTEMPT_ID,
      expectedRoot: where.root,
      now: () => NOW_MS,
      timeoutMs: 0
    })

    assert.equal(terminal, null)
    assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
  }
})

test('valid terminal failure is consumed once without fabricating success', async () => {
  const where = fixture()
  writeResult(where.home, failedResult(where))

  const terminal = await waitForTerminalHandoffResult(where.home, {
    expectedRoot: where.root,
    now: () => NOW_MS,
    timeoutMs: 0
  })

  assert.ok(terminal)
  assert.equal(terminal.state, 'failed')
  assert.equal(terminal.ok, false)
  assert.equal(terminal.receipt, null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('consumes the exact PowerShell receipt-bearing pre-spawn failure fixture', () => {
  const where = fixture()
  const fixturePath = path.resolve(
    import.meta.dirname,
    '..',
    '..',
    '..',
    'scripts',
    'tests',
    'fixtures',
    'desktop-update-receipt-failure.json'
  )
  const value = JSON.parse(fs.readFileSync(fixturePath, 'utf8'))
  value.root = where.root
  value.receipt.root = where.root
  writeResult(where.home, value)

  const parsed = read(where.home, where.root)

  assert.ok(parsed)
  assert.equal(parsed.state, 'failed')
  assert.equal(parsed.receipt?.timestamp, value.receipt.timestamp)
  assert.ok(parsed.relaunch.requestedAt >= (parsed.receipt?.timestamp ?? 0))
  assert.equal(parsed.relaunch.pid, null)
  assert.equal(parsed.finishedAt, parsed.relaunch.requestedAt)
})

test('legacy v0 fresh failure is returned as a diagnostic and consumed exactly once', () => {
  const where = fixture()
  writeLegacy(where.home, legacyResult())

  const diagnostic = consumeLegacyHandoffResult(where.home, { now: () => NOW_MS })

  assert.deepEqual(diagnostic, {
    exitCode: 6,
    message: 'legacy update failed',
    branch: 'main'
  })
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
  assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
})

test('legacy v0 success is retired silently and can never become v2 success', () => {
  const where = fixture()
  writeLegacy(where.home, legacyResult({ ok: true, exit_code: 0, message: 'legacy done' }))

  assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('legacy v0 stale and future records are consumed without surfacing diagnostics', () => {
  for (const finishedAt of [NOW_SECONDS - 3_600, NOW_SECONDS + 6]) {
    const where = fixture()
    writeLegacy(where.home, legacyResult({ finished_at: finishedAt }))

    assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
    assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
  }
})

test('legacy v0 malformed, extra, wrongly typed, and relationally invalid bytes are preserved', () => {
  const cases: Array<unknown | Buffer> = [
    Buffer.from('{nope', 'utf8'),
    Buffer.from([0x7b, 0x22, 0xff, 0x22, 0x3a, 0x31, 0x7d]),
    legacyResult({ extra: true }),
    legacyResult({ ok: 'false' }),
    legacyResult({ exit_code: 1.5 }),
    legacyResult({ finished_at: 0 }),
    legacyResult({ ok: true, exit_code: 9 }),
    legacyResult({ ok: false, exit_code: 0 })
  ]

  for (const body of cases) {
    const where = fixture()
    const original = writeLegacy(where.home, body)

    assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
    assert.deepEqual(fs.readFileSync(handoffResultPath(where.home)), original)
  }
})

test('legacy v0 consumer restores an exact foreign replacement raced before isolation', () => {
  const where = fixture()
  writeLegacy(where.home, legacyResult())
  const foreign = Buffer.from('{"foreign":"replacement"}', 'utf8')
  const originalRename = fs.renameSync.bind(fs)

  const rename = vi.spyOn(fs, 'renameSync').mockImplementationOnce((source, destination) => {
    fs.writeFileSync(source, foreign)
    originalRename(source, destination)
  })

  try {
    assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
  } finally {
    rename.mockRestore()
  }

  assert.deepEqual(fs.readFileSync(handoffResultPath(where.home)), foreign)
  assert.equal(
    fs.readdirSync(where.home).some(name => name.includes('.consume-')),
    false
  )
})

test('Windows leaves six-key success and manual results for fail-closed legacy retirement', () => {
  for (const manual of [false, true]) {
    const where = fixture()
    writeLegacy(where.home, posixResult({ manual, message: manual ? 'Reopen Hermes to finish' : 'done' }))

    assert.equal(
      consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'win32' }),
      null
    )
    assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
    assert.equal(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), null)
    assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
  }
})

test('Windows surfaces only a fresh six-key failure as a legacy diagnostic', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult({ ok: false, exit_code: 7, message: 'old Windows update failed' }))

  assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'win32' }), null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), true)
  assert.deepEqual(consumeLegacyHandoffResult(where.home, { now: () => NOW_MS }), {
    exitCode: 7,
    message: 'old Windows update failed',
    branch: 'main'
  })
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('exact fresh POSIX success is returned and consumed once without becoming v2', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult())

  assert.deepEqual(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), {
    ok: true,
    exitCode: 0,
    manual: false,
    message: 'POSIX update complete',
    branch: 'main'
  })
  assert.equal(readHandoffResult(where.home, { now: () => NOW_MS }), null)
  assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
})

test('exact fresh POSIX failure is returned and consumed once', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult({ ok: false, exit_code: 7, message: 'POSIX update failed' }))

  assert.deepEqual(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), {
    ok: false,
    exitCode: 7,
    manual: false,
    message: 'POSIX update failed',
    branch: 'main'
  })
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('stale ordinary POSIX result is retired without surfacing', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult({ finished_at: NOW_SECONDS - 3_600 }))

  assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('stale POSIX manual result remains actionable and is consumed once', () => {
  const where = fixture()
  writeLegacy(
    where.home,
    posixResult({
      manual: true,
      message: 'Reopen Hermes to finish',
      finished_at: NOW_SECONDS - 3_600
    })
  )

  assert.deepEqual(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), {
    ok: true,
    exitCode: 0,
    manual: true,
    message: 'Reopen Hermes to finish',
    branch: 'main'
  })
  assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
})

test('invalid or extended POSIX-shaped bytes remain for another consumer', () => {
  const v2Shaped = completeResult(fixture())

  for (const body of [
    posixResult({ manual: 'true' }),
    posixResult({ ok: false, exit_code: 7, manual: true }),
    posixResult({ extra: true }),
    { ...v2Shaped, manual: false }
  ]) {
    const where = fixture()
    const original = writeLegacy(where.home, body)

    assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
    assert.deepEqual(fs.readFileSync(handoffResultPath(where.home)), original)
  }
})

test('future POSIX result outside clock skew is consumed without surfacing', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult({ manual: true, finished_at: NOW_SECONDS + 6 }))

  assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
  assert.equal(fs.existsSync(handoffResultPath(where.home)), false)
})

test('POSIX consumer restores a foreign replacement raced before isolation', () => {
  const where = fixture()
  writeLegacy(where.home, posixResult())
  const foreign = Buffer.from('{"foreign":"replacement"}', 'utf8')
  const originalRename = fs.renameSync.bind(fs)
  const rename = vi.spyOn(fs, 'renameSync').mockImplementationOnce((source, destination) => {
    fs.writeFileSync(source, foreign)
    originalRename(source, destination)
  })

  try {
    assert.equal(consumePosixHandoffResult(where.home, { now: () => NOW_MS, platform: 'linux' }), null)
  } finally {
    rename.mockRestore()
  }

  assert.deepEqual(fs.readFileSync(handoffResultPath(where.home)), foreign)
  assert.equal(
    fs.readdirSync(where.home).some(name => name.includes('.consume-')),
    false
  )
})
