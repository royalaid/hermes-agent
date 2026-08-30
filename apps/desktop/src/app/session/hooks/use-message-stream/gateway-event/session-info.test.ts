import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import {
  $activeSessionId,
  $currentCwd,
  $selectedStoredSessionId,
  $sessions,
  $workspaceCwdOwner,
  releaseWorkspaceCwdOwner,
  setActiveSessionId,
  setCurrentCwd,
  setSessions
} from '@/store/session'
import {
  _resetSessionBindingsForTests,
  bindRuntimeToSession,
  claimSessionBinding,
  normalizeSessionBinding
} from '@/store/session-binding'

import { handleSessionInfoEvent } from './session-info'
import type { GatewayEventContext } from './types'

// `_session_info` stamps `stored_session_id: session_key or ""`, so every
// not-yet-persisted session on the gateway emits an UNNAMED session.info that
// still carries a real cwd.
function sessionInfoEvent({
  activeSessionId,
  connectionId,
  cwd,
  explicitSid = '',
  profile = 'default',
  storedSessionId = ''
}: {
  activeSessionId: null | string
  connectionId?: string
  cwd: string
  explicitSid?: string
  profile?: string
  storedSessionId?: string
}): GatewayEventContext {
  const sessionId = explicitSid || activeSessionId

  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: activeSessionId },
      hydrateFromStoredSession: vi.fn(),
      lastCwdInfoSessionRef: { current: null },
      queryClient: { invalidateQueries: vi.fn() },
      refreshHermesConfig: vi.fn(),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: () => false,
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(state => state),
      upsertToolCall: vi.fn()
    },
    event: { connectionId, profile, session_id: explicitSid, type: 'session.info' },
    explicitSid,
    fromActiveSource: () => true,
    isActiveEvent: !!sessionId && sessionId === activeSessionId,
    occurredAt: Date.now() / 1000,
    payload: { cwd, stored_session_id: storedSessionId },
    scheduleConfigRefresh: vi.fn(),
    sessionId
  } as unknown as GatewayEventContext
}

