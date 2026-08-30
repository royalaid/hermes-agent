import { atom } from 'nanostores'
import { beforeEach, describe, expect, it } from 'vitest'

import { backgroundResumeForSession } from './background-delegation'
import { $subagentsBySession, type SubagentProgress, type SubagentStreamEntry } from './subagents'

const sub = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  id: over.id ?? 'deleg:1',
  parentId: null,
  goal: 'do the thing',
  status: 'running',
  taskCount: 1,
  taskIndex: 0,
  startedAt: 0,
  updatedAt: 0,
  filesRead: [],
  filesWritten: [],
  stream: [],
  ...over
})

const stream = (text: string): SubagentStreamEntry => ({ at: 0, kind: 'progress', text })
const $runtimeId = atom<null | string>('s1')
const $surfaceBusy = atom(false)
const $backgroundResume = backgroundResumeForSession($runtimeId, $surfaceBusy)

describe('$backgroundResume', () => {
  beforeEach(() => {
    $surfaceBusy.set(false)
    $runtimeId.set('s1')
    $subagentsBySession.set({})
  })

  it('counts running/queued children for the active session while idle', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' }), sub({ id: 'b', status: 'queued' })] })
    expect($backgroundResume.get()?.count).toBe(2)
  })

  it('surfaces the primary child latest stream line as live activity', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a', stream: [stream('Searching the web…')] })] })
    expect($backgroundResume.get()?.activity).toBe('Searching the web…')
  })

  it('activity is null when no stream line has arrived (UI uses generic copy)', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' })] })
    expect($backgroundResume.get()?.activity).toBeNull()
  })

  it('is null while a turn is busy (the turn owns the main loader)', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' })] })
    $surfaceBusy.set(true)
    expect($backgroundResume.get()).toBeNull()
  })

  it('scopes simultaneous surfaces to their own runtime id and busy state', () => {
    const $owner = backgroundResumeForSession(atom<null | string>('s1'), atom(false))
    const $other = backgroundResumeForSession(atom<null | string>('s2'), atom(false))

    $subagentsBySession.set({ s1: [sub({ id: 'owner-child', stream: [stream('Owner activity')] })] })

    expect($owner.get()).toEqual({ activity: 'Owner activity', count: 1 })
    expect($other.get()).toBeNull()
  })

  it('is null when only terminal children or other sessions have work', () => {
    $subagentsBySession.set({
      s1: [sub({ id: 'a', status: 'completed' }), sub({ id: 'b', status: 'failed' })],
      s2: [sub({ id: 'c' })]
    })
    expect($backgroundResume.get()).toBeNull()
  })

  it('is null when there is no active session', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' })] })
    $runtimeId.set(null)
    expect($backgroundResume.get()).toBeNull()
  })
})
