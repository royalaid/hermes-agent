import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from 'react'

import { $terminalTakeover, setTerminalTakeover } from '@/app/right-sidebar/store'
import { writeAgentTerminalChunk } from '@/app/right-sidebar/terminal/agent-terminal-stream'
import {
  $activeTerminalId,
  $terminals,
  createTerminal,
  ensureAgentTerminal,
  selectTerminal,
  type TerminalEntry
} from '@/app/right-sidebar/terminal/terminals'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $repoStatusByCwd } from '@/store/coding-status'
import { $gateway } from '@/store/gateway'
import { $busy, $currentCwd, $messages, $sessions, setBusy, setCurrentCwdTransient, setMessages } from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import { $subagentsBySession } from '@/store/subagents'
import type { SessionInfo } from '@/types/hermes'

import { discardPerfProbeSession, dispatchPerfProbeGatewayEvent, seedPerfProbeSession } from './perf-probe-bridge'
import { buildSubagentFanoutBatches, normalizeSubagentFanoutOptions, type SubagentFanoutInput } from './subagent-fanout-workload'

type Sample = {
  id: string
  phase: string
  actualDuration: number
  baseDuration: number
  startTime: number
  commitTime: number
}

type SyntheticDriverHandle = { stop: () => void }

type DriverBaseline = {
  busy: boolean
  messages: ReturnType<typeof $messages.get>
  sessions: ReturnType<typeof $sessions.get>
  subagents: ReturnType<typeof $subagentsBySession.get>
}

type PerfDriveSnapshot = {
  activeRuntime: boolean
  busy: boolean
  fanoutActive: boolean
  gatewayDispatches: number
  gatewayDispatchFailures: number
  messageCount: number
  subagentRows: number
}

type PerfSessionState = ReturnType<typeof createClientSessionState>

type PerfSessionTiles = {
  close: (storedSessionId: string) => void
  drop: (runtimeId: string) => void
  open: (storedSessionId: string, dir?: 'bottom' | 'center' | 'left' | 'right' | 'top', anchor?: string) => void
  patch: (storedSessionId: string, patch: { runtimeId?: string }) => void
  publish: (runtimeId: string, state: PerfSessionState) => void
  seedSessions?: (rows: ReturnType<typeof $sessions.get>) => void
  update?: (runtimeId: string, updater: (state: PerfSessionState) => PerfSessionState) => PerfSessionState | undefined
}

type PerfLayoutTree = {
  reveal: (paneId: string) => void
}

const FANOUT_VISIBLE_STORED_ID = 'perf-fanout-visible'
const FANOUT_CONTROL_STORED_ID = 'perf-fanout-control'
const FANOUT_CONTROL_RUNTIME_ID = 'perf-fanout-control-runtime'

const perfSessionRow = (id: string, title: string): SessionInfo => ({
  ended_at: null,
  id,
  input_tokens: 0,
  is_active: true,
  last_active: 0,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: null,
  source: 'desktop',
  started_at: 0,
  title,
  tool_call_count: 0
})

declare global {
  interface Window {
    __HERMES_LAYOUT_TREE__?: PerfLayoutTree
    __HERMES_SESSION_TILES__?: PerfSessionTiles
    __PERF_PROBE__?: {
      samples: Sample[]
      enabled: boolean
      clear: () => void
      summary: () => Record<string, { count: number; total: number; max: number; p50: number; p95: number }>
    }
    __PERF_DRIVE__?: {
      /** Inject an assistant message and grow it by `chunk` every `intervalMs`. Returns a stop handle. */
      stream: (opts?: { chunk?: string; intervalMs?: number; totalTokens?: number }) => SyntheticDriverHandle
      /**
       * Replace the transcript with `turns` synthetic user/assistant pairs of
       * realistic mixed markdown, then resolve with the ms elapsed from the
       * `setMessages` commit to the second animation frame (a mount+paint
       * proxy). Used by the `transcript` perf scenario. `reset()` restores.
       */
      loadTranscript: (turns?: number) => Promise<number>
      /**
       * Whether the active gateway socket is open. The perf harness waits on
       * this before measuring so background reconnect churn (a booting/absent
       * backend) doesn't contaminate frame-pacing numbers.
       */
      connected: () => boolean
      /** Mount files + multiple xterms for the synthetic right-pane scenario. */
      rightPaneSetup: (opts: { cwd: string; terminals?: number }) => { procId: string; terminalIds: string[] }
      rightPaneGit: (path: string, kind?: 'added' | 'conflicted' | 'modified') => void
      rightPaneReset: () => void
      rightPaneSelect: (id: string) => void
      rightPaneWrite: (procId: string, chunk: string) => void
      /** Production socket callback, exposed only by this opt-in probe module. */
      dispatchEvent: (event: { type: string; session_id: string; payload?: Record<string, unknown> }) => boolean
      /** Mint a runtime only inside the already-isolated gateway when needed. */
      ensureFanoutRuntime: () => Promise<string>
      /** Drives the production gateway dispatcher; available only with the perf probe. */
      subagentFanout: (opts?: SubagentFanoutInput) => SyntheticDriverHandle
      /** Release a terminal batch intentionally held for active-phase input probes. */
      releaseFanoutTerminal: () => boolean
      reset: () => void
      snapshotMsgs: () => number
      stateSnapshot: () => PerfDriveSnapshot
    }
  }
}

