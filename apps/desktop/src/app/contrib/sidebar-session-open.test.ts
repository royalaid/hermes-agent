import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

const openExactSidebarSession = vi.fn()
const openSession = vi.fn()

vi.mock('./exact-sidebar-session', () => ({
  openExactSidebarSession: (...args: unknown[]) => openExactSidebarSession(...args)
}))
vi.mock('../open-session', () => ({
  openSession: (...args: unknown[]) => openSession(...args)
}))

import { openSidebarSession } from './sidebar-session-open'

const navigate = vi.fn()

const session = (profile: string, connectionId: string): SessionInfo =>
  ({ connection_id: connectionId, id: 'shared-id', profile }) as SessionInfo

describe('openSidebarSession', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $sidebarSessionsOpenInNewTab.set(true)
  })

  it('makes one coordinator call with the clicked exact binding and default tab placement', () => {
    openSidebarSession(' shared-id ', session('profile-b', 'connection-b'), navigate)

    expect(openExactSidebarSession).toHaveBeenCalledOnce()
    expect(openExactSidebarSession).toHaveBeenCalledWith({
      binding: {
        storedSessionId: 'shared-id',
        ownerRoute: {
          connectionId: 'connection-b',
          profile: 'profile-b',
          targetProfile: 'profile-b'
        }
      },
      navigate,
      placement: 'tab'
    })
    expect(openSession).not.toHaveBeenCalled()
  })

  it('passes explicit main placement for the persisted override', () => {
    $sidebarSessionsOpenInNewTab.set(false)
    openSidebarSession('shared-id', session('profile-a', 'connection-a'), navigate)

    expect(openExactSidebarSession).toHaveBeenCalledWith(
      expect.objectContaining({ placement: 'main' })
    )
    expect(openSession).not.toHaveBeenCalled()
  })

  it('lets a modifier window gesture override the plain-click preference without losing the row owner', () => {
    $sidebarSessionsOpenInNewTab.set(false)
    openSidebarSession('shared-id', session('profile-b', 'connection-b'), navigate, 'window')

    expect(openExactSidebarSession).toHaveBeenCalledWith(
      expect.objectContaining({
        binding: expect.objectContaining({
          ownerRoute: expect.objectContaining({ connectionId: 'connection-b', profile: 'profile-b' })
        }),
        placement: 'window'
      })
    )
    expect(openSession).not.toHaveBeenCalled()
  })

  it('opens a profile-only row by id with default tab placement', () => {
    openSidebarSession('shared-id', session('remote-profile', ''), navigate)

    expect(openSession).toHaveBeenCalledOnce()
    expect(openSession).toHaveBeenCalledWith('shared-id', navigate, 'tab')
    expect(openExactSidebarSession).not.toHaveBeenCalled()
  })

  it('opens an ownerless server-search row by id', () => {
    openSidebarSession('server-only-id', { id: 'server-only-id' } as SessionInfo, navigate)

    expect(openSession).toHaveBeenCalledWith('server-only-id', navigate, 'tab')
    expect(openExactSidebarSession).not.toHaveBeenCalled()
  })

  it('preserves the main preference for a generic fallback', () => {
    $sidebarSessionsOpenInNewTab.set(false)

    openSidebarSession('shared-id', session('remote-profile', ''), navigate)

    expect(openSession).toHaveBeenCalledWith('shared-id', navigate, 'main')
    expect(openExactSidebarSession).not.toHaveBeenCalled()
  })

  it('preserves explicit window intent for a generic fallback', () => {
    openSidebarSession('server-only-id', { id: 'server-only-id' } as SessionInfo, navigate, 'window')

    expect(openSession).toHaveBeenCalledWith('server-only-id', navigate, 'window')
    expect(openExactSidebarSession).not.toHaveBeenCalled()
  })
})
