import assert from 'node:assert/strict'

import { afterEach, test, vi } from 'vitest'

import { createHandoffRelaunchExitWatch } from './handoff-relaunch-exit-watch'

afterEach(() => {
  vi.useRealTimers()
})

test('idle relaunch watch caches process identity and does not repeatedly probe', async () => {
  vi.useFakeTimers()

  let inspectCalls = 0
  let processIdentityCalls = 0
  let rootCalls = 0
  let watchedChange: ((filename: string | Uint8Array | null) => void) | null = null
  let watchClosed = false

  const watch = createHandoffRelaunchExitWatch({
    activePollMs: 250,
    debounceMs: 50,
    hermesHome: 'C:\\Users\\hermes\\.hermes',
    idlePollMs: 30_000,
    inspect: async identity => {
      inspectCalls += 1
      assert.equal(identity.currentRoot, 'C:\\Users\\hermes\\hermes-agent')
      assert.equal(identity.currentProcessStartedAt, 1_725_000_000)

      return 'idle'
    },
    currentExecutable: 'C:\\Program Files\\Hermes\\Hermes.exe',
    currentPid: 4321,
    resolveCurrentProcessStartedAt: async pid => {
      processIdentityCalls += 1
      assert.equal(pid, 4321)

      return 1_725_000_000
    },
    resolveCurrentRoot: () => {
      rootCalls += 1

      return 'C:\\Users\\hermes\\hermes-agent'
    },
    watchDirectory: (_directory, onChange) => {
      watchedChange = onChange

      return {
        close: () => {
          watchClosed = true
        }
      }
    }
  })

  assert.equal(await watch.start(), true)
  assert.equal(inspectCalls, 1)
  assert.equal(rootCalls, 1)
  assert.equal(processIdentityCalls, 1)

  await vi.advanceTimersByTimeAsync(10_000)
  assert.equal(inspectCalls, 1, 'idle mode must not retain the 250ms poll')

  watchedChange?.('unrelated-session-state.json')
  await vi.advanceTimersByTimeAsync(100)
  assert.equal(inspectCalls, 1, 'unrelated home-directory churn must not trigger inspection')

  watchedChange?.('.hermes-update-relaunch-request-attempt-123456.json')
  await vi.advanceTimersByTimeAsync(50)
  assert.equal(inspectCalls, 2, 'a request publication must wake the idle watcher promptly')
  assert.equal(rootCalls, 1, 'the immutable update root is resolved only once')
  assert.equal(processIdentityCalls, 1, 'the current process identity is resolved only once')

  watch.stop()
  assert.equal(watchClosed, true)
  assert.equal(vi.getTimerCount(), 0)
})

test('active relaunch requests use the short poll only until inspection becomes idle', async () => {
  vi.useFakeTimers()

  const dispositions = ['active', 'active', 'idle'] as const
  let inspectCalls = 0

  const watch = createHandoffRelaunchExitWatch({
    activePollMs: 250,
    debounceMs: 50,
    hermesHome: 'C:\\Users\\hermes\\.hermes',
    idlePollMs: 30_000,
    inspect: async () => dispositions[Math.min(inspectCalls++, dispositions.length - 1)],
    currentExecutable: 'C:\\Program Files\\Hermes\\Hermes.exe',
    currentPid: 4321,
    resolveCurrentProcessStartedAt: async () => 1_725_000_000,
    resolveCurrentRoot: () => 'C:\\Users\\hermes\\hermes-agent',
    watchDirectory: () => ({ close: () => {} })
  })

  assert.equal(await watch.start(), true)
  assert.equal(inspectCalls, 1)
  await vi.advanceTimersByTimeAsync(250)
  assert.equal(inspectCalls, 2)
  await vi.advanceTimersByTimeAsync(250)
  assert.equal(inspectCalls, 3)
  await vi.advanceTimersByTimeAsync(5_000)
  assert.equal(inspectCalls, 3, 'idle transition must retire the short poll')

  watch.stop()
})
