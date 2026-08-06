// Renderer end of the diagnostics arming contract.
//
// The capture controller lives in Electron main (it is the only process that
// can reach the renderer AND the gateway), so main owns the capture id and the
// wall-clock anchor and pushes them down:
//
//   main → renderer  'diagnostics:arm'     { captureId, wallClockAnchorMs }
//   main → renderer  'diagnostics:disarm'  (no payload)
//
// Both arrive through the usual preload subscription shape
// (`window.hermesDesktop.diagnostics.onArm/onDisarm`, each returning its own
// unsubscribe). The bridge is optional at runtime: a renderer running against a
// preload without the diagnostics surface (older shell, tests, the browser
// harness) simply never arms, which is the disarmed — zero cost — state.

import { armDiagnostics, disarmDiagnostics } from './capture'

export interface DiagnosticsArmPayload {
  captureId: string
  wallClockAnchorMs: number
}

let teardown: null | (() => void) = null

/** Subscribe to main's arm/disarm pushes. Idempotent; returns the unsubscribe. */
export function initDiagnosticsArming(): () => void {
  if (teardown) {
    return teardown
  }

  const bridge = typeof window === 'undefined' ? undefined : window.hermesDesktop?.diagnostics

  if (!bridge) {
    return () => undefined
  }

  const offArm = bridge.onArm(payload => {
    if (payload && typeof payload.captureId === 'string') {
      armDiagnostics(payload.captureId, Number(payload.wallClockAnchorMs) || Date.now())
    }
  })

  const offDisarm = bridge.onDisarm(() => disarmDiagnostics())

  teardown = () => {
    offArm()
    offDisarm()
    teardown = null
    disarmDiagnostics()
  }

  return teardown
}
