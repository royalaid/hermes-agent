import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import type * as SessionStore from '@/store/session'
import {
  _resetSessionBindingsForTests,
  bindRuntimeToSession,
  claimSessionBinding,
  normalizeSessionBinding,
  runtimeForExactSessionBinding,
  setMainSessionBinding
} from '@/store/session-binding'
import type { SessionTile } from '@/store/session-states'
import type * as SessionStatesStore from '@/store/session-states'

const mocks = vi.hoisted(() => ({
  dropSessionState: vi.fn(),
  openSession: vi.fn(),
  patchSessionTile: vi.fn(),
  requestSessionResume: vi.fn(),
  setActiveSessionId: vi.fn(),
  setAwaitingResponse: vi.fn(),
  setBusy: vi.fn(),
  setMessages: vi.fn(),
  setSelectedStoredSessionId: vi.fn()
}))

vi.mock('../open-session', () => ({ openSession: (...args: unknown[]) => mocks.openSession(...args) }))
vi.mock('@/store/session', async importOriginal => ({
  ...(await importOriginal<typeof SessionStore>()),
  requestSessionResume: (...args: unknown[]) => mocks.requestSessionResume(...args),
  setActiveSessionId: (...args: unknown[]) => mocks.setActiveSessionId(...args),
  setAwaitingResponse: (...args: unknown[]) => mocks.setAwaitingResponse(...args),
  setBusy: (...args: unknown[]) => mocks.setBusy(...args),
  setMessages: (...args: unknown[]) => mocks.setMessages(...args),
  setSelectedStoredSessionId: (...args: unknown[]) => mocks.setSelectedStoredSessionId(...args)
}))
vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<typeof SessionStatesStore>()),
  dropSessionState: (...args: unknown[]) => mocks.dropSessionState(...args),
  patchSessionTile: (...args: unknown[]) => mocks.patchSessionTile(...args)
}))

import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { openExactSidebarSession } from './exact-sidebar-session'

const routeA = { connectionId: 'source-a', profile: 'profile-a', targetProfile: 'profile-a' }
const routeB = { connectionId: 'source-b', profile: 'profile-b', targetProfile: 'target-b' }
const navigate = vi.fn()

describe('openExactSidebarSession', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    _resetSessionBindingsForTests()
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessionTiles.set([])
  })

  it('focuses the exact same-owner tile without resume or duplicate', () => {
    const binding = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeA })!
    $sessionTiles.set([{ storedSessionId: 'shared-id', ownerRoute: routeA, runtimeId: 'runtime-a' }])
    bindRuntimeToSession(binding, 'runtime-a')

    expect(openExactSidebarSession({ binding, navigate, placement: 'tab' })).toBe('focused-warm')
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
    expect(mocks.openSession).toHaveBeenCalledOnce()
    expect(mocks.dropSessionState).not.toHaveBeenCalled()
  })

  it('keeps an exact same-owner main binding warm without another resume request', () => {
    const binding = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeA })!
    const generation = claimSessionBinding(binding)

    setMainSessionBinding(binding, $activeGatewayProfile.get())
    $selectedStoredSessionId.set('shared-id')
    $activeSessionId.set('runtime-a')
    bindRuntimeToSession(binding, 'runtime-a', generation)

    expect(openExactSidebarSession({ binding, navigate, placement: 'main' })).toBe('focused-warm')
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
    expect(mocks.setActiveSessionId).not.toHaveBeenCalled()
    expect(mocks.setMessages).not.toHaveBeenCalled()
  })

  it('fences a competing tile owner before committing and cold-opening the clicked owner', () => {
    const bindingA = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeA })!
    const bindingB = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeB })!
    $sessionTiles.set([{ storedSessionId: 'shared-id', ownerRoute: routeA, runtimeId: 'runtime-a' }])
    bindRuntimeToSession(bindingA, 'runtime-a')

    expect(openExactSidebarSession({ binding: bindingB, navigate, placement: 'tab' })).toBe('rebound-cold')
    expect(mocks.dropSessionState).toHaveBeenCalledWith('runtime-a')
    expect(mocks.patchSessionTile).toHaveBeenCalledWith('shared-id', {
      error: undefined,
      ownerRoute: bindingB.ownerRoute,
      runtimeId: undefined
    })
    expect(mocks.openSession).toHaveBeenCalledOnce()
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
  })

  it('releases a competing same-id main surface before cold-opening the clicked owner in a tab', () => {
    const bindingA = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeA })!
    const bindingB = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeB })!
    const generation = claimSessionBinding(bindingA)

    setMainSessionBinding(bindingA, $activeGatewayProfile.get())
    $selectedStoredSessionId.set('shared-id')
    $activeSessionId.set('runtime-a')
    bindRuntimeToSession(bindingA, 'runtime-a', generation)

    expect(openExactSidebarSession({ binding: bindingB, navigate, placement: 'tab' })).toBe('rebound-cold')
    expect(mocks.setSelectedStoredSessionId).toHaveBeenCalledWith(null)
    expect(mocks.openSession).toHaveBeenCalledWith('shared-id', navigate, 'tab', {
      ownerRoute: bindingB.ownerRoute,
      workspaceMode: 'sessions'
    })
  })

  it('fences a late A completion after the same-id surface is rebound to B', () => {
    const bindingA = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeA })!
    const bindingB = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeB })!
    const generationA = claimSessionBinding(bindingA)

    $sessionTiles.set([{ storedSessionId: 'shared-id', ownerRoute: routeA, runtimeId: 'runtime-a' }])
    bindRuntimeToSession(bindingA, 'runtime-a', generationA)

    openExactSidebarSession({ binding: bindingB, navigate, placement: 'tab' })

    expect(bindRuntimeToSession(bindingA, 'runtime-a-late', generationA)).toBe(false)
    expect(bindRuntimeToSession(bindingB, 'runtime-b')).toBe(true)
    expect(runtimeForExactSessionBinding(bindingA)).toBeNull()
    expect(runtimeForExactSessionBinding(bindingB)).toBe('runtime-b')
  })

  it('clears stale state from a legacy tile whose owner cannot be resolved before cold-opening B', () => {
    const binding = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeB })!
    $sessionTiles.set([
      {
        storedSessionId: 'shared-id',
        ownerRoute: { profile: 'legacy-only' } as unknown as SessionTile['ownerRoute'],
        runtimeId: 'runtime-legacy'
      }
    ])

    expect(openExactSidebarSession({ binding, navigate, placement: 'tab' })).toBe('rebound-cold')
    expect(mocks.dropSessionState).toHaveBeenCalledWith('runtime-legacy')
    expect(mocks.patchSessionTile).toHaveBeenCalledWith('shared-id', {
      error: undefined,
      ownerRoute: binding.ownerRoute,
      runtimeId: undefined
    })
  })

  it('commits main ownership and explicitly cold-resumes through the clicked owner', () => {
    const binding = normalizeSessionBinding({ storedSessionId: 'shared-id', ownerRoute: routeB })!

    expect(openExactSidebarSession({ binding, navigate, placement: 'main' })).toBe('created-cold')
    expect(mocks.requestSessionResume).toHaveBeenCalledWith('shared-id', binding.ownerRoute)
    expect(mocks.openSession).toHaveBeenCalledOnce()
  })
})
