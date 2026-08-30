import type { SessionInfo } from '@/hermes'
import { normalizeSessionBinding } from '@/store/session-binding'
import { sessionOwnerRouteFromRow } from '@/store/session-request-router'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

import type { OpenSessionIntent, OpenSessionNavigate } from '../open-session'

import { openExactSidebarSession } from './exact-sidebar-session'

/** Ordinary sidebar rows enter the owner-aware binding model once. */
export function openSidebarSession(
  sessionId: string,
  session: SessionInfo | undefined,
  navigate: OpenSessionNavigate,
  intent?: Extract<OpenSessionIntent, 'tab' | 'window'>
): void {
  const rowProfile = session?.profile?.trim()

  const ownerRoute =
    sessionOwnerRouteFromRow(session) ??
    (rowProfile
      ? { connectionId: 'local', mode: 'local' as const, profile: rowProfile, targetProfile: rowProfile }
      : undefined)

  const binding = ownerRoute ? normalizeSessionBinding({ ownerRoute, storedSessionId: sessionId }) : null

  if (!binding) {
    return
  }

  openExactSidebarSession({
    binding,
    navigate,
    placement: intent ?? ($sidebarSessionsOpenInNewTab.get() ? 'tab' : 'main')
  })
}
