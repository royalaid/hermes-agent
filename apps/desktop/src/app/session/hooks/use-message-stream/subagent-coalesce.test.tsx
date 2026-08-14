import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import type { ChatMessagePart } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearClarifyRequest } from '@/store/clarify'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'
import type { RpcEvent } from '@/types/hermes'

import { STREAM_DELTA_FLUSH_MS } from './utils'

import { useMessageStream } from './index'

// U3: subagent and non-terminal tool publishes coalesce onto the delta
// flusher's timing profile. What these tests pin is the CONTRACT, not the
// mechanism: how many publishes a burst costs, that the final state matches the
// last event, that terminal frames never wait, that text and tool rows keep
// arrival order, that a stop discards buffered running state, and that all of
// it still lands when the compositor has parked animation frames.

const SID = 'coalesce-session'

let handleEvent: ((event: RpcEvent) => void) | null = null
let states: Map<string, ClientSessionState>
let storeWrites: number

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      storeWrites += 1
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
    states = sessionStateByRuntimeIdRef.current
  }, [stream.handleGatewayEvent])

  return null
}

function mount() {
  render(<Harness />)
  expect(handleEvent).not.toBeNull()
}

const emit = (type: string, payload: Record<string, unknown>) =>
  act(() => handleEvent!({ payload, session_id: SID, type }))

const advance = async (ms = STREAM_DELTA_FLUSH_MS) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const subagent = (overrides: Record<string, unknown> = {}) => ({
  goal: 'inspect the failure',
  status: 'running',
  subagent_id: 'child-1',
  task_index: 0,
  ...overrides
})

const rows = (): SubagentProgress[] => $subagentsBySession.get()[SID] ?? []
const row = (id = 'child-1') => rows().find(item => item.id === id)
const parts = (): ChatMessagePart[] => (states.get(SID)?.messages ?? []).flatMap(message => message.parts)
const toolParts = () => parts().filter(part => part.type === 'tool-call')

const interrupt = () => {
  const current = states.get(SID) ?? createClientSessionState()
  states.set(SID, { ...current, interrupted: true })
}

