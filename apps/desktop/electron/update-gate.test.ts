/**
 * Tests for electron/update-gate.ts — the update mutual-exclusion gate that
 * parks local backend spawns while an in-app update is running.
 *
 * The regression this guards (#73822): applyUpdates kills its own backend
 * BEFORE the Windows venv-blocker scan, while the updater can claim its
 * on-disk marker only after handoff. A marker-only gate therefore let the
 * renderer's reconnect spawn a fresh backend inside the update's critical
 * section. The gate must consult the in-process updateInFlight flag as well.
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  updateGateReason,
  UpdateInFlightTransaction,
  waitForLocalBackendClearance,
  waitForUpdateClearance
} from './update-gate'

function deps(marker: boolean, inFlight: boolean) {
  return {
    hasLiveMarker: () => marker,
    isUpdateInFlight: () => inFlight
  }
}

// ---------------------------------------------------------------------------
// updateGateReason
// ---------------------------------------------------------------------------

test('gate open when neither marker nor flag is set', () => {
  assert.equal(updateGateReason(deps(false, false)), null)
})

test('marker alone closes the gate', () => {
  assert.equal(updateGateReason(deps(true, false)), 'marker')
})

test('updateInFlight alone closes the gate (#73822 — the pre-marker window)', () => {
  assert.equal(updateGateReason(deps(false, true)), 'update-in-flight')
})

test('marker wins as the reported reason when both are set', () => {
  assert.equal(updateGateReason(deps(true, true)), 'marker')
})

test('one transaction gate covers normal update and bootstrap recovery handoffs', async () => {
  const transaction = new UpdateInFlightTransaction()
  let releaseRecovery!: () => void

  const recoveryHeld = new Promise<void>(resolve => {
    releaseRecovery = resolve
  })

  let recoveryEntered = false

  const recovery = transaction.run(async () => {
    recoveryEntered = true
    await recoveryHeld

    return 'recovery-complete'
  })

  assert.equal(recoveryEntered, true, 'the gate must acquire synchronously before the first await')
  assert.equal(transaction.isActive(), true)
  assert.equal(
    updateGateReason({ hasLiveMarker: () => false, isUpdateInFlight: transaction.isActive }),
    'update-in-flight'
  )
  await assert.rejects(transaction.run(async () => 'normal-update'), /already in progress/)

  releaseRecovery()
  assert.equal(await recovery, 'recovery-complete')
  assert.equal(transaction.isActive(), false)

  await assert.rejects(
    transaction.run(async () => {
      throw new Error('preflight failed')
    }),
    /preflight failed/
  )
  assert.equal(transaction.isActive(), false, 'every abort path must release the in-process gate')
})

test('primary and pool starts remain parked across recovery preflight into marker adoption', async () => {
  const transaction = new UpdateInFlightTransaction()
  let marker = false
  let finishRecovery!: () => void

  const recoveryHeld = new Promise<void>(resolve => {
    finishRecovery = resolve
  })

  const reasons: string[] = []
  let primarySpawned = false
  let poolSpawned = false

  const recovery = transaction.run(async () => {
    await recoveryHeld
  })

  const waitForStart = async (kind: 'pool' | 'primary') => {
    await waitForLocalBackendClearance(
      { hasLiveMarker: () => marker, isUpdateInFlight: transaction.isActive },
      {
        onWaitTick: reason => {
          reasons.push(`${kind}:${reason}`)
        },
        pollMs: 1,
        sleep: async () => {
          if (reasons.length === 2) {
            marker = true
            finishRecovery()
          } else if (reasons.length >= 5) {
            marker = false
          }
        },
        timeoutMs: 10_000
      }
    )

    if (kind === 'primary') {primarySpawned = true}
    else {poolSpawned = true}
  }

  await Promise.all([waitForStart('primary'), waitForStart('pool'), recovery])

  assert.equal(primarySpawned, true)
  assert.equal(poolSpawned, true)
  assert.ok(reasons.some(reason => reason.endsWith('update-in-flight')))
  assert.ok(reasons.some(reason => reason.endsWith('marker')))
})

// ---------------------------------------------------------------------------
// waitForUpdateClearance
// ---------------------------------------------------------------------------

test('returns clear immediately without sleeping when the gate is open', async () => {
  let slept = 0

  const outcome = await waitForUpdateClearance(deps(false, false), {
    pollMs: 10,
    sleep: async () => {
      slept += 1
    },
    timeoutMs: 1000
  })

  assert.equal(outcome, 'clear')
  assert.equal(slept, 0)
})

test('parks on the in-flight flag and finishes when it clears', async () => {
  // Simulates the #73822 sequence: the reconnect arrives while updateInFlight
  // is true and no marker exists yet; the flag clears (abort path finally)
  // and the waiter proceeds.
  let inFlight = true
  let ticks = 0

  const outcome = await waitForUpdateClearance(
    { hasLiveMarker: () => false, isUpdateInFlight: () => inFlight },
    {
      onWaitTick: reason => {
        ticks += 1
        assert.equal(reason, 'update-in-flight')

        if (ticks >= 3) {
          inFlight = false
        }
      },
      pollMs: 1,
      sleep: async () => {},
      timeoutMs: 10_000
    }
  )

  assert.equal(outcome, 'finished')
  assert.equal(ticks, 3)
})

test('parks across the flag→marker handoff without a gap', async () => {
  // Success path: the updater claims its marker before applyUpdates' finally
  // clears the flag, so a waiter that arrived during the scan stays parked
  // through the transition instead of slipping through.
  let inFlight = true
  let marker = false
  let ticks = 0
  const reasons: string[] = []

  const outcome = await waitForUpdateClearance(
    { hasLiveMarker: () => marker, isUpdateInFlight: () => inFlight },
    {
      onWaitTick: reason => {
        ticks += 1
        reasons.push(reason)

        if (ticks === 2) {
          marker = true // updater hand-off: marker written first…
        }

        if (ticks === 3) {
          inFlight = false // …then the flag clears; marker still holds the gate
        }

        if (ticks === 5) {
          marker = false // updater finished
        }
      },
      pollMs: 1,
      sleep: async () => {},
      timeoutMs: 10_000
    }
  )

  assert.equal(outcome, 'finished')
  assert.deepEqual(reasons, ['update-in-flight', 'update-in-flight', 'marker', 'marker', 'marker'])
})

test('returns a typed still-blocked timeout with the unresolved reason', async () => {
  let clock = 0

  const outcome = await waitForUpdateClearance(deps(true, false), {
    now: () => clock,
    pollMs: 10,
    sleep: async ms => {
      clock += ms
    },
    timeoutMs: 50
  })

  assert.deepEqual(outcome, { kind: 'still-blocked-timeout', reason: 'marker' })
})

test('primary backend remains parked after the UI wait times out until the marker clears', async () => {
  let marker = true
  let clock = 0
  let backendStarted = false
  const blockedReasons: string[] = []

  const outcome = await waitForLocalBackendClearance(
    { hasLiveMarker: () => marker, isUpdateInFlight: () => false },
    {
      now: () => clock,
      onStillBlocked: reason => {
        assert.equal(backendStarted, false)
        blockedReasons.push(`primary:${reason}`)
        marker = false
      },
      pollMs: 10,
      sleep: async ms => {
        clock += ms
      },
      timeoutMs: 50
    }
  )

  backendStarted = true

  assert.equal(outcome, 'finished')
  assert.deepEqual(blockedReasons, ['primary:marker'])
})

test('pool backend remains parked after an unreadable-marker timeout until the marker clears', async () => {
  let unreadableMarker = true
  let clock = 0
  let backendStarted = false
  const blockedReasons: string[] = []

  const outcome = await waitForLocalBackendClearance(
    { hasLiveMarker: () => unreadableMarker, isUpdateInFlight: () => false },
    {
      now: () => clock,
      onStillBlocked: reason => {
        assert.equal(backendStarted, false)
        blockedReasons.push(`pool:${reason}`)
        unreadableMarker = false
      },
      pollMs: 10,
      sleep: async ms => {
        clock += ms
      },
      timeoutMs: 50
    }
  )

  backendStarted = true

  assert.equal(outcome, 'finished')
  assert.deepEqual(blockedReasons, ['pool:marker'])
})
