import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

const mocks = vi.hoisted(() => ({
  openSession: vi.fn(),
  requestSessionResume: vi.fn()
}))

vi.mock('@/store/session', () => ({
  requestSessionResume: (...args: unknown[]) => mocks.requestSessionResume(...args)
}))

vi.mock('../open-session', () => ({
  openSession: (...args: unknown[]) => mocks.openSession(...args)
}))

import { openSidebarSession } from './sidebar-session-open'

const navigate = vi.fn()

const session = (profile: string, connectionId: string): SessionInfo =>
  ({ connection_id: connectionId, id: 'shared-id', profile }) as SessionInfo

describe('openSidebarSession', () => {
  beforeEach(() => {
    mocks.openSession.mockReset()
    mocks.requestSessionResume.mockReset()
    navigate.mockReset()
    $sidebarSessionsOpenInNewTab.set(true)
  })

  it('uses tab intent by default while routing identical stored ids through the clicked row owner', () => {
    openSidebarSession('shared-id', session('profile-a', 'connection-a'), navigate)
    openSidebarSession('shared-id', session('profile-b', 'connection-b'), navigate)

    expect(mocks.requestSessionResume).toHaveBeenNthCalledWith(1, 'shared-id', {
      connectionId: 'connection-a',
      profile: 'profile-a',
      targetProfile: 'profile-a'
    })
    expect(mocks.requestSessionResume).toHaveBeenNthCalledWith(2, 'shared-id', {
      connectionId: 'connection-b',
      profile: 'profile-b',
      targetProfile: 'profile-b'
    })
    expect(mocks.openSession).toHaveBeenNthCalledWith(1, 'shared-id', navigate, 'tab')
    expect(mocks.openSession).toHaveBeenNthCalledWith(2, 'shared-id', navigate, 'tab')
  })

  it('uses in-place intent when the persisted preference selects the main tab', () => {
    $sidebarSessionsOpenInNewTab.set(false)

    openSidebarSession('shared-id', session('profile-a', 'connection-a'), navigate)

    expect(mocks.requestSessionResume).toHaveBeenCalledOnce()
    expect(mocks.openSession).toHaveBeenCalledWith('shared-id', navigate, 'in-place')
  })
})
