import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, it } from 'vitest'

import {
  acquireMcpBridgeQuiesceLease,
  clearMcpBridgeQuiesceLease,
  handOffMcpBridgeLeaseToStagedUpdater,
  markMcpBridgeQuiesceLeaseForHandoff,
  mcpBridgeQuiesceMarkerPath,
  pruneInactiveMcpBridgeQuiesceLease,
  readMcpBridgeQuiesceLease,
  revokeMcpBridgeQuiesceLease,
  transferMcpBridgeQuiesceLease,
  waitForMcpBridgeQuiesceLeaseAdoption
} from './mcp-bridge-quiesce'
import type { McpBridgeQuiesceLease } from './mcp-bridge-quiesce'

function sandbox() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-quiesce-'))
  const root = path.join(home, 'install')
  fs.mkdirSync(root)

  return { home, root }
}

function cleanupSandbox(home: string): void {
  const waitBuffer = new Int32Array(new SharedArrayBuffer(4))

  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      fs.rmSync(home, { recursive: true, force: true })
      return
    } catch (error: any) {
      if (error?.code !== 'EPERM' && error?.code !== 'EBUSY' && error?.code !== 'ENOTEMPTY') {
        throw error
      }
      Atomics.wait(waitBuffer, 0, 0, 25)
    }
  }

  // On Windows an antivirus/indexer can retain the now-empty directory handle
  // until the Electron worker exits. The test data itself is already gone, so
  // do not turn that external handle race into a product assertion failure.
  try {
    if (fs.readdirSync(home).length === 0) {
      return
    }
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return
    }
  }

  // The sandbox is unique per test, so a handle retained until worker exit
  // cannot contaminate another assertion. Best-effort cleanup is sufficient
  // after the bounded retries above.
}

