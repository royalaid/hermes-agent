import type { OpenSessionNavigate } from '@/app/open-session'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $selectedStoredSessionId,
  requestSessionResume,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId
} from '@/store/session'
import {
  claimSessionBinding,
  detachRuntimeForSession,
  getMainSessionBinding,
  invalidateSessionRuntimeBinding,
  normalizeSessionBinding,
  type SessionBinding,
  setMainSessionBinding
} from '@/store/session-binding'
import {
  $sessionTiles,
  dropSessionState,
  patchSessionTile,
  type SessionTile
} from '@/store/session-states'

import { openSession } from '../open-session'

export type ExactSidebarOpenOutcome =
  | 'created-cold'
  | 'focused-warm'
  | 'moved-to-main'
  | 'rebound-cold'
  | 'unresolved-owner'

export interface ExactSidebarOpenRequest {
  binding: SessionBinding
  navigate: OpenSessionNavigate
  placement: 'main' | 'tab' | 'window'
}

function tileBinding(tile: SessionTile | undefined): SessionBinding | null {
  return tile?.ownerRoute
    ? normalizeSessionBinding({ ownerRoute: tile.ownerRoute, storedSessionId: tile.storedSessionId })
    : null
}

/** The one ordinary-sidebar coordinator. It compares canonical authority,
 * fences a competing runtime/presentation synchronously, commits the new owner,
 * then delegates placement. Callers never sequence invalidation and resume. */
export function openExactSidebarSession({
  binding: rawBinding,
  navigate,
  placement
}: ExactSidebarOpenRequest): ExactSidebarOpenOutcome {
  const binding = normalizeSessionBinding(rawBinding)

  if (!binding) {
    return 'unresolved-owner'
  }

  const { storedSessionId } = binding
  const tiles = $sessionTiles.get()
  const tile = tiles.find(candidate => candidate.storedSessionId === storedSessionId)
  const selectedInMain = $selectedStoredSessionId.get() === storedSessionId
  const profile = $activeGatewayProfile.get()
  const previous = tileBinding(tile) ?? (selectedInMain ? getMainSessionBinding(storedSessionId, profile) : null)
  const { detached, runtimeId } = detachRuntimeForSession(binding, previous)
  const legacyTileOwnerUnresolved = Boolean(tile && !tileBinding(tile))
  const legacyTileRuntimeId = legacyTileOwnerUnresolved ? tile?.runtimeId ?? null : null
  const coldRebind = detached || legacyTileOwnerUnresolved
  const sameOwnerWarm = !coldRebind && Boolean(previous && runtimeId)

  if (legacyTileOwnerUnresolved) {
    invalidateSessionRuntimeBinding(storedSessionId)
  }

  claimSessionBinding(binding)

  if (coldRebind) {
    for (const id of new Set([runtimeId, legacyTileRuntimeId, selectedInMain ? $activeSessionId.get() : null])) {
      if (id) {
        dropSessionState(id)
      }
    }

    if (tile) {
      patchSessionTile(storedSessionId, { error: undefined, ownerRoute: binding.ownerRoute, runtimeId: undefined })
    }

    if (selectedInMain) {
      setActiveSessionId(null)
      setAwaitingResponse(false)
      setBusy(false)
      setMessages([])

      if (placement !== 'main') {
        setSelectedStoredSessionId(null)
      }
    }
  }

  if (placement === 'main') {
    setMainSessionBinding(binding, profile)

    if (!sameOwnerWarm) {
      requestSessionResume(storedSessionId, binding.ownerRoute)
    }
  } else if (placement === 'tab' && tile && !coldRebind && previous) {
    openSession(storedSessionId, navigate, 'tab', { ownerRoute: binding.ownerRoute, workspaceMode: 'sessions' })

    return 'focused-warm'
  }

  openSession(storedSessionId, navigate, placement, { ownerRoute: binding.ownerRoute, workspaceMode: 'sessions' })

  if (sameOwnerWarm) {
    return 'focused-warm'
  }

  if (coldRebind) {
    return 'rebound-cold'
  }

  if (placement === 'main' && tile) {
    return 'moved-to-main'
  }

  return 'created-cold'
}