describe('handleSessionInfoEvent workspace ownership', () => {
  beforeEach(() => {
    _resetSessionBindingsForTests()
    setActiveSessionId(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    setCurrentCwd('')
    setSessions([])
  })

  afterEach(() => {
    _resetSessionBindingsForTests()
    setActiveSessionId(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    setCurrentCwd('')
    setSessions([])
  })

  // #55831 / the "workspace pane visible with no agent selected" report: with
  // nothing selected an unscoped event is exactly the one that applies, and
  // `broadcast_session_info` re-emits for EVERY live session at once. Adopting
  // those repointed the pane at a stranger's folder and claimed it for the null
  // selection, so the tree/coding rail painted it until the next release
  // un-painted it — a flicker per fan-out, with no agent selected at all.
  it('ignores an unnamed broadcast from a session the pane is not bound to', () => {
    releaseWorkspaceCwdOwner()
    const unowned = $workspaceCwdOwner.get()

    handleSessionInfoEvent(sessionInfoEvent({ activeSessionId: null, cwd: '/repo/someone-elses-worktree' }))

    expect($currentCwd.get()).toBe('')
    expect($workspaceCwdOwner.get()).toBe(unowned)
  })

  it('does not let a fan-out of unnamed broadcasts walk the workspace path', () => {
    const cwds = ['/repo/one', '/repo/two', '/repo/three']

    for (const cwd of cwds) {
      handleSessionInfoEvent(sessionInfoEvent({ activeSessionId: null, cwd }))
    }

    expect($currentCwd.get()).toBe('')
  })

  // The case the absent-id allowance exists for: a lazy session that has not
  // been persisted yet is still the runtime this pane is bound to, so its cwd
  // must be adopted and owned — otherwise the workspace reads as un-owned for
  // the rest of the conversation.
  it('adopts an unnamed session.info from the pane its own runtime', () => {
    $selectedStoredSessionId.set('selected-session')

    handleSessionInfoEvent(
      sessionInfoEvent({ activeSessionId: 'runtime-1', cwd: '/repo/mine', explicitSid: 'runtime-1' })
    )

    expect($currentCwd.get()).toBe('/repo/mine')
    expect($workspaceCwdOwner.get()).toBe('selected-session')
  })

  it('carries the gateway source into stored-runtime admission', () => {
    const ctx = sessionInfoEvent({
      activeSessionId: 'runtime-a',
      connectionId: 'source-a',
      cwd: '/repo/a',
      explicitSid: 'runtime-a',
      storedSessionId: 'shared-id'
    })

    handleSessionInfoEvent(ctx)

    expect(ctx.deps.updateSessionState).toHaveBeenCalledWith(
      'runtime-a',
      expect.any(Function),
      'shared-id',
      { connectionId: 'source-a', profile: 'default' }
    )
  })

  it('adopts an untagged rebuilt runtime from the active primary source', () => {
    const primary = normalizeSessionBinding({
      ownerRoute: { connectionId: 'primary-source', profile: 'default' },
      storedSessionId: 'shared-id'
    })!

    claimSessionBinding(primary)
    bindRuntimeToSession(primary, 'runtime-old')
    setActiveSessionId('runtime-old')
    $selectedStoredSessionId.set('shared-id')
    setCurrentCwd('/repo/old')

    const ctx = sessionInfoEvent({
      activeSessionId: 'runtime-old',
      cwd: '/repo/rebuilt',
      explicitSid: 'runtime-rebuilt',
      storedSessionId: 'shared-id'
    })

    ctx.deps.sessionStateByRuntimeIdRef.current.set('runtime-old', {
      ...createClientSessionState('shared-id'),
      awaitingResponse: false,
      busy: false,
      streamId: null
    })

    handleSessionInfoEvent(ctx)

    expect($activeSessionId.get()).toBe('runtime-rebuilt')
    expect(ctx.deps.activeSessionIdRef.current).toBe('runtime-rebuilt')
    expect($currentCwd.get()).toBe('/repo/rebuilt')
    expect($workspaceCwdOwner.get()).toBe('shared-id')
  })

  it('adopts a tagged rebuilt runtime from the exact backend target behind a Desktop alias', () => {
    const owner = normalizeSessionBinding({
      ownerRoute: { connectionId: 'source-a', profile: 'desktop-alias', targetProfile: 'backend-a' },
      storedSessionId: 'shared-id'
    })!

    claimSessionBinding(owner)
    bindRuntimeToSession(owner, 'runtime-old')
    setActiveSessionId('runtime-old')
    $selectedStoredSessionId.set('shared-id')
    setCurrentCwd('/repo/old')

    const ctx = sessionInfoEvent({
      activeSessionId: 'runtime-old',
      connectionId: 'source-a',
      cwd: '/repo/rebuilt',
      explicitSid: 'runtime-rebuilt',
      profile: 'backend-a',
      storedSessionId: 'shared-id'
    })

    ctx.deps.sessionStateByRuntimeIdRef.current.set('runtime-old', {
      ...createClientSessionState('shared-id'),
      awaitingResponse: false,
      busy: false,
      streamId: null
    })

    handleSessionInfoEvent(ctx)

    expect($activeSessionId.get()).toBe('runtime-rebuilt')
    expect(ctx.deps.activeSessionIdRef.current).toBe('runtime-rebuilt')
    expect($currentCwd.get()).toBe('/repo/rebuilt')
  })

  it("does not let owner A's stale rebuilt-runtime info capture owner B's main pane", () => {
    const ownerB = normalizeSessionBinding({
      ownerRoute: { connectionId: 'source-b', profile: 'default' },
      storedSessionId: 'shared-id'
    })!

    claimSessionBinding(ownerB)
    bindRuntimeToSession(ownerB, 'runtime-b')
    setActiveSessionId('runtime-b')
    $selectedStoredSessionId.set('shared-id')
    setCurrentCwd('/repo/b')

    const ctx = sessionInfoEvent({
      activeSessionId: 'runtime-b',
      connectionId: 'source-a',
      cwd: '/repo/a',
      explicitSid: 'runtime-a-rebuilt',
      storedSessionId: 'shared-id'
    })

    ctx.deps.sessionStateByRuntimeIdRef.current.set('runtime-b', {
      ...createClientSessionState('shared-id'),
      awaitingResponse: false,
      busy: false,
      streamId: null
    })

    handleSessionInfoEvent(ctx)

    expect($activeSessionId.get()).toBe('runtime-b')
    expect(ctx.deps.activeSessionIdRef.current).toBe('runtime-b')
    expect($currentCwd.get()).toBe('/repo/b')
  })

  it("updates only the exact owner's row for a tagged session title", () => {
    setSessions([
      { connection_id: 'source-a', id: 'shared-id', profile: 'profile-a', title: 'Owner A' },
      { connection_id: 'source-b', id: 'shared-id', profile: 'profile-b', title: 'Owner B' }
    ] as never)

    const ctx = sessionInfoEvent({
      activeSessionId: 'runtime-b',
      connectionId: 'source-a',
      cwd: '',
      explicitSid: 'runtime-a',
      storedSessionId: 'shared-id'
    })

    ctx.event = { connectionId: 'source-a', profile: 'profile-a', session_id: 'runtime-a', type: 'session.title' }
    ctx.payload = { session_id: 'shared-id', title: 'Updated A' }

    handleSessionInfoEvent(ctx)

    expect($sessions.get().map(session => [session.connection_id, session.title])).toEqual([
      ['source-a', 'Updated A'],
      ['source-b', 'Owner B']
    ])
  })
})