function leaseRecordBytes(lease: McpBridgeQuiesceLease): Buffer {
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

function writeLeaseRecord(home: string, lease: McpBridgeQuiesceLease): void {
  fs.writeFileSync(mcpBridgeQuiesceMarkerPath(home), leaseRecordBytes(lease))
}

function leaseRecordWithInvalidRootByte(lease: McpBridgeQuiesceLease, byte: number): Buffer {
  const raw = leaseRecordBytes(lease)
  const rootBasename = Buffer.from(path.basename(lease.installRoot), 'utf8')
  const offset = raw.lastIndexOf(rootBasename)
  assert.notEqual(offset, -1)
  raw[offset] = byte

  return raw
}

function markerCasArtifacts(home: string): string[] {
  const marker = mcpBridgeQuiesceMarkerPath(home)
  const prefix = `${path.basename(marker)}.cas-`

  return fs
    .readdirSync(path.dirname(marker))
    .filter(name => name.startsWith(prefix))
    .map(name => path.join(path.dirname(marker), name))
}

function updateOwner(pid: number, startedAt = 99_999): { pid: number; startedAt: number } {
  return { pid, startedAt }
}

describe('MCP bridge quiesce lease', () => {
  it('consumes the shared cross-runtime v1 fixture', () => {
    const { home, root } = sandbox()

    try {
      const fixturePath = path.resolve(
        import.meta.dirname,
        '..',
        '..',
        '..',
        'scripts',
        'tests',
        'fixtures',
        'desktop-update-bridge-lease.json'
      )

      const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'))
      fixture.install_root = fs.realpathSync.native(root)
      fs.writeFileSync(mcpBridgeQuiesceMarkerPath(home), `${JSON.stringify(fixture)}\n`, 'utf8')

      assert.deepEqual(Object.keys(fixture).sort(), [
        'created_at',
        'expires_at',
        'handoff_grace_until',
        'install_root',
        'lease_id',
        'owner_pid',
        'schema_version'
      ])
      assert.deepEqual(readMcpBridgeQuiesceLease(home), {
        schemaVersion: 1,
        leaseId: 'fixture-lease-id',
        ownerPid: 4242,
        createdAt: 1_700_000_000,
        expiresAt: 1_700_001_200,
        handoffGraceUntil: 1_700_000_090,
        installRoot: fs.realpathSync.native(root)
      })
    } finally {
      cleanupSandbox(home)
    }
  })

  it('rejects schema-v1 records with extra keys or 91 seconds of handoff grace', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)

    try {
      const base = {
        schema_version: 1,
        lease_id: 'strict-schema-lease-123',
        owner_pid: 4242,
        created_at: 100_000,
        expires_at: 101_200,
        handoff_grace_until: 100_090,
        install_root: fs.realpathSync.native(root)
      }

      fs.writeFileSync(marker, `${JSON.stringify({ ...base, unexpected: true })}\n`, 'utf8')
      assert.equal(readMcpBridgeQuiesceLease(home), null)

      fs.writeFileSync(marker, `${JSON.stringify({ ...base, handoff_grace_until: 100_091 })}\n`, 'utf8')
      assert.equal(readMcpBridgeQuiesceLease(home), null)

      fs.writeFileSync(marker, `${JSON.stringify(base)}\n`, 'utf8')
      assert.equal(readMcpBridgeQuiesceLease(home)?.handoffGraceUntil, 100_090)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('fails closed instead of replacement-decoding invalid UTF-8 lease bytes', () => {
    const { home, root } = sandbox()

    try {
      const lease: McpBridgeQuiesceLease = {
        schemaVersion: 1,
        leaseId: 'invalid-utf8-lease-123',
        ownerPid: 321,
        createdAt: 100_000,
        expiresAt: 101_200,
        handoffGraceUntil: 100_090,
        installRoot: fs.realpathSync.native(root)
      }

      fs.writeFileSync(mcpBridgeQuiesceMarkerPath(home), leaseRecordWithInvalidRootByte(lease, 0x80))

      assert.equal(readMcpBridgeQuiesceLease(home), null)
      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => false,
          now: () => 100_100,
          ownerPid: 654,
          randomId: () => 'replacement-lease-12345'
        }),
        null
      )
    } finally {
      cleanupSandbox(home)
    }
  })

  it('distinguishes invalid UTF-8 bytes during stale-marker CAS cleanup', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)
    const originalRename = fs.renameSync

    try {
      const stale: McpBridgeQuiesceLease = {
        schemaVersion: 1,
        leaseId: 'invalid-utf8-stale-123',
        ownerPid: 321,
        createdAt: 100_000,
        expiresAt: 101_200,
        handoffGraceUntil: 100_090,
        installRoot: fs.realpathSync.native(root)
      }

      const first = leaseRecordWithInvalidRootByte(stale, 0x80)
      const foreign = leaseRecordWithInvalidRootByte(stale, 0x81)
      assert.equal(first.toString('utf8'), foreign.toString('utf8'))
      assert.equal(first.equals(foreign), false)
      fs.writeFileSync(marker, first)
      fs.utimesSync(marker, new Date(0), new Date(0))

      let injected = false
      fs.renameSync = ((source, destination) => {
        if (!injected && source === marker && String(destination).includes('.cas-displaced-')) {
          injected = true
          fs.writeFileSync(marker, foreign)
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => false,
          now: () => 102_000,
          ownerPid: 654,
          randomId: () => 'replacement-lease-12345'
        }),
        null
      )
      assert.equal(fs.readFileSync(marker).equals(foreign), true)
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('writes the cross-runtime JSON schema with canonical install ownership', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'unguessable-token-123'
      })

      assert.ok(lease)
      assert.equal(lease.leaseId, 'unguessable-token-123')
      assert.equal(lease.expiresAt, 101_200)
      assert.equal(lease.handoffGraceUntil, 100_000)
      assert.equal(lease.installRoot, fs.realpathSync.native(root))

      const raw = JSON.parse(fs.readFileSync(mcpBridgeQuiesceMarkerPath(home), 'utf8'))
      assert.deepEqual(raw, {
        schema_version: 1,
        lease_id: 'unguessable-token-123',
        owner_pid: 321,
        created_at: 100_000,
        expires_at: 101_200,
        handoff_grace_until: 100_000,
        install_root: fs.realpathSync.native(root)
      })
    } finally {
      cleanupSandbox(home)
    }
  })

  it('renews handoff grace by lease id without transferring ownership to the cmd wrapper', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      const handedOff = markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 })
      assert.ok(handedOff)
      assert.equal(handedOff.ownerPid, 321)
      assert.equal(handedOff.createdAt, 100_010)
      assert.equal(handedOff.expiresAt, 101_210)
      assert.equal(handedOff.handoffGraceUntil, 100_100)
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, 'lease-a-unguessable')
    } finally {
      cleanupSandbox(home)
    }
  })

  it('starts the final 90-second handoff grant only after a long preflight finishes', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'old-preflight-lease-123'
      })

      assert.ok(lease)
      assert.equal(readMcpBridgeQuiesceLease(home)?.createdAt, 100_000)

      const handedOff = markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 101_190 })
      assert.ok(handedOff)
      assert.equal(handedOff.createdAt, 101_190)
      assert.equal(handedOff.expiresAt, 102_390)
      assert.equal(handedOff.handoffGraceUntil, 101_280)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('never renews or clears a lease owned by another transaction', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const impostor = { ...lease, leaseId: 'lease-b-unguessable' }

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, impostor, { now: () => 100_010 }), null)
      assert.equal(clearMcpBridgeQuiesceLease(home, impostor), false)
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, 'lease-a-unguessable')
      assert.equal(clearMcpBridgeQuiesceLease(home, lease), true)
      assert.equal(readMcpBridgeQuiesceLease(home), null)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('binds mutations to the caller original object and exact raw generation', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const originalRaw = fs.readFileSync(marker)
      const unboundClone = { ...lease }

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, unboundClone, { now: () => 100_010 }), null)
      assert.equal(
        transferMcpBridgeQuiesceLease(home, unboundClone, 555, {
          isPidAlive: pid => pid === 555,
          now: () => 100_010
        }),
        null
      )
      assert.equal(clearMcpBridgeQuiesceLease(home, unboundClone), false)
      assert.equal(fs.readFileSync(marker).equals(originalRaw), true)

      const equivalentForeign = Buffer.from(
        `${JSON.stringify({
          install_root: lease.installRoot,
          handoff_grace_until: lease.handoffGraceUntil,
          expires_at: lease.expiresAt,
          created_at: lease.createdAt,
          owner_pid: lease.ownerPid,
          lease_id: lease.leaseId,
          schema_version: lease.schemaVersion
        }, null, 2)}\n`,
        'utf8'
      )

      assert.equal(equivalentForeign.equals(originalRaw), false)
      fs.writeFileSync(marker, equivalentForeign)

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 }), null)
      assert.equal(
        transferMcpBridgeQuiesceLease(home, lease, 555, {
          isPidAlive: pid => pid === 555,
          now: () => 100_010
        }),
        null
      )
      assert.equal(clearMcpBridgeQuiesceLease(home, lease), false)
      assert.equal(fs.readFileSync(marker).equals(equivalentForeign), true)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('preserves and refuses to steal a live lease when PID liveness is inconclusive', () => {
    const { home, root } = sandbox()
    const originalKill = process.kill

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      process.kill = (() => {
        const error = new Error('transient Windows process query failure')

        ;(error as NodeJS.ErrnoException).code = 'EBUSY'
        throw error
      }) as typeof process.kill

      const competingLease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_010,
        ownerPid: 654,
        randomId: () => 'lease-b-unguessable'
      })

      assert.equal(competingLease, null)
      assert.equal(pruneInactiveMcpBridgeQuiesceLease(home, { installRoot: root, now: () => 100_020 }), 'active')
      assert.deepEqual(readMcpBridgeQuiesceLease(home), lease)
    } finally {
      process.kill = originalKill
      cleanupSandbox(home)
    }
  })

  it('preserves an existing lease when process creation identity is unknown', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      const competingLease = acquireMcpBridgeQuiesceLease(home, root, {
        getProcessCreatedAt: () => null,
        isPidAlive: () => true,
        now: () => 100_010,
        ownerPid: 654,
        randomId: () => 'lease-b-unguessable'
      })

      assert.equal(competingLease, null)
      assert.deepEqual(readMcpBridgeQuiesceLease(home), lease)
      assert.equal(
        transferMcpBridgeQuiesceLease(home, lease, 555, {
          getProcessCreatedAt: () => null,
          isPidAlive: () => true,
          now: () => 100_010
        }),
        null
      )
    } finally {
      cleanupSandbox(home)
    }
  })

  it('treats a definitely newer process with a reused PID as stale', () => {
    const { home, root } = sandbox()

    try {
      const stale = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(stale)

      const replacement = acquireMcpBridgeQuiesceLease(home, root, {
        getProcessCreatedAt: pid => (pid === 321 ? 100_001 : 100_009),
        isPidAlive: () => true,
        now: () => 100_010,
        ownerPid: 654,
        randomId: () => 'lease-b-unguessable'
      })

      assert.ok(replacement)
      assert.equal(replacement.ownerPid, 654)
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, 'lease-b-unguessable')
    } finally {
      cleanupSandbox(home)
    }
  })

  it('transfers only a full expected lease to an exact live updater PID', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      const transferred = transferMcpBridgeQuiesceLease(home, lease, 555, {
        isPidAlive: pid => pid === 555,
        now: () => 100_010
      })

      assert.ok(transferred)
      assert.equal(transferred.ownerPid, 555)
      assert.equal(
        transferMcpBridgeQuiesceLease(home, lease, 556, {
          isPidAlive: () => true,
          now: () => 100_010
        }),
        null
      )
    } finally {
      cleanupSandbox(home)
    }
  })

  it('acknowledges only a live non-wrapper owner that also owns the shared update marker', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let nowMs = 0
      let updateOwnerClaim: { pid: number; startedAt: number } | null = updateOwner(444)

      const adopted = await waitForMcpBridgeQuiesceLeaseAdoption(home, lease, {
        excludedOwnerPids: [444],
        getProcessCreatedAt: pid => (pid === 555 ? 99_999 : null),
        isPidAlive: pid => pid === 555,
        now: () => 100_000,
        nowMs: () => nowMs,
        pollMs: 10,
        timeoutMs: 100,
        readUpdateOwner: () => updateOwnerClaim,
        wait: async delay => {
          nowMs += delay

          if (nowMs === 20) {
            const record = JSON.parse(fs.readFileSync(mcpBridgeQuiesceMarkerPath(home), 'utf8'))
            record.owner_pid = 555
            fs.writeFileSync(mcpBridgeQuiesceMarkerPath(home), `${JSON.stringify(record)}\n`, 'utf8')
            updateOwnerClaim = updateOwner(555)
          }
        }
      })

      assert.ok(adopted)
      assert.equal(adopted.ownerPid, 555)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('refuses lease adoption when updater creation identity is unknown or stale for the marker', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      writeLeaseRecord(home, { ...lease, ownerPid: 555 })

      const cases = [
        { markerStartedAt: 99_999, probe: () => null },
        {
          markerStartedAt: 99_999,
          probe: () => {
            throw new Error('process creation query failed')
          }
        },
        { markerStartedAt: 99_998, probe: () => 99_999 }
      ]

      for (const testCase of cases) {
        let nowMs = 0

        const adopted = await waitForMcpBridgeQuiesceLeaseAdoption(home, lease, {
          getProcessCreatedAt: testCase.probe,
          isPidAlive: pid => pid === 555,
          now: () => 100_000,
          nowMs: () => nowMs,
          pollMs: 10,
          readUpdateOwner: () => updateOwner(555, testCase.markerStartedAt),
          requiredOwnerPid: 555,
          timeoutMs: 0,
          wait: async delay => {
            nowMs += delay
          }
        })

        assert.equal(adopted, null)
      }
    } finally {
      cleanupSandbox(home)
    }
  })

  it('times out when only the transient wrapper owns the shared marker', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let nowMs = 0

      const adopted = await waitForMcpBridgeQuiesceLeaseAdoption(home, lease, {
        excludedOwnerPids: [444],
        isPidAlive: () => true,
        now: () => 100_000,
        nowMs: () => nowMs,
        pollMs: 10,
        timeoutMs: 30,
        readUpdateOwner: () => updateOwner(444),
        wait: async delay => {
          nowMs += delay
        }
      })

      assert.equal(adopted, null)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('revokes the same capability after a late updater transfer wins the timeout edge', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-late-adoption-12345'
      })

      assert.ok(lease)
      const handoff = markMcpBridgeQuiesceLeaseForHandoff(home, lease, {
        isPidAlive: pid => pid === 321,
        now: () => 100_010
      })

      assert.ok(handoff)
      const adopted = transferMcpBridgeQuiesceLease(home, handoff, 555, {
        isPidAlive: pid => pid === 555,
        now: () => 100_020
      })

      assert.ok(adopted)
      assert.equal(adopted.ownerPid, 555)

      assert.equal(
        revokeMcpBridgeQuiesceLease(home, handoff, { now: () => 100_030, ownerPid: 321 }),
        'revoked'
      )
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, handoff.leaseId)
      assert.equal(
        transferMcpBridgeQuiesceLease(home, adopted, 556, {
          isPidAlive: pid => pid === 556,
          now: () => 100_021
        }),
        null
      )
      const revocationArtifacts = fs
        .readdirSync(home)
        .filter(name => name.startsWith('.hermes-venv-quiesce.cas-'))

      assert.equal(revocationArtifacts.length, 1)
      assert.match(revocationArtifacts[0], /^\.hermes-venv-quiesce\.cas-release-/)
      assert.equal(
        pruneInactiveMcpBridgeQuiesceLease(home, {
          installRoot: root,
          isPidAlive: () => true,
          now: () => 100_120
        }),
        'active'
      )
      assert.equal(
        pruneInactiveMcpBridgeQuiesceLease(home, {
          installRoot: root,
          isPidAlive: () => true,
          now: () => 100_121
        }),
        'absent'
      )
    } finally {
      cleanupSandbox(home)
    }
  })

  it('requires the exact staged updater PID when one is known', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let nowMs = 0
      let updateOwnerClaim = updateOwner(444)

      const adopted = await waitForMcpBridgeQuiesceLeaseAdoption(home, lease, {
        getProcessCreatedAt: pid => (pid === 555 ? 99_999 : null),
        isPidAlive: pid => pid === 444 || pid === 555,
        now: () => 100_000,
        nowMs: () => nowMs,
        pollMs: 10,
        requiredOwnerPid: 555,
        timeoutMs: 100,
        readUpdateOwner: () => updateOwnerClaim,
        wait: async delay => {
          nowMs += delay
          const record = JSON.parse(fs.readFileSync(mcpBridgeQuiesceMarkerPath(home), 'utf8'))

          if (nowMs === 10) {
            record.owner_pid = 444
          } else if (nowMs === 20) {
            record.owner_pid = 555
            updateOwnerClaim = updateOwner(555)
          }

          fs.writeFileSync(mcpBridgeQuiesceMarkerPath(home), `${JSON.stringify(record)}\n`, 'utf8')
        }
      })

      assert.ok(adopted)
      assert.equal(adopted.ownerPid, 555)
      assert.equal(nowMs, 20)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('brackets the legacy PID transfer with exact staged process-generation probes', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let nowMs = 0
      let updateOwnerClaim: { pid: number; startedAt: number } | null = null
      let processStartQueries = 0
      let generationChecks = 0

      const handoff = await handOffMcpBridgeLeaseToStagedUpdater(home, lease, 555, {
        getProcessCreatedAt: () => {
          processStartQueries += 1

          return 99_999
        },
        handoffTimeoutMs: 100,
        isPidAlive: pid => pid === 555,
        now: () => 100_000,
        nowMs: () => nowMs,
        pollMs: 10,
        readUpdateOwner: () => updateOwnerClaim,
        requiredOwnerStartedAt: 99_999,
        verifyRequiredOwnerGeneration: () => {
          generationChecks += 1

          return true
        },
        wait: async delay => {
          nowMs += delay

          if (nowMs === 20) {
            updateOwnerClaim = updateOwner(555)
          }
        }
      })

      assert.equal(handoff.kind, 'legacy-transfer')

      if (handoff.kind === 'legacy-transfer') {
        assert.equal(handoff.lease.ownerPid, 555)
      }

      assert.equal(nowMs, 20)
      assert.equal(processStartQueries, 2)
      assert.equal(generationChecks, 2)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('rolls back a legacy transfer when the staged PID generation changes during the CAS', async () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)
    const originalRename = fs.renameSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let generationChanged = false
      let processStartQueries = 0
      let generationChecks = 0

      fs.renameSync = ((source, destination) => {
        if (
          !generationChanged &&
          source === marker &&
          String(destination).includes('.cas-previous-')
        ) {
          generationChanged = true
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      const handoff = await handOffMcpBridgeLeaseToStagedUpdater(home, lease, 555, {
        getProcessCreatedAt: () => {
          processStartQueries += 1

          return generationChanged ? 100_001 : 99_999
        },
        isPidAlive: pid => pid === 555,
        now: () => 100_000,
        nowMs: () => 0,
        readUpdateOwner: () => updateOwner(555),
        requiredOwnerStartedAt: 99_999,
        verifyRequiredOwnerGeneration: () => {
          generationChecks += 1

          return !generationChanged
        },
        wait: async () => {}
      })

      assert.equal(handoff.kind, 'failed')
      assert.equal(processStartQueries, 2)
      assert.equal(generationChecks, 2)
      assert.equal(readMcpBridgeQuiesceLease(home)?.ownerPid, 321)
      assert.deepEqual(markerCasArtifacts(home), [])
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('accepts capability adoption only when lease and update marker name the exact staged PID', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const adopted = { ...lease, ownerPid: 555 }
      fs.writeFileSync(
        mcpBridgeQuiesceMarkerPath(home),
        `${JSON.stringify({
          schema_version: adopted.schemaVersion,
          lease_id: adopted.leaseId,
          owner_pid: adopted.ownerPid,
          created_at: adopted.createdAt,
          expires_at: adopted.expiresAt,
          handoff_grace_until: adopted.handoffGraceUntil,
          install_root: adopted.installRoot
        })}\n`,
        'utf8'
      )
      let processStartQueries = 0

      const handoff = await handOffMcpBridgeLeaseToStagedUpdater(home, lease, 555, {
        getProcessCreatedAt: () => {
          processStartQueries += 1

          return 99_999
        },
        isPidAlive: pid => pid === 555,
        now: () => 100_000,
        nowMs: () => 0,
        readUpdateOwner: () => updateOwner(555),
        requiredOwnerStartedAt: 99_999,
        verifyRequiredOwnerGeneration: () => true,
        wait: async () => {}
      })

      assert.equal(handoff.kind, 'adopted')
      assert.equal(processStartQueries, 1)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('rejects reused staged PIDs unless one direct start probe matches the capture, lease, and marker', async () => {
    const cases = [
      { mode: 'legacy', requiredOwnerStartedAt: 99_998, markerStartedAt: 99_999 },
      { mode: 'legacy', requiredOwnerStartedAt: 99_999, markerStartedAt: 99_998 },
      { mode: 'adopted', requiredOwnerStartedAt: 99_999, markerStartedAt: 99_998 }
    ] as const

    for (const testCase of cases) {
      const { home, root } = sandbox()

      try {
        const lease = acquireMcpBridgeQuiesceLease(home, root, {
          now: () => 100_000,
          ownerPid: 321,
          randomId: () => `lease-${testCase.mode}-reuse`
        })

        assert.ok(lease)

        if (testCase.mode === 'adopted') {
          writeLeaseRecord(home, { ...lease, ownerPid: 555 })
        }

        let nowMs = 0
        let processStartQueries = 0

        const handoff = await handOffMcpBridgeLeaseToStagedUpdater(home, lease, 555, {
          getProcessCreatedAt: () => {
            processStartQueries += 1

            return 99_999
          },
          handoffTimeoutMs: 1,
          isPidAlive: pid => pid === 555,
          now: () => 100_000,
          nowMs: () => nowMs,
          pollMs: 2,
          readUpdateOwner: () => updateOwner(555, testCase.markerStartedAt),
          requiredOwnerStartedAt: testCase.requiredOwnerStartedAt,
          verifyRequiredOwnerGeneration: () => true,
          wait: async delay => {
            nowMs += delay
          }
        })

        assert.equal(handoff.kind, 'failed')
        assert.equal(processStartQueries, 1)
      } finally {
        cleanupSandbox(home)
      }
    }
  })

  it('fails closed when the staged updater never claims the update marker', async () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let nowMs = 0

      const handoff = await handOffMcpBridgeLeaseToStagedUpdater(home, lease, 555, {
        handoffTimeoutMs: 20,
        isPidAlive: pid => pid === 555,
        now: () => 100_000,
        nowMs: () => nowMs,
        pollMs: 10,
        readUpdateOwner: () => null,
        requiredOwnerStartedAt: 99_999,
        verifyRequiredOwnerGeneration: () => true,
        wait: async delay => {
          nowMs += delay
        }
      })

      assert.equal(handoff.kind, 'failed')
      assert.equal(readMcpBridgeQuiesceLease(home)?.ownerPid, 321)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('prunes only an inactive exact lease on relaunch', () => {
    const { home, root } = sandbox()

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      assert.equal(pruneInactiveMcpBridgeQuiesceLease(home, { now: () => 100_010, isPidAlive: () => true }), 'active')
      assert.equal(pruneInactiveMcpBridgeQuiesceLease(home, { now: () => 100_100, isPidAlive: () => false }), 'removed')
      assert.equal(readMcpBridgeQuiesceLease(home), null)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('publishes a handoff renewal atomically without exposing truncated JSON', () => {
    const { home, root } = sandbox()
    const originalRename = fs.renameSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      let observed: unknown = null

      fs.renameSync = ((source, destination) => {
        if (source === mcpBridgeQuiesceMarkerPath(home) && String(destination).includes('.cas-previous-')) {
          const shadow = markerCasArtifacts(home).find(candidate => candidate.includes('.cas-shadow-'))
          assert.ok(shadow)
          observed = JSON.parse(fs.readFileSync(shadow, 'utf8'))
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      assert.ok(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 }))
      assert.ok(observed)
      assert.equal((observed as any).lease_id, lease.leaseId)
      assert.deepEqual(Object.keys(observed as object).sort(), [
        'created_at',
        'expires_at',
        'handoff_grace_until',
        'install_root',
        'lease_id',
        'owner_pid',
        'schema_version'
      ])
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('keeps renewal and clear visible through valid CAS recovery artifacts', () => {
    const { home, root } = sandbox()
    const originalRename = fs.renameSync
    const observations: Array<{ purpose: string; leaseId: string | undefined }> = []

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      fs.renameSync = ((source, destination) => {
        const result = originalRename(source, destination)

        if (source === mcpBridgeQuiesceMarkerPath(home)) {
          const purpose = String(destination).includes('.cas-previous-')
            ? 'previous'
            : String(destination).includes('.cas-release-')
              ? 'release'
              : ''

          if (purpose) {
            assert.equal(fs.existsSync(mcpBridgeQuiesceMarkerPath(home)), false)
            observations.push({ purpose, leaseId: readMcpBridgeQuiesceLease(home)?.leaseId })
          }
        }

        return result
      }) as typeof fs.renameSync

      const renewed = markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 })
      assert.ok(renewed)
      assert.equal(clearMcpBridgeQuiesceLease(home, renewed), true)
      assert.deepEqual(observations, [
        { purpose: 'previous', leaseId: lease.leaseId },
        { purpose: 'release', leaseId: lease.leaseId }
      ])
      assert.deepEqual(markerCasArtifacts(home), [])
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('blocks on a valid emergency artifact but never adopts it', async () => {
    const { home, root } = sandbox()
    const now = Math.floor(Date.now() / 1000)

    const emergency: McpBridgeQuiesceLease = {
      schemaVersion: 1,
      leaseId: 'emergency-lease-123456',
      ownerPid: 555,
      createdAt: now,
      expiresAt: now + 120,
      handoffGraceUntil: now + 90,
      installRoot: fs.realpathSync.native(root)
    }

    const marker = mcpBridgeQuiesceMarkerPath(home)
    const artifact = `${marker}.cas-emergency-555-testnonce`

    try {
      fs.writeFileSync(
        artifact,
        `${JSON.stringify({
          schema_version: emergency.schemaVersion,
          lease_id: emergency.leaseId,
          owner_pid: emergency.ownerPid,
          created_at: emergency.createdAt,
          expires_at: emergency.expiresAt,
          handoff_grace_until: emergency.handoffGraceUntil,
          install_root: emergency.installRoot
        })}\n`,
        'utf8'
      )

      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, emergency.leaseId)
      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => false,
          now: () => now + 1,
          ownerPid: 777,
          randomId: () => 'replacement-lease-12345'
        }),
        null
      )

      let nowMs = 0
      assert.equal(
        await waitForMcpBridgeQuiesceLeaseAdoption(home, emergency, {
          isPidAlive: () => true,
          now: () => now + 1,
          nowMs: () => nowMs,
          pollMs: 10,
          readUpdateOwner: () => updateOwner(555, now),
          timeoutMs: 10,
          wait: async delay => {
            nowMs += delay
          }
        }),
        null
      )
    } finally {
      cleanupSandbox(home)
    }
  })

  it('retires a dead-owner non-emergency artifact only after the 90-second grace', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)
    const artifact = `${marker}.cas-shadow-555-testnonce`
    const recovery: McpBridgeQuiesceLease = {
      schemaVersion: 1,
      leaseId: 'dead-owner-recovery-12345',
      ownerPid: 555,
      createdAt: 100_000,
      expiresAt: 101_200,
      handoffGraceUntil: 100_090,
      installRoot: fs.realpathSync.native(root)
    }

    try {
      fs.writeFileSync(artifact, leaseRecordBytes(recovery))

      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => false,
          now: () => 100_090,
          ownerPid: 777,
          randomId: () => 'replacement-lease-12345'
        }),
        null
      )
      assert.equal(fs.existsSync(artifact), true)

      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => true,
          now: () => 100_091,
          ownerPid: 777,
          randomId: () => 'replacement-lease-12345'
        }),
        null
      )
      assert.equal(fs.existsSync(artifact), true)

      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: () => false,
          now: () => 100_091,
          ownerPid: 777,
          randomId: () => 'replacement-lease-12345'
        })?.leaseId,
        'replacement-lease-12345'
      )
      assert.equal(fs.existsSync(artifact), false)
    } finally {
      cleanupSandbox(home)
    }
  })

  it('does not overwrite a foreign lease that races a handoff renewal', () => {
    const { home, root } = sandbox()
    const originalRename = fs.renameSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const foreign = { ...lease, leaseId: 'lease-b-unguessable', ownerPid: 654 }
      let injected = false

      fs.renameSync = ((source, destination) => {
        if (
          !injected &&
          source === mcpBridgeQuiesceMarkerPath(home) &&
          String(destination).includes('.cas-previous-')
        ) {
          injected = true
          writeLeaseRecord(home, foreign)
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 }), null)
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, foreign.leaseId)
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('retains the isolated generation when hard-link restoration fails', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)
    const originalCopy = fs.copyFileSync
    const originalLink = fs.linkSync
    const originalRead = fs.readFileSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const foreign = { ...lease, leaseId: 'lease-hardlink-foreign', ownerPid: 654 }
      const foreignRaw = leaseRecordBytes(foreign)
      let copied = 0
      let injected = false

      const read = originalRead as unknown as (
        file: fs.PathOrFileDescriptor,
        options?: unknown
      ) => Buffer | string

      fs.readFileSync = ((file: fs.PathOrFileDescriptor, options?: unknown) => {
        const result = read(file, options)

        if (!injected && String(file) === marker) {
          injected = true
          fs.writeFileSync(marker, foreignRaw)
        }

        return result
      }) as typeof fs.readFileSync
      fs.linkSync = ((existingPath, newPath) => {
        if (String(existingPath).includes('.cas-previous-') && newPath === marker) {
          const error = new Error('hard links unavailable') as NodeJS.ErrnoException
          error.code = 'EPERM'
          throw error
        }

        return originalLink(existingPath, newPath)
      }) as typeof fs.linkSync
      fs.copyFileSync = ((source, destination, mode) => {
        copied += 1

        return originalCopy(source, destination, mode)
      }) as typeof fs.copyFileSync

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 }), null)
      assert.equal(fs.existsSync(marker), false)
      const previous = markerCasArtifacts(home).find(candidate => candidate.includes('.cas-previous-'))
      assert.ok(previous)
      assert.equal(fs.readFileSync(previous).equals(foreignRaw), true)
      assert.equal(copied, 0)
    } finally {
      fs.copyFileSync = originalCopy
      fs.linkSync = originalLink
      fs.readFileSync = originalRead
      cleanupSandbox(home)
    }
  })

  it('preserves a post-open foreign winner and the displaced evidence during restore', () => {
    const { home, root } = sandbox()
    const marker = mcpBridgeQuiesceMarkerPath(home)
    const originalLink = fs.linkSync
    const originalRead = fs.readFileSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const displaced = { ...lease, leaseId: 'lease-displaced-foreign', ownerPid: 654 }
      const winner = { ...lease, leaseId: 'lease-winning-foreign', ownerPid: 777 }
      const displacedRaw = leaseRecordBytes(displaced)
      const winnerRaw = leaseRecordBytes(winner)
      let injectedAfterRead = false
      let injectedWinner = false

      const read = originalRead as unknown as (
        file: fs.PathOrFileDescriptor,
        options?: unknown
      ) => Buffer | string

      fs.readFileSync = ((file: fs.PathOrFileDescriptor, options?: unknown) => {
        const result = read(file, options)

        if (!injectedAfterRead && String(file) === marker) {
          injectedAfterRead = true
          fs.writeFileSync(marker, displacedRaw)
        }

        return result
      }) as typeof fs.readFileSync
      fs.linkSync = ((existingPath, newPath) => {
        if (!injectedWinner && String(existingPath).includes('.cas-previous-') && newPath === marker) {
          injectedWinner = true
          fs.writeFileSync(marker, winnerRaw)
        }

        return originalLink(existingPath, newPath)
      }) as typeof fs.linkSync

      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_010 }), null)
      assert.equal(fs.readFileSync(marker).equals(winnerRaw), true)
      const previous = markerCasArtifacts(home).find(candidate => candidate.includes('.cas-previous-'))
      assert.ok(previous)
      assert.equal(fs.readFileSync(previous).equals(displacedRaw), true)
    } finally {
      fs.linkSync = originalLink
      fs.readFileSync = originalRead
      cleanupSandbox(home)
    }
  })

  it('does not clear a foreign lease that replaces the bytes already checked', () => {
    const { home, root } = sandbox()
    const originalRename = fs.renameSync

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)
      const foreign = { ...lease, leaseId: 'lease-b-unguessable', ownerPid: 654 }
      let injected = false

      fs.renameSync = ((source, destination) => {
        if (!injected && source === mcpBridgeQuiesceMarkerPath(home) && String(destination).includes('.cas-release-')) {
          injected = true
          writeLeaseRecord(home, foreign)
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      assert.equal(clearMcpBridgeQuiesceLease(home, lease), false)
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, foreign.leaseId)
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('does not delete a racing active lease while pruning a stale acquisition marker', () => {
    const { home, root } = sandbox()
    const originalRename = fs.renameSync

    try {
      const stale = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(stale)

      const foreign = {
        ...stale,
        leaseId: 'lease-b-unguessable',
        ownerPid: 654,
        createdAt: 102_000,
        expiresAt: 103_200,
        handoffGraceUntil: 102_090
      }

      let injected = false

      fs.renameSync = ((source, destination) => {
        if (
          !injected &&
          source === mcpBridgeQuiesceMarkerPath(home) &&
          String(destination).includes('.cas-displaced-')
        ) {
          injected = true
          writeLeaseRecord(home, foreign)
        }

        return originalRename(source, destination)
      }) as typeof fs.renameSync

      assert.equal(
        acquireMcpBridgeQuiesceLease(home, root, {
          isPidAlive: pid => pid === 654,
          now: () => 102_000,
          ownerPid: 777,
          randomId: () => 'lease-c-unguessable'
        }),
        null
      )
      assert.equal(readMcpBridgeQuiesceLease(home)?.leaseId, foreign.leaseId)
    } finally {
      fs.renameSync = originalRename
      cleanupSandbox(home)
    }
  })

  it('rejects future and wrong-root leases during handoff', () => {
    const { home, root } = sandbox()
    const otherRoot = path.join(home, 'other-install')
    fs.mkdirSync(otherRoot)

    try {
      const lease = acquireMcpBridgeQuiesceLease(home, root, {
        now: () => 100_000,
        ownerPid: 321,
        randomId: () => 'lease-a-unguessable'
      })

      assert.ok(lease)

      const future = {
        ...lease,
        createdAt: 100_006,
        expiresAt: 101_206,
        handoffGraceUntil: 100_096
      }

      writeLeaseRecord(home, future)
      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, future, { now: () => 100_000 }), null)

      const wrongRoot = { ...lease, installRoot: fs.realpathSync.native(otherRoot) }
      writeLeaseRecord(home, wrongRoot)
      assert.equal(markMcpBridgeQuiesceLeaseForHandoff(home, lease, { now: () => 100_000 }), null)
    } finally {
      cleanupSandbox(home)
    }
  })
})
