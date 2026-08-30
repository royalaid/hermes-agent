import { activeConnectionScopeSuffix } from '@/lib/connection-scoped'
import { readJson, writeJson } from '@/lib/storage'

import type { SessionOwnerRoute } from './session-request-router'

export interface SessionBinding {
  storedSessionId: string
  ownerRoute: SessionOwnerRoute & { targetProfile: string }
}

interface RuntimeBinding {
  binding: SessionBinding
  generation: number
  runtimeId: null | string
}

export interface SessionBindingRuntimeAdapter {
  detach(storedSessionId: string): null | string
}

const MAIN_BINDING_KEY = 'hermes.desktop.mainSessionBinding.v1'
const runtimeBindings = new Map<string, RuntimeBinding>()
const generations = new Map<string, number>()
let runtimeAdapter: SessionBindingRuntimeAdapter | null = null

function normalizedProfile(value: unknown): string {
  return typeof value === 'string' ? value.trim() || 'default' : 'default'
}

/** Parse and canonicalize untrusted binding data. Identifiers remain opaque and
 * case-sensitive; only surrounding whitespace and profile defaults normalize. */
export function normalizeSessionBinding(value: unknown): SessionBinding | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const candidate = value as { ownerRoute?: unknown; storedSessionId?: unknown }
  const route = candidate.ownerRoute

  if (!route || typeof route !== 'object') {
    return null
  }

  const rawRoute = route as Record<string, unknown>
  const storedSessionId = typeof candidate.storedSessionId === 'string' ? candidate.storedSessionId.trim() : ''
  const connectionId = typeof rawRoute.connectionId === 'string' ? rawRoute.connectionId.trim() : ''

  if (!storedSessionId || !connectionId || typeof rawRoute.profile !== 'string') {
    return null
  }

  const mode = rawRoute.mode

  if (mode !== undefined && mode !== 'local' && mode !== 'remote') {
    return null
  }

  const profile = normalizedProfile(rawRoute.profile)

  const targetProfile =
    rawRoute.targetProfile === undefined || rawRoute.targetProfile === null
      ? profile
      : normalizedProfile(rawRoute.targetProfile)

  return {
    storedSessionId,
    ownerRoute: {
      connectionId,
      ...(mode ? { mode } : {}),
      profile,
      targetProfile
    }
  }
}

export function sessionBindingKey(binding: SessionBinding): string {
  const normalized = normalizeSessionBinding(binding)

  if (!normalized) {
    return ''
  }

  const { ownerRoute, storedSessionId } = normalized

  return JSON.stringify([storedSessionId, ownerRoute.connectionId, ownerRoute.profile, ownerRoute.targetProfile])
}

export function bindingsEqual(a: null | SessionBinding | undefined, b: null | SessionBinding | undefined): boolean {
  if (!a || !b) {
    return false
  }

  const aKey = sessionBindingKey(a)

  return Boolean(aKey) && aKey === sessionBindingKey(b)
}

function mainBindingStorageKey(profile: string, connectionScopeSuffix = activeConnectionScopeSuffix()): string {
  return `${MAIN_BINDING_KEY}.profile.${encodeURIComponent(normalizedProfile(profile))}${connectionScopeSuffix}`
}

export function setMainSessionBinding(
  binding: SessionBinding,
  profile: string,
  connectionScopeSuffix?: string
): boolean {
  const normalized = normalizeSessionBinding(binding)

  if (!normalized) {
    return false
  }

  writeJson(mainBindingStorageKey(profile, connectionScopeSuffix), normalized)

  return true
}

export function clearMainSessionBinding(profile: string, connectionScopeSuffix?: string): void {
  writeJson(mainBindingStorageKey(profile, connectionScopeSuffix), null)
}

/** Persisted main authority is usable only for the remembered route's exact id. */
export function getMainSessionBinding(
  rememberedStoredSessionId: string,
  profile: string,
  connectionScopeSuffix?: string
): SessionBinding | null {
  const binding = normalizeSessionBinding(readJson<unknown>(mainBindingStorageKey(profile, connectionScopeSuffix)))

  return binding?.storedSessionId === rememberedStoredSessionId.trim() ? binding : null
}

export function nextSessionBindingGeneration(storedSessionId: string): number {
  const id = storedSessionId.trim()
  const generation = (generations.get(id) ?? 0) + 1
  generations.set(id, generation)

  return generation
}

export function currentSessionBindingGeneration(storedSessionId: string): number {
  return generations.get(storedSessionId.trim()) ?? 0
}

/** Make one exact owner authoritative before an asynchronous resume starts.
 * A competing owner advances the generation, fencing every older completion. */
