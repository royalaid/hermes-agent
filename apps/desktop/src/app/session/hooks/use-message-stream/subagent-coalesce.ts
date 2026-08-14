import { useCallback, useEffect, useRef } from 'react'

import { isDiagnosticsArmed, recordGatewayEventApplied } from '@/diagnostics'
import { $subagentsBySession, subagentIdOf, type SubagentPayload, type SubagentUpsert, upsertSubagents } from '@/store/subagents'

import { MAX_STREAM_FLUSH_GAP_MS, STREAM_DELTA_FLUSH_MS } from './utils'

interface SubagentCoalescerOptions {
  /** Diagnostics only: concurrent in-flight turns, for the capture row. */
  countBusySessions: () => number
  sessionInterrupted: (sessionId: string) => boolean
}

// Same reasoning as the gateway-dispatch floor in ./index.ts: a delegate burst
// produces dozens of sub-millisecond flushes, and only the ones that plausibly
// cost a frame are worth a ring slot.
const SUBAGENT_FLUSH_RECORD_MIN_MS = 4

/**
 * Coalesce non-terminal `subagent.*` publishes.
 *
 * A running delegate emits progress/thinking/tool frames continuously, and each
 * one used to clone the subagent map and notify every subscriber — one React
 * commit per event, per delegate, for rows whose visible content changes by a
 * line of text. This buffers them per session and republishes on the SAME
 * timing profile as the text-delta flusher (33ms floor, stretched to 3x the
 * last flush's measured cost, capped at 250ms).
 *
 * Two rules keep the batching invisible:
 * - The first event for a row that is not on screen yet publishes immediately,
 *   so a new delegate appears the moment it starts rather than a window later.
 * - Terminal frames never come through here; `subagent.complete` flushes the
 *   buffer and then applies eagerly (see the gateway dispatcher).
 *
 * Timer-scheduled, never rAF: Chromium parks animation frames for an occluded
 * or minimized renderer, which would strand a delegate's last progress frames
 * until the user happened to focus the window.
 */
