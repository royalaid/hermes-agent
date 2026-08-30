import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

const openExactSidebarSession = vi.fn()

vi.mock('./exact-sidebar-session', () => ({
  openExactSidebarSession: (...args: unknown[]) => openExactSidebarSession(...args)
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
  })

  it('passes explicit main placement for the persisted override', () => {
    $sidebarSessionsOpenInNewTab.set(false)
    openSidebarSession('shared-id', session('profile-a', 'connection-a'), navigate)

    expect(openExactSidebarSession).toHaveBeenCalledWith(
      expect.objectContaining({ placement: 'main' })
    )
  })

  it('fails closed when the row has no exact owner source', () => {
    openSidebarSession('shared-id', session('', ''), navigate)

    expect(openExactSidebarSession).not.toHaveBeenCalled()
  })
})
