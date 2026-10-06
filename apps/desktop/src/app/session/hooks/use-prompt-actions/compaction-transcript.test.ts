import { expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { mergeCompressedTranscript } from './compaction-transcript'

const user = (id: string, timestamp: number, rowId?: number): ChatMessage => ({
  id, timestamp, rowId, role: 'user', parts: [{ type: 'text', text: 'Repeated prompt' }]
})

it('retains mounted IDs for unversioned compression copies without borrowing durable metadata', () => {
  const previous = [user('mounted-1', 10, 1), user('mounted-2', 20, 2)]
  const active = [user('position-3', 20)]
  const merged = mergeCompressedTranscript(active, previous)
  expect(merged.map(row => row.id)).toEqual(['mounted-1', 'mounted-2'])
  expect(merged[1].rowId).toBeUndefined()
})

it('keeps repeated prose as distinct source occurrences and preserves new authoritative row IDs', () => {
  const previous = [user('old-1', 10, 1), user('old-2', 20, 2)]
  const merged = mergeCompressedTranscript([user('copy-2', 20, 200), user('new-3', 30, 300)], previous)
  expect(merged.map(row => row.id)).toEqual(['old-1', 'old-2', 'new-3'])
  expect(merged[1].rowId).toBe(200)
})

it('refuses ambiguous same-timestamp ownership rather than consuming a historical occurrence', () => {
  const merged = mergeCompressedTranscript([user('unknown', 10)], [user('old-1', 10), user('old-2', 10)])
  expect(new Set(merged.map(row => row.id))).toEqual(new Set(['old-1', 'old-2', 'unknown']))
})

it('does not alias structured turns with different tool identities and identical prose', () => {
  const assistant = (id: string, toolCallId: string): ChatMessage => ({
    id, timestamp: 10, role: 'assistant', parts: [
      { type: 'text', text: 'Working' },
      { type: 'tool-call', toolCallId, toolName: 'terminal', args: {}, argsText: '{}' }
    ]
  })
  const merged = mergeCompressedTranscript([assistant('new', 'tool-new')], [assistant('old', 'tool-old')])
  expect(new Set(merged.map(row => row.id))).toEqual(new Set(['old', 'new']))
})
