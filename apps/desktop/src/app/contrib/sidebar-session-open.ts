import type { SessionInfo } from '@/hermes'
import { requestSessionResume } from '@/store/session'
import { type SessionOwnerRoute, sessionOwnerRouteFromRow } from '@/store/session-request-router'
import { prepareSessionOwnerRetarget } from '@/store/session-states'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

import { openSession, type OpenSessionNavigate } from '../open-session'

/**
 * Resume the exact sidebar row the user selected, preserving its profile and
 * connection identity before applying the sidebar's navigation policy.
 */
export function openSidebarSession(sessionId: string, session: SessionInfo | undefined, navigate: OpenSessionNavigate): void {
  const rowProfile = session?.profile?.trim()

  const ownerRoute: SessionOwnerRoute | undefined =
    sessionOwnerRouteFromRow(session) ??
    (rowProfile
      ? { connectionId: 'local', mode: 'local', profile: rowProfile, targetProfile: rowProfile }
      : undefined)

  const intent = $sidebarSessionsOpenInNewTab.get() ? 'tab' : 'main'

  if (ownerRoute) {
    prepareSessionOwnerRetarget(sessionId, ownerRoute, intent === 'main')
    requestSessionResume(sessionId, ownerRoute)
  }

  openSession(
    sessionId,
    navigate,
    intent,
    ownerRoute ? { ownerRoute, workspaceMode: 'sessions' } : { workspaceMode: 'sessions' }
  )
}
