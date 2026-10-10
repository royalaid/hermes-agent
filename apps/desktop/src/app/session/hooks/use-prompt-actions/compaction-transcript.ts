import { mergeOlderTranscriptPage } from '@/app/chat/transcript-backfill'
import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'

const occurrence = (message: ChatMessage): string | null =>
  typeof message.timestamp === 'number' && !message.pending && !message.id.startsWith('assistant-stream-')
    ? JSON.stringify([
        message.role,
        message.timestamp,
        chatMessageText(message),
        message.attachmentRefs ?? [],
        message.parts.map(part => part.type === 'tool-call' ? [part.type, part.toolCallId] : [part.type])
      ])
    : null

/** Compression preserves authoring timestamps but its display RPC can omit
 * durable row IDs. Reuse unique mounted display identities at this known
 * compression boundary without transferring row IDs or other backend metadata. */
export function mergeCompressedTranscript(active: ChatMessage[], previous: ChatMessage[]): ChatMessage[] {
  const mounted = new Map<string, ChatMessage[]>()
  for (const message of previous) {
    const key = occurrence(message)
    if (key !== null) mounted.set(key, [...(mounted.get(key) ?? []), message])
  }

  const projectedCounts = new Map<string, number>()
  for (const message of active) {
    const key = occurrence(message)
    if (key !== null) projectedCounts.set(key, (projectedCounts.get(key) ?? 0) + 1)
  }

  const stable = active.map(message => {
    const key = occurrence(message)
    const candidates = key === null ? undefined : mounted.get(key)
    return candidates?.length === 1 && projectedCounts.get(key!) === 1
      ? { ...message, id: candidates[0]!.id }
      : message
  })
  return mergeOlderTranscriptPage(stable, previous)
}