if (typeof window !== 'undefined' && !window.__PERF_PROBE__) {
  const samples: Sample[] = []
  window.__PERF_PROBE__ = {
    samples,
    enabled: false,
    clear: () => {
      samples.length = 0
    },
    summary: () => {
      const byId = new Map<string, number[]>()

      for (const s of samples) {
        const k = `${s.id}:${s.phase}`
        const arr = byId.get(k) ?? []
        arr.push(s.actualDuration)
        byId.set(k, arr)
      }

      const out: Record<string, { count: number; total: number; max: number; p50: number; p95: number }> = {}

      for (const [k, arr] of byId) {
        arr.sort((a, b) => a - b)
        const total = arr.reduce((a, b) => a + b, 0)
        out[k] = {
          count: arr.length,
          total: Math.round(total * 100) / 100,
          max: Math.round(arr[arr.length - 1] * 100) / 100,
          p50: Math.round(arr[Math.floor(arr.length * 0.5)] * 100) / 100,
          p95: Math.round(arr[Math.floor(arr.length * 0.95)] * 100) / 100
        }
      }

      return out
    }
  }
}

const onRender: ProfilerOnRenderCallback = (id, phase, actualDuration, baseDuration, startTime, commitTime) => {
  const probe = typeof window !== 'undefined' ? window.__PERF_PROBE__ : undefined

  if (!probe || !probe.enabled) {
    return
  }

  probe.samples.push({ id, phase, actualDuration, baseDuration, startTime, commitTime })

  if (probe.samples.length > 5000) {
    probe.samples.splice(0, probe.samples.length - 5000)
  }
}