describe('subagent and non-terminal tool publish coalescing', () => {
  let publishes: number
  let stopListening: (() => void) | null = null

  beforeEach(() => {
    vi.useFakeTimers()
    handleEvent = null
    states = new Map()
    storeWrites = 0
    publishes = 0
    $subagentsBySession.set({})
    clearClarifyRequest()
    stopListening = $subagentsBySession.listen(() => {
      publishes += 1
    })
  })

  afterEach(() => {
    stopListening?.()
    stopListening = null
    cleanup()
    $subagentsBySession.set({})
    clearClarifyRequest()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('publishes a rapid subagent.progress burst once, landing on the last event', async () => {
    mount()

    emit('subagent.start', subagent({ tool_name: 'read' }))
    const publishesAfterStart = publishes

    for (const step of ['grep', 'read', 'edit', 'terminal', 'write']) {
      emit('subagent.progress', subagent({ text: `running ${step}`, tool_name: step }))
    }

    // The whole burst is still buffered: no republish, and the row on screen
    // still shows what the start event said.
    expect(publishes).toBe(publishesAfterStart)
    expect(row()?.currentTool).toBe('read')

    await advance()

    expect(publishes).toBe(publishesAfterStart + 1)
    expect(row()?.currentTool).toBe('write')
    // Content parity: batching must not eat the progress lines the row shows.
    expect(row()?.stream.map(entry => entry.text)).toEqual(
      expect.arrayContaining(['running grep', 'running write'])
    )
  })

  it('renders a not-yet-seen subagent immediately and coalesces its later progress', async () => {
    mount()

    emit('subagent.start', subagent({ subagent_id: 'child-a' }))
    expect(row('child-a')?.status).toBe('running')

    // A sibling spawning mid-window is also new, so it appears at once.
    emit('subagent.progress', subagent({ subagent_id: 'child-a', text: 'first step' }))
    emit('subagent.start', subagent({ goal: 'second job', subagent_id: 'child-b' }))

    expect(row('child-b')?.goal).toBe('second job')
    expect(row('child-a')?.stream.some(entry => entry.text === 'first step')).toBe(false)

    await advance()

    expect(row('child-a')?.stream.some(entry => entry.text === 'first step')).toBe(true)
  })

  it('applies subagent.complete immediately mid-window, after the buffered progress', () => {
    mount()

    emit('subagent.start', subagent())
    emit('subagent.progress', subagent({ text: 'halfway there' }))
    expect(row()?.stream.some(entry => entry.text === 'halfway there')).toBe(false)

    // No timer advance: the terminal frame must not wait for the window, and
    // the buffered progress must land in front of it, not after.
    emit('subagent.complete', subagent({ status: 'completed', summary: 'done' }))

    expect(row()?.status).toBe('completed')
    const texts = row()?.stream.map(entry => entry.text) ?? []
    expect(texts).toContain('halfway there')
    expect(texts.indexOf('halfway there')).toBeLessThan(texts.indexOf('done'))
  })

  it('renders text, a tool row, and more text in arrival order from one window', async () => {
    mount()

    emit('message.start', {})
    emit('message.delta', { text: 'before the tool ' })
    emit('tool.start', { args: { command: 'ls' }, name: 'terminal', tool_id: 't1' })
    emit('message.delta', { text: 'after the tool' })

    await advance()

    expect(
      parts().map(part => (part.type === 'text' ? `text:${part.text}` : part.type === 'tool-call' ? 'tool' : part.type))
    ).toEqual(['text:before the tool ', 'tool', 'text:after the tool'])
  })

  it('coalesces a tool.progress burst and flushes it before an immediate tool.complete', async () => {
    mount()

    emit('message.start', {})
    emit('tool.start', { args: { command: 'pytest' }, name: 'terminal', tool_id: 't1' })

    for (const line of ['1%', '25%', '50%', '75%']) {
      emit('tool.progress', { args: { command: 'pytest' }, name: 'terminal', preview: line, tool_id: 't1' })
    }

    // Not one commit per tick any more: the burst is queued.
    expect(toolParts()).toHaveLength(0)
    const writesDuringBurst = storeWrites

    await advance()

    expect(storeWrites).toBe(writesDuringBurst + 1)
    expect(toolParts()).toHaveLength(1)

    // A second burst, then the terminal frame with no timer advance at all.
    emit('tool.progress', { args: { command: 'pytest' }, name: 'terminal', preview: '99%', tool_id: 't1' })
    emit('message.delta', { text: 'wrapping up' })
    emit('tool.complete', { name: 'terminal', summary: 'suite passed', tool_id: 't1' })

    const rendered = toolParts()
    expect(rendered).toHaveLength(1)
    // The completed row carries its result: the terminal frame applied on the
    // dispatch, not a window later.
    expect(rendered[0].type === 'tool-call' && rendered[0].result).toMatchObject({ summary: 'suite passed' })
    // The text queued behind the row (and still unflushed when the terminal
    // frame arrived) was drained by it, and kept its place after the row.
    const order = parts().map(part => part.type)
    expect(order).toEqual(['tool-call', 'text'])
    expect(parts().some(part => part.type === 'text' && part.text === 'wrapping up')).toBe(true)
  })

  it('drops buffered running state when a stop lands mid-window', async () => {
    mount()

    emit('message.start', {})
    emit('subagent.start', subagent())
    emit('subagent.progress', subagent({ text: 'still working', tool_name: 'terminal' }))
    emit('tool.start', { args: { command: 'sleep 30' }, name: 'terminal', tool_id: 't1' })

    const beforeStop = row()

    interrupt()
    await advance(STREAM_DELTA_FLUSH_MS * 4)

    // Nothing buffered before the stop may publish after it.
    expect(row()).toBe(beforeStop)
    expect(row()?.status).toBe('running')
    expect(toolParts()).toHaveLength(0)
  })

  it('keeps delivering batched publishes while animation frames are parked', async () => {
    // A renderer the compositor parked: rAF accepts callbacks and never runs
    // them. Anything gated on a frame would strand the batch here.
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1)

    mount()

    emit('subagent.start', subagent())
    emit('subagent.progress', subagent({ text: 'parked-frame progress', tool_name: 'grep' }))
    emit('tool.start', { args: { command: 'ls' }, name: 'terminal', tool_id: 't1' })

    await advance()

    expect(row()?.currentTool).toBe('grep')
    expect(row()?.stream.some(entry => entry.text === 'parked-frame progress')).toBe(true)
    expect(toolParts()).toHaveLength(1)
    // The cost probes are allowed to wait for a frame that never comes; the
    // delivery is not.
    expect(rafSpy).toHaveBeenCalled()
  })

  it('never delays a clarify, MCP-setup, or approval request', () => {
    mount()

    emit('message.start', {})
    emit('message.delta', { text: 'thinking out loud ' })
    emit('clarify.request', { choices: ['yes', 'no'], question: 'Ship it?', request_id: 'req-1' })

    // Applied on the dispatch, with the text that preceded it already flushed.
    const clarify = toolParts()
    expect(clarify).toHaveLength(1)
    expect(clarify[0].type === 'tool-call' && clarify[0].toolCallId).toBe('req-1')
    expect(parts().some(part => part.type === 'text' && part.text === 'thinking out loud ')).toBe(true)
    expect(states.get(SID)?.needsInput).toBe(true)

    emit('mcp.setup.request', { action: 'install', reason: 'needs the docs server', request_id: 'req-2', server: 'docs' })
    expect(toolParts().some(part => part.type === 'tool-call' && part.toolCallId === 'req-2')).toBe(true)

    states.set(SID, { ...states.get(SID)!, needsInput: false })
    emit('approval.request', { command: 'rm -rf /tmp/x', description: 'dangerous command' })
    expect(states.get(SID)?.needsInput).toBe(true)
  })

  it('applies the queued tool row before publishing an approval request', () => {
    // The inline approval strip binds positionally to the pending tool row. If
    // the row is still sitting in the queue when the request is published, the
    // floating fallback mounts instead and the strip jumps into place when the
    // timer flush lands a window later.
    mount()

    emit('message.start', {})
    emit('tool.start', { args: { command: 'rm -rf /tmp/x' }, name: 'terminal', tool_id: 't1' })
    expect(toolParts()).toHaveLength(0)

    emit('approval.request', { command: 'rm -rf /tmp/x', description: 'dangerous command' })

    // No timer advance: the row is on screen by the time the request is.
    const rendered = toolParts()
    expect(rendered).toHaveLength(1)
    expect(rendered[0].type === 'tool-call' && rendered[0].toolCallId).toBe('t1')
  })

  it('does not duplicate a queued tool row when clarify.request carries the same id', async () => {
    // The dedup that collapses a clarify tool.start and its clarify.request into
    // ONE card has to hold across the queue boundary too: the tool.start is
    // buffered when the request arrives, so the request's own upsert must land
    // behind the drained row, not beside it.
    mount()

    emit('message.start', {})
    emit('tool.start', { args: { choices: ['yes', 'no'], question: 'Ship it?' }, name: 'clarify', tool_id: 'req-1' })
    emit('clarify.request', { choices: ['yes', 'no'], question: 'Ship it?', request_id: 'req-1' })

    await advance(STREAM_DELTA_FLUSH_MS * 4)

    const rendered = toolParts()
    expect(rendered).toHaveLength(1)
    expect(rendered[0].type === 'tool-call' && rendered[0].toolCallId).toBe('req-1')
  })
})
