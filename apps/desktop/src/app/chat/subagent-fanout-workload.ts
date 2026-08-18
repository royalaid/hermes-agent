import type { RpcEvent } from '@/types/hermes'

export interface SubagentFanoutOptions {
  holdTerminal: boolean
  intervalMs: number
  seed: number
  turns: number
  updates: number
  workers: number
}

export interface SubagentFanoutInput {
  holdTerminal?: boolean
  intervalMs?: number
  /** Internal harness handoff from ensureFanoutRuntime; not a workload axis. */
  runtimeId?: string
  seed?: number
  turns?: number
  updates?: number
  workers?: number
}

export type SubagentFanoutEvent = Omit<RpcEvent<Record<string, unknown>>, 'payload' | 'session_id' | 'type'> & {
  payload: Record<string, unknown>
  session_id: string
  type: 'subagent.complete' | 'subagent.progress' | 'subagent.start' | 'subagent.thinking' | 'subagent.tool'
}

const clampInteger = (value: unknown, fallback: number, min: number, max: number) => {
  const numeric = typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : fallback

  return Math.min(max, Math.max(min, numeric))
}

export function normalizeSubagentFanoutOptions(input: SubagentFanoutInput): SubagentFanoutOptions {
  return {
    holdTerminal: input.holdTerminal === true,
    intervalMs: clampInteger(input.intervalMs, 33, 1, 1000),
    seed: clampInteger(input.seed, 1, 0, 0x7fffffff),
    turns: clampInteger(input.turns, 20, 1, 400),
    updates: clampInteger(input.updates, 12, 1, 240),
    workers: clampInteger(input.workers, 1, 1, 8)
  }
}

const randomSource = (seed: number) => {
  let state = seed || 1

  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0

    return state / 0x100000000
  }
}

const updateType = (index: number): SubagentFanoutEvent['type'] =>
  (['subagent.progress', 'subagent.thinking', 'subagent.tool'] as const)[index % 3]

export function buildSubagentFanoutEvents(options: SubagentFanoutOptions): SubagentFanoutEvent[] {
  const random = randomSource(options.seed)
  const events: SubagentFanoutEvent[] = []
  const sessionId = 'perf-subagent-fanout'

  const basePayload = (worker: number) => ({
    goal: `Inspect deterministic fanout worker ${worker}`,
    parent_id: null,
    subagent_id: `perf-worker-${worker}`,
    task_count: options.workers,
    task_index: worker - 1
  })

  for (let worker = 1; worker <= options.workers; worker += 1) {
    events.push({
      type: 'subagent.start',
      session_id: sessionId,
      payload: { ...basePayload(worker), status: 'running', tool_name: 'delegate_task' }
    })
  }

  for (let update = 0; update < options.updates; update += 1) {
    const type = updateType(update)

    for (let worker = 1; worker <= options.workers; worker += 1) {
      const marker = Math.floor(random() * 1_000_000)

      const payload: Record<string, unknown> = {
        ...basePayload(worker),
        status: 'running',
        text: `worker ${worker} update ${update + 1} marker ${marker}`
      }

      if (type === 'subagent.tool') {
        payload.tool_name = update % 2 === 0 ? 'read_file' : 'terminal'
        payload.tool_preview = `fixture/perf-worker-${worker}-${marker}.ts`
      }

      events.push({ type, session_id: sessionId, payload })
    }
  }

  for (let worker = 1; worker <= options.workers; worker += 1) {
    events.push({
      type: 'subagent.complete',
      session_id: sessionId,
      payload: {
        ...basePayload(worker),
        duration_seconds: Math.round((options.updates * options.intervalMs) / 10) / 100,
        status: 'completed',
        summary: `Worker ${worker} completed deterministic fanout work.`
      }
    })
  }

  return events
}

/**
 * Group the deterministic lifecycle into shared cadence ticks. Each tick
 * carries one event per worker, so increasing workers increases aggregate
 * reducer pressure without multiplying scenario duration.
 */
export function buildSubagentFanoutBatches(options: SubagentFanoutOptions): SubagentFanoutEvent[][] {
  const events = buildSubagentFanoutEvents(options)
  const batches: SubagentFanoutEvent[][] = []

  for (let index = 0; index < events.length; index += options.workers) {
    batches.push(events.slice(index, index + options.workers))
  }

  return batches
}
