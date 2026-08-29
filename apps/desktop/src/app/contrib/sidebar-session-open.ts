import type { SessionInfo } from '@/hermes'
import { requestSessionResume } from '@/store/session'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

import { openSession, type OpenSessionNavigate } from '../open-session'

/**
 * Resume the exact sidebar row the user selected, preserving its profile and
 * connection identity before applying the sidebar's navigation policy.
 */
export function openSidebarSession(sessionId: string, session: SessionInfo | undefined, navigate: OpenSessionNavigate): void {
  const rowProfile = session?.profile?.trim()

  if (rowProfile) {
    requestSessionResume(sessionId, {
      connectionId: session?.connection_id?.trim() || 'local',
      ...(session?.connection_id?.trim() ? {} : { mode: 'local' as const }),
      profile: rowProfile,
      targetProfile: rowProfile
    })
  }

  openSession(sessionId, navigate, $sidebarSessionsOpenInNewTab.get() ? 'tab' : 'in-place')
}
