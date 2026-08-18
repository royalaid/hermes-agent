import type { ChatMessage } from '@/lib/chat-messages'
import type { RpcEvent } from '@/types/hermes'

type GatewayEventHandler = (event: RpcEvent) => void
type SessionSeeder = (sessionId: string, messages: ChatMessage[]) => void
type SessionDiscarder = (sessionId: string) => void

let activeRegistration: null | {
  discardSession: SessionDiscarder
  generation: number
  handler: GatewayEventHandler
  seedSession: SessionSeeder
} = null
let generation = 0

/**
 * Narrow dev-probe seam for sending a synthetic frame through the exact
 * gateway callback installed by ContribWiring. It deliberately is not part of
 * the gateway store: the probe must not manufacture a second event path.
 */
export function registerPerfProbeGatewayHandler(
  handler: GatewayEventHandler,
  seedSession: SessionSeeder,
  discardSession: SessionDiscarder
): () => void {
  const registration = { discardSession, generation: ++generation, handler, seedSession }
  activeRegistration = registration

  return () => {
    // Effects can overlap during StrictMode/HMR. An old cleanup must never
    // unregister the newer callback, even when both renders received the same
    // memoized function identity.
    if (activeRegistration?.generation === registration.generation) {
      activeRegistration = null
    }
  }
}

/** Returns false while the app has not mounted (or after it unmounts). */
export function dispatchPerfProbeGatewayEvent(event: RpcEvent): boolean {
  const handler = activeRegistration?.handler

  if (!handler) {
    return false
  }

  handler(event)

  return true
}

/** Discard queued stream/coalescer work before the probe restores snapshots. */
export function discardPerfProbeSession(sessionId: string): boolean {
  const discardSession = activeRegistration?.discardSession

  if (!discardSession) {
    return false
  }

  discardSession(sessionId)

  return true
}

/** Seed the isolated runtime through ContribWiring's authoritative cache. */
export function seedPerfProbeSession(sessionId: string, messages: ChatMessage[]): boolean {
  const seedSession = activeRegistration?.seedSession

  if (!seedSession) {
    return false
  }

  seedSession(sessionId, messages)

  return true
}
