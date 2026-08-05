import '@/styles.css'

import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { Thread } from '@/components/assistant-ui/thread'
import { toChatMessages } from '@/lib/chat-messages'

const createdAt = new Date('2026-08-03T00:00:00.000Z')

const [hydratedMessage] = toChatMessages([
  {
    role: 'assistant',
    content: 'Fixture complete.',
    reasoning:
      'Analyzing Pi core cut selection algorithm\nClarifying cut point handling in tool results\n\nExamining payload rewrite excluding pre-compaction kept window\nVerifying serializer handling of compacted data',
    codex_reasoning_items: [
      {
        type: 'reasoning',
        id: 'rs_fixture_a',
        summary: [
          { type: 'summary_text', text: 'Analyzing Pi core cut selection algorithm' },
          { type: 'summary_text', text: 'Clarifying cut point handling in tool results' }
        ]
      },
      {
        type: 'reasoning',
        id: 'rs_fixture_b',
        summary: [
          { type: 'summary_text', text: 'Examining payload rewrite excluding pre-compaction kept window' },
          { type: 'summary_text', text: 'Verifying serializer handling of compacted data' }
        ]
      }
    ],
    timestamp: createdAt.getTime()
  }
])

const message = {
  id: 'reasoning-layout-harness',
  role: 'assistant',
  content: hydratedMessage.parts,
  status: { type: 'complete', reason: 'stop' },
  createdAt,
  metadata: {
    unstable_state: null,
    unstable_annotations: [],
    unstable_data: [],
    steps: [],
    custom: {}
  }
} as ThreadMessage

function ReasoningLayoutHarness() {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [message],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <main className="mx-auto h-screen max-w-3xl bg-(--ui-chat-surface-background) p-8" data-slot="reasoning-layout-harness">
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ReasoningLayoutHarness />
  </StrictMode>
)
