/**
 * Tests for electron/update-marker.ts — the in-app update mutual-exclusion
 * marker that prevents a desktop relaunched mid-update from spawning a backend
 * the updater then kills in a loop (#50238).
 *
 * Run with: bunx vitest run --project electron electron/update-marker.test.ts
 *
 * Why this matters: the gate must (a) preserve any live updater regardless of
 * elapsed time, (b) fail closed for malformed, unreadable, future-dated, or
 * identity-unknown claims, and (c) remove only claims proven dead or PID-reused
 * without hiding a concurrently published replacement.
 */

import fs from 'fs'
import assert from 'node:assert/strict'
import os from 'os'
import path from 'path'

import { test } from 'vitest'

import { normalizeHermesHomeRoot } from './backend-env'
import { handoffResultPath } from './handoff-result'
import { waitForUpdateClearance } from './update-gate'
import {
  isPidAlive,
  markerPath,
  pidMatchesUpdateOwner,
  probePidIdentity,
  readLiveUpdateMarker,
  UPDATE_MARKER_CLOCK_SKEW_MS,
  UPDATE_MARKER_MAX_AGE_MS,
  updateHandoffConflict
} from './update-marker'

function tmpHome(tag) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-marker-${tag}-`))

  return dir
}

function writeMarker(home, pid, startedAtSec) {
  fs.writeFileSync(markerPath(home), `${pid}\n${startedAtSec}`)
}

const ALIVE: typeof process.kill = () => true // injected kill that "succeeds" => pid alive

const DEAD: typeof process.kill = () => {
  const err = new Error('no such process')

  ;(err as any).code = 'ESRCH'
  throw err
}

test('profile homes resolve every install lifecycle marker under the global Hermes root', () => {
  const globalHome = tmpHome('profile-root')
  const profileHome = path.join(globalHome, 'profiles', 'coder')
  const canonicalHome = normalizeHermesHomeRoot(profileHome)

  assert.equal(canonicalHome, globalHome)
  assert.equal(markerPath(canonicalHome), markerPath(globalHome))
  assert.equal(handoffResultPath(canonicalHome), handoffResultPath(globalHome))
})

test('absent marker => no live update', () => {
  const home = tmpHome('absent')
  assert.equal(readLiveUpdateMarker(home, { kill: ALIVE }), null)
})

test('live marker returns its exact serialized started_at identity', () => {
  const home = tmpHome('live')
  const now = 1_000_000_000_000
  const startedAt = Math.floor(now / 1000) - 5
  writeMarker(home, 4242, startedAt) // 5s old
  const res = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })
  assert.ok(res && res.kind === 'live', 'a fresh, alive marker is a live update')
  assert.equal(res.pid, 4242)
  assert.equal(res.startedAt, startedAt)
  assert.ok(res.ageMs >= 0 && res.ageMs < 10_000)
  assert.ok(fs.existsSync(markerPath(home)), 'a live marker is NOT deleted')
})

test('dead pid => no live update and marker is pruned', () => {
  const home = tmpHome('dead')
  writeMarker(home, 999999, Math.floor(Date.now() / 1000))
  assert.equal(readLiveUpdateMarker(home, { kill: DEAD }), null)
  assert.ok(!fs.existsSync(markerPath(home)), 'a dead-pid marker self-heals (deleted)')
})

test('a live updater remains blocking past the expected age ceiling', () => {
  const home = tmpHome('long-running-live')
  const now = 1_000_000_000_000
  const startedAt = Math.floor((now - UPDATE_MARKER_MAX_AGE_MS - 60_000) / 1000)
  writeMarker(home, 4242, startedAt)

  const marker = readLiveUpdateMarker(home, {
    kill: ALIVE,
    now: () => now,
    getProcessCreatedAt: () => startedAt - 60
  })

  assert.ok(marker)
  assert.equal(marker.kind, 'live')
  assert.equal(marker.pid, 4242)
  assert.ok(marker.ageMs > UPDATE_MARKER_MAX_AGE_MS)
  assert.equal(marker.overdue, true)
  assert.ok(fs.existsSync(markerPath(home)), 'a live updater retains its exact marker bytes')
})

test('an old marker is pruned only after its owner is proven dead', () => {
  const home = tmpHome('old-dead')
  const now = 1_000_000_000_000
  writeMarker(home, 4242, Math.floor((now - UPDATE_MARKER_MAX_AGE_MS - 60_000) / 1000))

  assert.equal(readLiveUpdateMarker(home, { kill: DEAD, now: () => now }), null)
  assert.ok(!fs.existsSync(markerPath(home)))
})

test('an unreadable marker fails closed and is not pruned as if absent', () => {
  const home = tmpHome('unreadable')
  const file = markerPath(home)
  writeMarker(home, 4242, Math.floor(Date.now() / 1000))
  const originalRead = fs.readFileSync

  fs.readFileSync = ((candidate, ...args) => {
    if (path.resolve(String(candidate)) === path.resolve(file)) {
      const error = new Error('sharing violation')

      ;(error as NodeJS.ErrnoException).code = 'EACCES'
      throw error
    }

    return (originalRead as any)(candidate, ...args)
  }) as typeof fs.readFileSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: ALIVE })
    assert.ok(marker)
    assert.equal(marker.kind, 'unreadable')
    assert.equal(marker.pid, null)

    const conflict = updateHandoffConflict(home, { kill: ALIVE })
    assert.ok(conflict)
    assert.equal(conflict.pid, null)
    assert.match(conflict.message, /could not verify/i)
  } finally {
    fs.readFileSync = originalRead
  }

  assert.ok(fs.existsSync(file), 'an unreadable marker must never be deleted')
})

test('marker timestamp beyond bounded clock skew fails closed and is preserved', () => {
  const home = tmpHome('future')
  const now = 1_000_000_000_000
  writeMarker(home, 4242, Math.floor((now + UPDATE_MARKER_CLOCK_SKEW_MS + 1_000) / 1000))

  const marker = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })

  assert.ok(marker)
  assert.equal(marker.kind, 'unreadable')
  assert.equal(marker.reason, 'future')
  assert.ok(fs.existsSync(markerPath(home)), 'clock disagreement cannot authorize backend startup')
})

test('only an exact two-line positive-integer marker is accepted', () => {
  const now = 1_000_000_000_000
  const startedAt = Math.floor(now / 1000)

  const malformedBodies = [
    '',
    `4242`,
    `4242junk\n${startedAt}`,
    `4242\n${startedAt}junk`,
    `4242\n${startedAt}\nforeign-owner`,
    `4242\n${startedAt}\n\n`,
    `0\n${startedAt}`,
    `-1\n${startedAt}`,
    `4242\n0`,
    `4242\n-1`,
    `4242.5\n${startedAt}`,
    `4242\n${startedAt}.5`,
    `${Number.MAX_SAFE_INTEGER}0\n${startedAt}`
  ]

  for (const [index, body] of malformedBodies.entries()) {
    const home = tmpHome(`malformed-${index}`)
    const file = markerPath(home)
    fs.writeFileSync(file, body)

    const marker = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })

    assert.ok(marker, `malformed body ${JSON.stringify(body)} must fail closed`)
    assert.equal(marker.kind, 'unreadable')
    assert.equal(marker.reason, 'malformed')
    assert.equal(fs.readFileSync(file, 'utf8'), body, 'untrusted bytes remain for safe recovery')
  }

  for (const [index, body] of [`4242\n${startedAt}\n`, `4242\r\n${startedAt}\r\n`].entries()) {
    const home = tmpHome(`well-formed-${index}`)
    fs.writeFileSync(markerPath(home), body)

    const marker = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })

    assert.ok(marker)
    assert.equal(marker.kind, 'live')
    assert.equal(marker.pid, 4242)
  }
})

test('dead-owner cleanup returns a same-PID replacement from the same read call', () => {
  const home = tmpHome('cleanup-race')
  const file = markerPath(home)
  const now = 1_000_000_000_000
  writeMarker(home, 111, Math.floor(now / 1000) - 5)
  const originalRename = fs.renameSync
  let injected = false
  let isolatedPath = ''
  let probeCount = 0

  const firstDeadThenAlive: typeof process.kill = () => {
    probeCount += 1

    if (probeCount === 1) {
      return DEAD(111, 0)
    }

    return true
  }

  fs.renameSync = ((source, destination) => {
    if (!injected && path.resolve(String(source)) === path.resolve(file)) {
      injected = true
      isolatedPath = String(destination)
      writeMarker(home, 111, Math.floor(now / 1000))
    }

    return originalRename(source, destination)
  }) as typeof fs.renameSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: firstDeadThenAlive, now: () => now })

    assert.ok(marker, 'the authoritative replacement must be returned without a second poll')
    assert.equal(marker.kind, 'live')
    assert.equal(marker.pid, 111)
    assert.equal(marker.ageMs, 0)
  } finally {
    fs.renameSync = originalRename
  }

  assert.ok(fs.existsSync(file))
  assert.match(path.basename(isolatedPath), /^\.hermes-update-in-progress\.cas-release-/)
})

test('exact stale cleanup returns a replacement published immediately after unlink', () => {
  const home = tmpHome('post-unlink-replacement')
  const file = markerPath(home)
  const now = 1_000_000_000_000
  const replacement = `555\n${Math.floor(now / 1000)}`
  writeMarker(home, 111, Math.floor(now / 1000) - 5)

  const originalUnlink = fs.unlinkSync
  let probeCount = 0

  const firstDeadThenAlive: typeof process.kill = () => {
    probeCount += 1

    if (probeCount === 1) {
      return DEAD(111, 0)
    }

    return true
  }

  fs.unlinkSync = (candidate => {
    originalUnlink(candidate)

    if (String(candidate).includes('.cas-release-')) {
      fs.writeFileSync(file, replacement)
    }
  }) as typeof fs.unlinkSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: firstDeadThenAlive, now: () => now })

    assert.ok(marker)
    assert.equal(marker.kind, 'live')
    assert.equal(marker.pid, 555)
    assert.equal(fs.readFileSync(file, 'utf8'), replacement)
  } finally {
    fs.unlinkSync = originalUnlink
  }
})

test('repeated stale-cleanup failures exhaust into a blocking state', () => {
  const home = tmpHome('cleanup-exhausted')
  const file = markerPath(home)
  writeMarker(home, 111, Math.floor(Date.now() / 1000))
  const originalRename = fs.renameSync

  fs.renameSync = ((source, destination) => {
    if (path.resolve(String(source)) === path.resolve(file)) {
      const error = new Error('sharing violation')

      ;(error as NodeJS.ErrnoException).code = 'EACCES'
      throw error
    }

    return originalRename(source, destination)
  }) as typeof fs.renameSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: DEAD })

    assert.ok(marker)
    assert.equal(marker.kind, 'unreadable')
    assert.equal(marker.reason, 'cleanup-race')
    assert.ok(fs.existsSync(file))
  } finally {
    fs.renameSync = originalRename
  }
})

test('an interrupted CAS release is restored before the marker read returns', () => {
  const home = tmpHome('release-recovery')
  const file = markerPath(home)
  const now = 1_000_000_000_000
  const raw = `444\n${Math.floor(now / 1000)}`
  const release = `${file}.cas-release-999-test`
  fs.writeFileSync(release, raw)

  const marker = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })

  assert.ok(marker)
  assert.equal(marker.kind, 'live')
  assert.equal(marker.pid, 444)
  assert.equal(fs.readFileSync(file, 'utf8'), raw)
  assert.ok(!fs.existsSync(release))
})

test('an ambiguous CAS recovery artifact keeps the marker gate closed', () => {
  const home = tmpHome('ambiguous-recovery')
  const file = markerPath(home)
  const shadow = `${file}.cas-shadow-999-test`
  fs.writeFileSync(shadow, `444\n${Math.floor(Date.now() / 1000)}`)

  const marker = readLiveUpdateMarker(home, { kill: ALIVE })

  assert.ok(marker)
  assert.equal(marker.kind, 'unreadable')
  assert.equal(marker.reason, 'cleanup-race')
  assert.ok(fs.existsSync(shadow))
})

test('cleanup restore never overwrites a foreign replacement that wins the restore race', () => {
  const home = tmpHome('exclusive-restore')
  const file = markerPath(home)
  const now = 1_000_000_000_000
  const originalRaw = `111\n${Math.floor(now / 1000) - 5}`
  const movedReplacement = `222\n${Math.floor(now / 1000) - 1}`
  const winningReplacement = `333\n${Math.floor(now / 1000)}`
  fs.writeFileSync(file, originalRaw)

  const originalExists = fs.existsSync
  const originalLink = fs.linkSync
  const originalRename = fs.renameSync
  let foreignInjected = false
  let renameCount = 0
  let probeCount = 0

  const injectWinningReplacement = () => {
    if (!foreignInjected) {
      foreignInjected = true
      fs.writeFileSync(file, winningReplacement)
    }
  }

  const firstDeadThenAlive: typeof process.kill = () => {
    probeCount += 1

    if (probeCount === 1) {
      return DEAD(111, 0)
    }

    return true
  }

  fs.existsSync = (candidate => {
    if (path.resolve(String(candidate)) === path.resolve(file)) {
      injectWinningReplacement()

      return false
    }

    return originalExists(candidate)
  }) as typeof fs.existsSync
  fs.linkSync = ((existingPath, newPath) => {
    if (path.resolve(String(newPath)) === path.resolve(file)) {
      injectWinningReplacement()

      const error = new Error('foreign marker already won')

      ;(error as NodeJS.ErrnoException).code = 'EEXIST'
      throw error
    }

    return originalLink(existingPath, newPath)
  }) as typeof fs.linkSync
  fs.renameSync = ((source, destination) => {
    renameCount += 1

    if (renameCount === 1 && path.resolve(String(source)) === path.resolve(file)) {
      fs.writeFileSync(file, movedReplacement)

      return originalRename(source, destination)
    }

    if (path.resolve(String(destination)) === path.resolve(file)) {
      // Simulate rename(2)'s replacement semantics so this regression is
      // meaningful on Windows as well as POSIX.
      if (originalExists(file)) {
        fs.unlinkSync(file)
      }

      return originalRename(source, destination)
    }

    return originalRename(source, destination)
  }) as typeof fs.renameSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: firstDeadThenAlive, now: () => now })

    assert.ok(marker)
    assert.equal(marker.kind, 'live')
    assert.equal(marker.pid, 333)
    assert.equal(fs.readFileSync(file, 'utf8'), winningReplacement)
  } finally {
    fs.existsSync = originalExists
    fs.linkSync = originalLink
    fs.renameSync = originalRename
  }
})

test('O_EXCL restore fallback preserves a foreign winner when hard links are unavailable', () => {
  const home = tmpHome('exclusive-copy-restore')
  const file = markerPath(home)
  const now = 1_000_000_000_000
  const movedReplacement = `222\n${Math.floor(now / 1000) - 1}`
  const winningReplacement = `333\n${Math.floor(now / 1000)}`
  writeMarker(home, 111, Math.floor(now / 1000) - 5)

  const originalLink = fs.linkSync
  const originalOpen = fs.openSync
  const originalRename = fs.renameSync
  let renameCount = 0
  let probeCount = 0

  const firstDeadThenAlive: typeof process.kill = () => {
    probeCount += 1

    if (probeCount === 1) {
      return DEAD(111, 0)
    }

    return true
  }

  fs.renameSync = ((source, destination) => {
    renameCount += 1

    if (renameCount === 1 && path.resolve(String(source)) === path.resolve(file)) {
      fs.writeFileSync(file, movedReplacement)
    }

    return originalRename(source, destination)
  }) as typeof fs.renameSync
  fs.linkSync = ((existingPath, newPath) => {
    if (path.resolve(String(newPath)) === path.resolve(file)) {
      const error = new Error('hard links unsupported')

      ;(error as NodeJS.ErrnoException).code = 'EPERM'
      throw error
    }

    return originalLink(existingPath, newPath)
  }) as typeof fs.linkSync
  fs.openSync = ((candidate, flags, mode) => {
    if (path.resolve(String(candidate)) === path.resolve(file) && flags === 'wx') {
      fs.writeFileSync(file, winningReplacement)
    }

    return (originalOpen as any)(candidate, flags, mode)
  }) as typeof fs.openSync

  try {
    const marker = readLiveUpdateMarker(home, { kill: firstDeadThenAlive, now: () => now })

    assert.ok(marker)
    assert.equal(marker.kind, 'live')
    assert.equal(marker.pid, 333)
    assert.equal(fs.readFileSync(file, 'utf8'), winningReplacement)
  } finally {
    fs.linkSync = originalLink
    fs.openSync = originalOpen
    fs.renameSync = originalRename
  }
})

test('isPidAlive: own pid is alive, impossible pid is dead', () => {
  assert.equal(isPidAlive(process.pid), true)
  assert.equal(isPidAlive(-1), false)
  assert.equal(isPidAlive(0), false)
  assert.equal(isPidAlive(NaN), false)
  assert.equal(isPidAlive(Number.MAX_SAFE_INTEGER + 1), false)
})

test('isPidAlive: EPERM counts as alive (process owned by another user)', () => {
  const eperm = () => {
    const err = new Error('operation not permitted')

    ;(err as any).code = 'EPERM'
    throw err
  }

  assert.equal(isPidAlive(4242, eperm), true)
})

test('isPidAlive: only ESRCH proves death; access and unknown probe errors fail closed', () => {
  for (const code of ['EACCES', 'EBUSY', 'UNKNOWN_WINDOWS_ERROR']) {
    const inconclusiveProbe = () => {
      const error = new Error(`inconclusive process probe: ${code}`)

      ;(error as NodeJS.ErrnoException).code = code
      throw error
    }

    assert.equal(isPidAlive(4242, inconclusiveProbe), true, `${code} must not prove process death`)
  }

  assert.equal(isPidAlive(4242, DEAD), false)
})

test('PID identity requires the process to predate the marker second', () => {
  const startedAt = 1_000

  assert.equal(
    probePidIdentity(4242, startedAt, { kill: ALIVE, getProcessCreatedAt: () => startedAt + 0.999 }),
    'matching'
  )
  assert.equal(
    probePidIdentity(4242, startedAt, { kill: ALIVE, getProcessCreatedAt: () => startedAt + 1 }),
    'stale'
  )
  assert.equal(
    pidMatchesUpdateOwner(4242, startedAt, {
      kill: ALIVE,
      getProcessCreatedAt: () => startedAt + 1
    }),
    false
  )
})

test('PID identity query errors and unavailable creation times remain unknown', () => {
  const startedAt = 1_000

  const queryFailure = () => {
    throw new Error('process creation query unavailable')
  }

  assert.equal(probePidIdentity(4242, startedAt, { kill: ALIVE }), 'unknown')
  assert.equal(
    probePidIdentity(4242, startedAt, { kill: ALIVE, getProcessCreatedAt: queryFailure }),
    'unknown'
  )
  assert.equal(
    probePidIdentity(4242, startedAt, { kill: ALIVE, getProcessCreatedAt: () => Number.NaN }),
    'unknown'
  )
  assert.equal(
    pidMatchesUpdateOwner(4242, startedAt, { kill: ALIVE, getProcessCreatedAt: queryFailure }),
    true,
    'the reader must block when identity cannot be disproved'
  )
})

test('PID creation time can prove identity after an inconclusive liveness probe', () => {
  const startedAt = 1_000

  const inaccessible = (code: string) => () => {
    const error = new Error(`process probe failed: ${code}`)

    ;(error as NodeJS.ErrnoException).code = code
    throw error
  }

  assert.equal(
    probePidIdentity(4242, startedAt, {
      kill: inaccessible('EPERM'),
      getProcessCreatedAt: () => startedAt + 1
    }),
    'stale'
  )
  assert.equal(
    probePidIdentity(4242, startedAt, {
      kill: inaccessible('EACCES'),
      getProcessCreatedAt: () => startedAt - 1
    }),
    'matching'
  )
  assert.equal(
    probePidIdentity(4242, startedAt, {
      kill: inaccessible('EBUSY'),
      getProcessCreatedAt: () => {
        throw new Error('creation time unavailable')
      }
    }),
    'unknown'
  )
})

test('a definitely reused live PID is stale and its marker is pruned', () => {
  const home = tmpHome('reused-pid')
  const now = 1_000_000_000_000
  const startedAt = Math.floor(now / 1000) - 10
  writeMarker(home, 4242, startedAt)

  assert.equal(
    readLiveUpdateMarker(home, {
      kill: ALIVE,
      now: () => now,
      getProcessCreatedAt: () => startedAt + 1
    }),
    null
  )
  assert.ok(!fs.existsSync(markerPath(home)), 'a newer process cannot inherit an older PID claim')
})

test('an unknown PID-probe error preserves the live marker and keeps the backend parked', async () => {
  const home = tmpHome('unknown-probe')
  writeMarker(home, 4242, Math.floor(Date.now() / 1000))

  const unknownProbe = () => {
    const error = new Error('transient Windows process query failure')

    ;(error as NodeJS.ErrnoException).code = 'EBUSY'
    throw error
  }

  let clock = 0

  const outcome = await waitForUpdateClearance(
    {
      hasLiveMarker: () => Boolean(readLiveUpdateMarker(home, { kill: unknownProbe })),
      isUpdateInFlight: () => false
    },
    {
      now: () => clock,
      pollMs: 10,
      sleep: async delayMs => {
        clock += delayMs
      },
      timeoutMs: 30
    }
  )

  assert.deepEqual(outcome, { kind: 'still-blocked-timeout', reason: 'marker' })
  assert.ok(fs.existsSync(markerPath(home)), 'an inconclusive PID probe cannot release the update marker')
})

// ---------------------------------------------------------------------------
// updateHandoffConflict (#75778)
//
// A retried "Update" click must not spawn a second updater over a still-live
// one while it is still alive and mutating the checkout.
// ---------------------------------------------------------------------------

test('no marker => hand-off is not blocked', () => {
  const home = tmpHome('conflict-none')
  assert.equal(updateHandoffConflict(home, { kill: ALIVE }), null)
})

test('a different live updater already owns the marker => hand-off is blocked', () => {
  const home = tmpHome('conflict-live')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor(now / 1000) - 6) // 6s old
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })
  assert.ok(conflict, 'a live foreign updater must block a new hand-off')
  assert.equal(conflict.pid, 1010)
  assert.match(conflict.message, /already running/)
  assert.match(conflict.message, /PID 1010/)
  assert.match(conflict.message, /6s/)
})

test('a dead-pid marker does not block a hand-off (self-heals)', () => {
  const home = tmpHome('conflict-dead')
  writeMarker(home, 999999, Math.floor(Date.now() / 1000))
  assert.equal(updateHandoffConflict(home, { kill: DEAD }), null)
})

test('a long-running live updater continues to block a second hand-off', () => {
  const home = tmpHome('conflict-long-running')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor((now - UPDATE_MARKER_MAX_AGE_MS - 60_000) / 1000))
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })

  assert.ok(conflict)
  assert.equal(conflict.pid, 1010)
  assert.match(conflict.message, /already running/)
})

test('minutes-scale elapsed time is formatted as "Nm Ss"', () => {
  const home = tmpHome('conflict-minutes')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor(now / 1000) - 125) // 2m 5s old
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })
  assert.ok(conflict)
  assert.match(conflict.message, /2m 5s/)
})