if (typeof window !== 'undefined' && !window.__PERF_DRIVE__) {
  // Synthetic stream driver — pushes tokens through the live $messages atom so the
  // assistant-ui runtime + react tree sees them exactly as a real LLM stream would.
  // Driven by the perf harness (scripts/perf/) when no live LLM credit is available.
  let baseline: DriverBaseline | null = null
  let activeHandle: SyntheticDriverHandle | null = null
  let fanoutRuntimeId: null | string = null
  let fanoutTilesActive = false
  let releaseHeldFanoutTerminal: null | (() => boolean) = null
  let gatewayDispatches = 0
  let gatewayDispatchFailures = 0

  let rightPaneBaseline: null | {
    activeTerminalId: null | string
    cwd: string
    repoStatusByCwd: ReturnType<typeof $repoStatusByCwd.get>
    takeover: boolean
    terminals: readonly TerminalEntry[]
  } = null

  const stop = () => {
    activeHandle = null
    setBusy(false)
  }

  const snapshotBaseline = () => {
    if (!baseline) {
      baseline = {
        busy: $busy.get(),
        messages: $messages.get(),
        sessions: $sessions.get(),
        subagents: $subagentsBySession.get()
      }
    }
  }

  const stateSnapshot = (): PerfDriveSnapshot => {
    const runtimeId = fanoutRuntimeId
    const state = runtimeId ? $sessionStates.get()[runtimeId] : undefined

    return {
      activeRuntime: Boolean(runtimeId),
      busy: Boolean(state?.busy),
      fanoutActive: activeHandle !== null,
      gatewayDispatches,
      gatewayDispatchFailures,
      messageCount: state?.messages.length ?? 0,
      subagentRows: runtimeId ? ($subagentsBySession.get()[runtimeId]?.length ?? 0) : 0
    }
  }

  const resetRightPane = () => {
    if (!rightPaneBaseline) {
      return
    }

    setTerminalTakeover(rightPaneBaseline.takeover)
    $terminals.set(rightPaneBaseline.terminals)
    $activeTerminalId.set(rightPaneBaseline.activeTerminalId)
    $repoStatusByCwd.set(rightPaneBaseline.repoStatusByCwd)
    setCurrentCwdTransient(rightPaneBaseline.cwd)
    rightPaneBaseline = null
  }

  const cleanupFanoutTiles = () => {
    if (!fanoutTilesActive) {
      return
    }

    const tiles = window.__HERMES_SESSION_TILES__
    const visibleStoredId = FANOUT_VISIBLE_STORED_ID
    const controlStoredId = FANOUT_CONTROL_STORED_ID
    const controlRuntimeId = FANOUT_CONTROL_RUNTIME_ID

    if (tiles) {
      tiles.close(visibleStoredId)
      tiles.close(controlStoredId)
      tiles.drop(controlRuntimeId)

      if (fanoutRuntimeId) {
        tiles.drop(fanoutRuntimeId)
      }
    }

    fanoutTilesActive = false
  }

  // One synthetic turn's worth of mixed markdown — prose, a list, a fenced
  // code block, inline code, a link, and a short table — so a loaded transcript
  // exercises the same render cost (Streamdown blocks, code cards) a real one
  // would. Kept deterministic (seeded by index) so runs are comparable.
  const syntheticTurn = (i: number): ReturnType<typeof $messages.get> => {
    const user = {
      id: `perf-u-${i}`,
      role: 'user' as const,
      parts: [
        { type: 'text' as const, text: `Question ${i}: how does the widget in module ${i} handle back-pressure?` }
      ],
      timestamp: Date.now()
    }

    const assistant = {
      id: `perf-a-${i}`,
      role: 'assistant' as const,
      parts: [
        {
          type: 'text' as const,
          text: [
            `## Answer ${i}`,
            '',
            `The widget buffers writes and applies a bounded queue. Key points for module \`${i}\`:`,
            '',
            '- It coalesces bursts into a single flush.',
            '- Back-pressure propagates via a `Promise` that resolves on drain.',
            '- See [the design note](https://example.com/design) for the state machine.',
            '',
            '```ts',
            `function flush${i}(items: number[]) {`,
            '  return items.reduce((a, b) => a + b, 0)',
            '}',
            '```',
            '',
            '| stage | cost |',
            '|---|---|',
            '| enqueue | O(1) |',
            '| flush | O(n) |',
            ''
          ].join('\n')
        }
      ],
      timestamp: Date.now(),
      pending: false
    }

    return [user, assistant]
  }

  window.__PERF_DRIVE__ = {
    // The bridge holds the exact callback from ContribWiring. It may load
    // before this module, but refuses dispatch until that callback has mounted.
    dispatchEvent: event => dispatchPerfProbeGatewayEvent(event),
    ensureFanoutRuntime: async () => {
      snapshotBaseline()
      const gateway = $gateway.get()

      if (!gateway || gateway.connectionState !== 'open') {
        throw new Error('subagent-fanout requires the isolated gateway to be connected')
      }

      const created = (await gateway.request('session.create', {
        profile: 'default',
        source: 'desktop'
      })) as { session_id?: string }
      const sessionId = created.session_id?.trim()

      if (!sessionId) {
        throw new Error('isolated gateway did not return a runtime session id')
      }

      // Keep the primary route completely out of the benchmark. The scenario
      // binds this real isolated runtime to a session tile through the existing
      // automation hooks; SessionTile then reads its own $sessionStates slice.
      fanoutRuntimeId = sessionId

      return sessionId
    },
    snapshotMsgs: () => $messages.get().length,
    stateSnapshot,
    connected: () => {
      try {
        return $gateway.get()?.connectionState === 'open'
      } catch {
        return false
      }
    },
    rightPaneGit: (path, kind = 'modified') => {
      const file = {
        conflicted: kind === 'conflicted',
        path,
        staged: false,
        unstaged: kind === 'modified',
        untracked: kind === 'added'
      }

      const cwd = $currentCwd.get().trim()
      $repoStatusByCwd.set({
        ...$repoStatusByCwd.get(),
        [cwd]: {
          added: 0,
          ahead: 0,
          behind: 0,
          branch: 'perf',
          changed: 1,
          conflicted: kind === 'conflicted' ? 1 : 0,
          defaultBranch: 'main',
          detached: false,
          files: [file],
          removed: 0,
          staged: 0,
          unstaged: kind === 'modified' ? 1 : 0,
          untracked: kind === 'added' ? 1 : 0
        }
      })
    },
    rightPaneReset: resetRightPane,
    rightPaneSelect: selectTerminal,
    rightPaneSetup: ({ cwd, terminals = 3 }) => {
      resetRightPane()
      rightPaneBaseline = {
        activeTerminalId: $activeTerminalId.get(),
        cwd: $currentCwd.get(),
        repoStatusByCwd: $repoStatusByCwd.get(),
        takeover: $terminalTakeover.get(),
        terminals: $terminals.get()
      }

      setCurrentCwdTransient(cwd)
      const terminalIds = [createTerminal(cwd)]
      let procId = ''

      for (let index = 1; index < Math.max(1, terminals); index += 1) {
        procId = `right-pane-perf-${Date.now()}-${index}`
        const id = ensureAgentTerminal(procId, `perf output ${index}`)

        if (id) {
          terminalIds.push(id)
        }
      }

      if (procId) {
        selectTerminal(terminalIds.at(-1) ?? terminalIds[0])
      }

      setTerminalTakeover(true)

      return { procId, terminalIds }
    },
    rightPaneWrite: (procId, chunk) => writeAgentTerminalChunk(procId, chunk),
    loadTranscript: (turns = 200) => {
      snapshotBaseline()

      const next: ReturnType<typeof $messages.get> = []

      for (let i = 0; i < turns; i += 1) {
        next.push(...syntheticTurn(i))
      }

      const t0 = performance.now()
      setMessages(next)

      return new Promise<number>(resolve => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            resolve(performance.now() - t0)
          })
        })
      })
    },
    releaseFanoutTerminal: () => releaseHeldFanoutTerminal?.() ?? false,
    reset: () => {
      activeHandle?.stop()
      releaseHeldFanoutTerminal = null
      resetRightPane()
      cleanupFanoutTiles()

      if (fanoutRuntimeId) {
        discardPerfProbeSession(fanoutRuntimeId)
      }

      if (baseline) {
        setMessages(baseline.messages)
        window.__HERMES_SESSION_TILES__?.seedSessions?.(baseline.sessions)
        $subagentsBySession.set(baseline.subagents)
        setBusy(baseline.busy)

      }

      baseline = null
      fanoutRuntimeId = null
    },
    subagentFanout: (input = {}) => {
      const options = normalizeSubagentFanoutOptions(input)
      snapshotBaseline()
      activeHandle?.stop()
      gatewayDispatches = 0
      gatewayDispatchFailures = 0

      // Use the isolated gateway's real runtime identity. A fabricated id would
      // bypass active-session routing and create noisy session-not-found calls.
      const sessionId = input.runtimeId?.trim() || fanoutRuntimeId

      if (!sessionId) {
        throw new Error('call ensureFanoutRuntime before starting subagent-fanout')
      }


      const transcript = Array.from({ length: options.turns }, (_, index) => syntheticTurn(index)).flat()

      const tasks = Array.from({ length: options.workers }, (_, index) => ({
        goal: `Inspect deterministic fanout worker ${index + 1}`
      }))

      transcript.push({
        id: 'perf-fanout-user',
        role: 'user' as const,
        timestamp: Date.now(),
        parts: [{ type: 'text' as const, text: `Run ${options.workers} isolated fanout workers.` }]
      })
      transcript.push({
        id: 'perf-delegate-card',
        role: 'assistant' as const,
        timestamp: Date.now(),
        pending: true,
        parts: [
          {
            type: 'tool-call' as const,
            toolCallId: 'perf-delegate-task',
            toolName: 'delegate_task',
            args: { tasks },
            argsText: JSON.stringify({ tasks })
          },
          {
            type: 'text' as const,
            text: `\`\`\`ts\nconst worker = await delegate_task()\nconst PERF_WIDE_CODE_MARKER = '${'x'.repeat(4096)}'\n\`\`\``
          }
        ]
      })
      if (!seedPerfProbeSession(sessionId, transcript)) {
        throw new Error('subagent-fanout session bridge is not mounted')
      }

      const tiles = window.__HERMES_SESSION_TILES__
      const layout = window.__HERMES_LAYOUT_TREE__

      if (!tiles || !layout) {
        throw new Error('subagent-fanout session tile hooks are not mounted')
      }

      const visibleStoredId = FANOUT_VISIBLE_STORED_ID
      const controlStoredId = FANOUT_CONTROL_STORED_ID
      const controlRuntimeId = FANOUT_CONTROL_RUNTIME_ID
      const startedAt = Date.now()
      const visibleState: PerfSessionState = {
        ...createClientSessionState(visibleStoredId, transcript),
        busy: true,
        sawAssistantPayload: true,
        turnLive: true,
        turnStartedAt: startedAt
      }
      const controlState = createClientSessionState(controlStoredId, syntheticTurn(-1))

      tiles.seedSessions?.([
        ...$sessions.get(),
        perfSessionRow(visibleStoredId, 'Perf fanout visible'),
        perfSessionRow(controlStoredId, 'Perf fanout control')
      ])

      tiles.open(visibleStoredId, 'center')
      tiles.open(controlStoredId, 'center', `session-tile:${visibleStoredId}`)
      tiles.patch(visibleStoredId, { runtimeId: sessionId })
      tiles.patch(controlStoredId, { runtimeId: controlRuntimeId })
      tiles.publish(sessionId, visibleState)
      tiles.publish(controlRuntimeId, controlState)
      layout.reveal(`session-tile:${visibleStoredId}`)
      fanoutTilesActive = true

      const batches = buildSubagentFanoutBatches(options).map(batch =>
        batch.map(event => ({ ...event, session_id: sessionId }))
      )
      let cursor = 0
      let pendingTerminalBatch: (typeof batches)[number] | null = null
      let terminalReleaseRequested = false
      let timer: ReturnType<typeof setTimeout> | null = null

      const handle: SyntheticDriverHandle = {
        stop: () => {
          if (timer) {
            clearTimeout(timer)
          }

          timer = null
          pendingTerminalBatch = null
          terminalReleaseRequested = false
          releaseHeldFanoutTerminal = null
          activeHandle = null
          // The scenario cleanup settles and drops the tile through the
          // existing session-tile hook. Never project this tile into primary.
        }
      }

      const dispatchBatch = (batch: (typeof batches)[number]) => {
        // This is deliberately the configured socket callback, which is the
        // same handleGatewayEventWithPlugins seam used by a real frame. Never
        // write $subagentsBySession directly from the perf driver. One cadence
        // tick publishes one event per worker so worker count scales pressure.
        for (const event of batch) {
          if (!window.__PERF_DRIVE__?.dispatchEvent(event)) {
            gatewayDispatchFailures += 1
            handle.stop()

            return false
          }

          gatewayDispatches += 1
        }

        return true
      }

      releaseHeldFanoutTerminal = () => {
        if (activeHandle !== handle) {
          return false
        }

        terminalReleaseRequested = true
        if (!pendingTerminalBatch) {
          return true
        }

        const terminalBatch = pendingTerminalBatch
        pendingTerminalBatch = null
        releaseHeldFanoutTerminal = null

        if (!dispatchBatch(terminalBatch)) {
          return false
        }

        timer = setTimeout(() => handle.stop(), 0)
        return true
      }
      activeHandle = handle

      const tick = () => {
        if (activeHandle !== handle) {
          return
        }

        const batch = batches[cursor++]

        if (!batch) {
          handle.stop()

          return
        }

        const terminalBatch = batch.every(event => event.type === 'subagent.complete')
        if (terminalBatch && options.holdTerminal && !terminalReleaseRequested) {
          pendingTerminalBatch = batch
          return
        }

        if (!dispatchBatch(batch)) {
          return
        }

        timer = setTimeout(tick, terminalBatch ? 0 : options.intervalMs)
      }

      // Let ContribWiring observe the newly selected isolated runtime before
      // the first event reaches its activeSessionIdRef.
      timer = setTimeout(tick, 0)

      return handle
    },
    stream: ({
      chunk = 'word ',
      intervalMs = 16,
      totalTokens = 400,
      // Mimic `use-message-stream.scheduleDeltaFlush` — batch token deltas
      // into at-most one $messages update every `flushMinMs` ms, exactly as
      // the real gateway path does. With this on, the synthetic harness's
      // numbers actually reflect what a real LLM stream of the same token
      // rate would feel like. Set to 0 to bypass and apply every token
      // immediately (worst-case).
      flushMinMs = 0
    }: { chunk?: string; intervalMs?: number; totalTokens?: number; flushMinMs?: number } = {}) => {
      snapshotBaseline()
      activeHandle?.stop()
      const current = $messages.get()

      const msgId = `synthetic-${Date.now()}`
      // Seed an empty assistant message — assistant-ui will see it grow.
      setMessages([
        ...current,
        {
          id: msgId,
          role: 'assistant',
          parts: [{ type: 'text', text: '' }],
          timestamp: Date.now(),
          pending: true
        }
      ])
      setBusy(true)

      let pushed = 0
      let pendingDelta = ''
      let lastFlushAt = 0
      let timer: ReturnType<typeof setTimeout> | null = null
      let flushHandle: number | null = null

      const applyDelta = (delta: string) => {
        if (!delta) {
          return
        }

        setMessages(prev =>
          prev.map(m => {
            if (m.id !== msgId) {
              return m
            }

            const head = m.parts.slice(0, -1)
            const last = m.parts.at(-1)
            const lastText = last && last.type === 'text' ? last.text : ''

            return {
              ...m,
              parts: [...head, { type: 'text', text: lastText + delta }]
            }
          })
        )
      }

      const flushNow = () => {
        flushHandle = null
        lastFlushAt = performance.now()
        const delta = pendingDelta
        pendingDelta = ''
        applyDelta(delta)
      }

      const scheduleFlush = () => {
        if (flushHandle !== null) {
          return
        }

        if (flushMinMs <= 0) {
          flushNow()

          return
        }

        const since = performance.now() - lastFlushAt
        const wait = Math.max(0, flushMinMs - since)
        flushHandle =
          wait <= 0 && typeof requestAnimationFrame === 'function'
            ? requestAnimationFrame(flushNow)
            : (setTimeout(flushNow, wait) as unknown as number)
      }

      const handle: SyntheticDriverHandle = {
        stop: () => {
          if (timer) {
            clearTimeout(timer)
          }

          timer = null

          if (flushHandle !== null) {
            clearTimeout(flushHandle)
            cancelAnimationFrame?.(flushHandle)
          }

          flushHandle = null

          if (pendingDelta) {
            applyDelta(pendingDelta)
            pendingDelta = ''
          }

          activeHandle = null
          // Mark message finalized.
          setMessages(prev => prev.map(m => (m.id === msgId ? { ...m, pending: false } : m)))
          setBusy(false)
        }
      }

      activeHandle = handle

      const tick = () => {
        if (activeHandle !== handle) {
          return
        }

        if (pushed >= totalTokens) {
          if (pendingDelta) {
            flushNow()
          }

          handle.stop()

          return
        }

        pushed += 1

        if (flushMinMs > 0) {
          pendingDelta += chunk
          scheduleFlush()
        } else {
          applyDelta(chunk)
        }

        timer = setTimeout(tick, intervalMs)
      }

      timer = setTimeout(tick, intervalMs)

      return handle
    }
  }

  // Suppress dead-import warning.
  void stop
}

export function PerfProbe({ id, children }: { id: string; children: ReactNode }) {
  return (
    <Profiler id={id} onRender={onRender}>
      {children}
    </Profiler>
  )
}