export function useSubagentCoalescer({ countBusySessions, sessionInterrupted }: SubagentCoalescerOptions) {
  const buffersRef = useRef<Map<string, SubagentUpsert[]>>(new Map())
  const flushHandleRef = useRef<number | null>(null)
  const lastFlushAtRef = useRef<number>(0)
  // What the previous batch cost on the main thread, driving the adaptive floor
  // exactly like scheduleDeltaFlush's.
  const lastFlushCostRef = useRef<number>(0)
  const measureRafRef = useRef<number | null>(null)

  // Apply (or drop) the buffered payloads for the given sessions. A session
  // that was stopped or torn down mid-window discards its buffer instead of
  // publishing running state after the stop — the same re-check the delta
  // flusher does, moved to flush time because the stop can land after the
  // events were queued.
  const drainSessions = useCallback(
    (ids: string[]) => {
      let applied = 0

      for (const id of ids) {
        const buffered = buffersRef.current.get(id)
        buffersRef.current.delete(id)

        if (!buffered?.length || sessionInterrupted(id)) {
          continue
        }

        upsertSubagents(id, buffered)
        applied += buffered.length
      }

      return applied
    },
    [sessionInterrupted]
  )

  const flushSubagentEvents = useCallback(
    (sessionId?: string) => {
      const buffers = buffersRef.current

      if (!buffers.size) {
        return
      }

      const ids = sessionId ? (buffers.has(sessionId) ? [sessionId] : []) : [...buffers.keys()]

      if (!ids.length) {
        return
      }

      const startedAt = performance.now()
      lastFlushAtRef.current = startedAt
      const applied = drainSessions(ids)

      if (applied && isDiagnosticsArmed()) {
        const durationMs = performance.now() - startedAt

        if (durationMs >= SUBAGENT_FLUSH_RECORD_MIN_MS) {
          recordGatewayEventApplied({
            busySessions: countBusySessions(),
            durationMs,
            eventType: 'subagent.flush'
          })
        }
      }

      if (!buffers.size && flushHandleRef.current !== null && typeof window !== 'undefined') {
        window.clearTimeout(flushHandleRef.current)
        flushHandleRef.current = null
      }
    },
    [countBusySessions, drainSessions]
  )

  const scheduleFlush = useCallback(() => {
    if (flushHandleRef.current !== null) {
      return
    }

    if (typeof window === 'undefined') {
      flushSubagentEvents()

      return
    }

    const sinceLast = performance.now() - lastFlushAtRef.current

    const adaptiveFloor = Math.min(
      Math.max(STREAM_DELTA_FLUSH_MS, lastFlushCostRef.current * 3),
      MAX_STREAM_FLUSH_GAP_MS
    )

    const runFlush = () => {
      flushHandleRef.current = null
      const startedAt = performance.now()
      lastFlushAtRef.current = startedAt
      const applied = drainSessions([...buffersRef.current.keys()])
      const writeCost = performance.now() - startedAt
      lastFlushCostRef.current = writeCost

      if (applied && isDiagnosticsArmed() && writeCost >= SUBAGENT_FLUSH_RECORD_MIN_MS) {
        recordGatewayEventApplied({
          busySessions: countBusySessions(),
          durationMs: writeCost,
          eventType: 'subagent.flush'
        })
      }

      // The store write is only the cheap half: the subscriber commit runs in
      // the next frame. Measure through it so the floor adapts to what the
      // batch really costs — and cancel the previous probe, because a parked
      // renderer never fires these and they would otherwise pile up.
      if (measureRafRef.current !== null) {
        window.cancelAnimationFrame(measureRafRef.current)
      }

      measureRafRef.current = window.requestAnimationFrame(frameStart => {
        measureRafRef.current = null

        // A newer flush already started; its own measurement wins.
        if (lastFlushAtRef.current !== startedAt) {
          return
        }

        lastFlushCostRef.current = writeCost + Math.max(0, performance.now() - frameStart)
      })
    }

    flushHandleRef.current = window.setTimeout(runFlush, Math.max(0, adaptiveFloor - sinceLast))
  }, [countBusySessions, drainSessions, flushSubagentEvents])

  const queueSubagentEvent = useCallback(
    (sessionId: string, payload: SubagentPayload, createIfMissing: boolean, eventType: string) => {
      const buffered = buffersRef.current.get(sessionId)
      const id = subagentIdOf(payload)
      const onScreen = ($subagentsBySession.get()[sessionId] ?? []).some(item => item.id === id)

      // A row nobody has seen yet: publish now so the delegate shows up as it
      // starts. Everything after that is an update to a visible row and batches.
      if (!onScreen && !buffered?.some(update => subagentIdOf(update.payload) === id)) {
        upsertSubagents(sessionId, [{ createIfMissing, eventType, payload }])

        return
      }

      const next = buffered ?? []
      next.push({ createIfMissing, eventType, payload })
      buffersRef.current.set(sessionId, next)
      scheduleFlush()
    },
    [scheduleFlush]
  )

  /** Session teardown (reclaim/swap): its buffered progress must never apply. */
  const discardSubagentEvents = useCallback((sessionId: string) => {
    buffersRef.current.delete(sessionId)
  }, [])

  useEffect(
    () => () => {
      if (typeof window !== 'undefined') {
        if (flushHandleRef.current !== null) {
          window.clearTimeout(flushHandleRef.current)
        }

        if (measureRafRef.current !== null) {
          window.cancelAnimationFrame(measureRafRef.current)
        }
      }

      flushHandleRef.current = null
      measureRafRef.current = null
      // The surface is going away; buffered progress for a hook that no longer
      // renders anything is exactly the late publish R5 forbids.
      buffersRef.current.clear()
    },
    []
  )

  return { discardSubagentEvents, flushSubagentEvents, queueSubagentEvent }
}