export function claimSessionBinding(binding: SessionBinding): number {
  const normalized = normalizeSessionBinding(binding)

  if (!normalized) {
    return -1
  }

  const current = runtimeBindings.get(normalized.storedSessionId)

  if (current && bindingsEqual(current.binding, normalized)) {
    return current.generation
  }

  const generation = nextSessionBindingGeneration(normalized.storedSessionId)
  runtimeBindings.set(normalized.storedSessionId, { binding: normalized, generation, runtimeId: null })

  return generation
}

export function bindRuntimeToSession(binding: SessionBinding, runtimeId: string, generation?: number): boolean {
  const normalized = normalizeSessionBinding(binding)
  const id = runtimeId.trim()

  if (!normalized || !id) {
    return false
  }

  const current = runtimeBindings.get(normalized.storedSessionId)
  const nextGeneration = generation ?? current?.generation ?? currentSessionBindingGeneration(normalized.storedSessionId)

  if (
    current &&
    (!bindingsEqual(current.binding, normalized) || (generation !== undefined && current.generation !== generation))
  ) {
    return false
  }

  runtimeBindings.set(normalized.storedSessionId, {
    binding: normalized,
    generation: nextGeneration,
    runtimeId: id
  })

  return true
}

export function runtimeForExactSessionBinding(binding: SessionBinding): string | null {
  const normalized = normalizeSessionBinding(binding)

  if (!normalized) {
    return null
  }

  const record = runtimeBindings.get(normalized.storedSessionId)

  return record && bindingsEqual(record.binding, normalized) ? record.runtimeId : null
}

export function currentSessionBinding(storedSessionId: string): SessionBinding | null {
  return runtimeBindings.get(storedSessionId.trim())?.binding ?? null
}

/** Admit an asynchronous runtime event only when its source still owns the
 * stored id. Tagged events are authoritative for their connection/profile;
 * untagged events are accepted only for the already-bound runtime or the
 * explicitly active legacy runtime. Callers must check this before mutating
 * any stored-id keyed cache. */
export function acceptsSessionRuntimeSource(
  storedSessionId: string,
  runtimeId: string,
  sourceOwner?: SessionOwnerRoute,
  legacyActive = false
): boolean {
  const id = storedSessionId.trim()
  const runtime = runtimeId.trim()

  if (!id || !runtime) {
    return false
  }

  const current = runtimeBindings.get(id)

  if (sourceOwner) {
    const sourceConnectionId = sourceOwner.connectionId.trim()
    const sourceProfile = sourceOwner.profile.trim() || 'default'

    if (!sourceConnectionId || !sourceOwner.profile) {
      return false
    }

    return (
      !current ||
      (current.binding.ownerRoute.connectionId === sourceConnectionId &&
        current.binding.ownerRoute.profile === sourceProfile)
    )
  }

  // An untagged legacy event can establish the first owner when no competing
  // binding exists. Once an owner is authoritative, only that exact runtime
  // may continue using the legacy path; the active-runtime hint is retained
  // for callers that need to prove the primary case explicitly.
  return current ? current.runtimeId === runtime : legacyActive || !current
}

export function invalidateSessionRuntimeBinding(storedSessionId: string): null | string {
  const id = storedSessionId.trim()
  const record = runtimeBindings.get(id)
  runtimeBindings.delete(id)
  nextSessionBindingGeneration(id)

  return record?.runtimeId ?? null
}

export function sessionBindingOwnsGeneration(binding: SessionBinding, generation: number): boolean {
  const record = runtimeBindings.get(binding.storedSessionId)

  return Boolean(record && record.generation === generation && bindingsEqual(record.binding, binding))
}

export function setSessionBindingRuntimeAdapter(adapter: SessionBindingRuntimeAdapter | null): void {
  runtimeAdapter = adapter
}

/** Compare before detaching: same owner preserves its warm runtime; a different
 * owner advances the generation before synchronously clearing the cache adapter. */
export function detachRuntimeForSession(
  next: SessionBinding,
  previous: null | SessionBinding | undefined
): { detached: boolean; runtimeId: null | string } {
  if (previous && bindingsEqual(next, previous)) {
    return { detached: false, runtimeId: runtimeForExactSessionBinding(next) }
  }

  if (!previous) {
    return { detached: false, runtimeId: null }
  }

  const runtimeId = runtimeAdapter?.detach(next.storedSessionId) ?? invalidateSessionRuntimeBinding(next.storedSessionId)

  // The adapter clears the renderer cache; the central registry and generation
  // are always retired here as the binding authority.
  if (runtimeBindings.has(next.storedSessionId)) {
    invalidateSessionRuntimeBinding(next.storedSessionId)
  }

  return { detached: true, runtimeId }
}

export function _resetSessionBindingsForTests(): void {
  runtimeBindings.clear()
  generations.clear()
  runtimeAdapter = null
}
