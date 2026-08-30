import type { SessionInfo } from '@/hermes'
import { normalizeSessionBinding } from '@/store/session-binding'
import { sessionOwnerRouteFromRow } from '@/store/session-request-router'
import { $sidebarSessionsOpenInNewTab } from '@/store/sidebar-open-preference'

import { openSession, type OpenSessionIntent, type OpenSessionNavigate } from '../open-session'

import { openExactSidebarSession } from './exact-sidebar-session'

/** Ordinary sidebar rows enter the owner-aware binding model once. */
export function openSidebarSession(
  sessionId: string,
  session: SessionInfo | undefined,
  navigate: OpenSessionNavigate,
  intent?: Extract<OpenSessionIntent, 'tab' | 'window'>
): void {
  const ownerRoute = sessionOwnerRouteFromRow(session)
  const binding = ownerRoute ? normalizeSessionBinding({ ownerRoute, storedSessionId: sessionId }) : null
  const placement = intent ?? ($sidebarSessionsOpenInNewTab.get() ? 'tab' : 'main')

  if (!binding) {
    openSession(sessionId, navigate, placement)

    return
  }

  openExactSidebarSession({ binding, navigate, placement })
}
