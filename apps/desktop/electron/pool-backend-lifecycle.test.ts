import assert from 'node:assert/strict'

import { afterEach, test, vi } from 'vitest'

import {
  cancelPoolBackendStart,
  deletePoolBackendEntryIfCurrent,
  waitForPoolBackendStartClearance
} from './pool-backend-lifecycle'

afterEach(() => {
  vi.useRealTimers()
})

test('stopping a parked pool start prevents a later spawn and preserves its replacement', async () => {
  vi.useFakeTimers()

  const profile = 'research'
  const oldEntry = { startAbortController: new AbortController() }
  const replacement = { startAbortController: new AbortController() }
  const pool = new Map<string, typeof oldEntry>()
  let marker = true
  let spawnCount = 0

  pool.set(profile, oldEntry)

  const parkedStart = waitForPoolBackendStartClearance(
    pool,
    profile,
    oldEntry,
    { hasLiveMarker: () => marker, isUpdateInFlight: () => false },
    { pollMs: 1_000, timeoutMs: 20 * 60 * 1_000 }
  )
    .then(() => {
      spawnCount += 1
    })
    .catch(error => {
      deletePoolBackendEntryIfCurrent(pool, profile, oldEntry)
      throw error
    })

  await vi.advanceTimersByTimeAsync(0)
  assert.equal(vi.getTimerCount(), 1, 'the parked gate owns one cancellable sleep')

  assert.equal(cancelPoolBackendStart(pool, profile, oldEntry), true)
  pool.set(profile, replacement)
  marker = false

  await assert.rejects(parkedStart, error => {
    assert.equal((error as Error).name, 'AbortError')

    return true
  })

  assert.equal(spawnCount, 0, 'a stopped waiter must not spawn when the update gate later clears')
  assert.equal(pool.get(profile), replacement, 'the old rejection cleanup must not delete a replacement entry')
  assert.equal(vi.getTimerCount(), 0, 'aborting the parked wait must clear its pending timer')
})
