import { AssistantRuntimeProvider, ExportedMessageRepository, type ThreadMessage } from '@assistant-ui/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'

import { Thread } from '.'

/**
 * The sticky human bubble measures its unclamped body through the shared
 * ResizeObserver and publishes the result as `--human-msg-full`. A hidden
 * keep-alive tab skips its contents, so every bubble in that transcript now
 * gets a ZERO-height resize on hide — which is an unlaid-out element, not a
 * measurement. Taking it churns state and rewrites the variable to 0px per
 * bubble per hide, and the clamp then has to be re-derived on reveal.
 */

const createdAt = new Date('2026-05-01T00:00:00.000Z')

// Several observers are live in this tree (the shared element observer plus
// use-stick-to-bottom's own), so a delivery has to be routed to the ones that
// actually observe the target — exactly as the browser would.
const observers = new Set<{ callback: (entries: ResizeObserverEntry[]) => void; targets: Set<Element> }>()

class RoutingResizeObserver {
  private entry: { callback: (entries: ResizeObserverEntry[]) => void; targets: Set<Element> }

  constructor(callback: (entries: ResizeObserverEntry[]) => void) {
    this.entry = { callback, targets: new Set() }
    observers.add(this.entry)
  }

  observe(target: Element) {
    this.entry.targets.add(target)
  }

  unobserve(target: Element) {
    this.entry.targets.delete(target)
  }

  disconnect() {
    observers.delete(this.entry)
  }
}

vi.stubGlobal('ResizeObserver', RoutingResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

afterEach(cleanup)

function userMessage(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'a prompt long enough to clamp' }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as ThreadMessage
}

function Harness() {
  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository: ExportedMessageRepository.fromArray([userMessage()]),
    isRunning: false,
    setMessages: () => {},
    onNew: async () => {},
    onEdit: async () => {},
    onCancel: async () => {},
    onReload: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread cwd={null} gateway={null} sessionId="session-1" />
    </AssistantRuntimeProvider>
  )
}

const resize = (target: Element, blockSize: number) =>
  act(() => {
    const entry = {
      borderBoxSize: [{ blockSize, inlineSize: 400 }],
      contentRect: { height: blockSize, width: 400 },
      target
    } as unknown as ResizeObserverEntry

    for (const observer of observers) {
      if (observer.targets.has(target)) {
        observer.callback([entry])
      }
    }
  })

describe('sticky human bubble clamp measurement', () => {
  it('ignores the zero-height resize a hidden pane delivers', async () => {
    render(<Harness />)

    const inner = await waitFor(() => {
      const node = document.querySelector('.sticky-human-clamp > div')

      if (!node) {
        throw new Error('clamp body not rendered')
      }

      return node
    })

    const outer = inner.parentElement as HTMLElement

    await resize(inner, 96)
    expect(outer.style.getPropertyValue('--human-msg-full')).toBe('96px')
    expect(outer.dataset.clamped).toBe('true')

    // The pane goes hidden: content-visibility skips the subtree, so the
    // observer reports 0. The last real measurement has to survive it.
    await resize(inner, 0)
    expect(outer.style.getPropertyValue('--human-msg-full')).toBe('96px')
    expect(outer.dataset.clamped).toBe('true')

    // A genuine change still lands.
    await resize(inner, 20)
    expect(outer.style.getPropertyValue('--human-msg-full')).toBe('20px')
    expect(outer.dataset.clamped).toBeUndefined()
  })
})
