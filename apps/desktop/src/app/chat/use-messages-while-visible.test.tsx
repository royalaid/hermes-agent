import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { atom, type ReadableAtom } from 'nanostores'
import { type ReactElement, useContext, useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type { ChatMessage } from '@/lib/chat-messages'

import { useMessagesWhileVisible } from './index'

const message = (id: string): ChatMessage => ({ id, role: 'assistant', parts: [{ type: 'text', text: `body-${id}` }] })

const transcript = (...ids: string[]): ChatMessage[] => ids.map(message)

const ids = (messages: readonly ChatMessage[]) => messages.map(m => m.id).join(',')

type TranscriptBody = (props: { $messages: ReadableAtom<ChatMessage[]> }) => ReactElement

const Transcript: TranscriptBody = ({ $messages }) => (
  <div data-testid="transcript">{ids(useMessagesWhileVisible($messages))}</div>
)

/**
 * Control body: the pre-U2 shape of the hook, inlined. Only the harness-validity
 * test below renders it — see that test for why it has to exist.
 */
const EagerTranscript: TranscriptBody = ({ $messages }) => {
  const visible = useContext(PaneVisibleContext)
  const [messages, setMessages] = useState(() => $messages.get())

  useEffect(
    () => (visible ? $messages.subscribe(value => setMessages(value as ChatMessage[])) : undefined),
    [$messages, visible]
  )

  return <div data-testid="transcript">{ids(messages)}</div>
}

/** Models a keep-alive pane layer: a scroller that stays mounted across the
 *  hide/reveal round-trip, under the visibility context the real layer provides. */
function Pane({
  $messages,
  body: Body = Transcript,
  visible
}: {
  $messages: ReadableAtom<ChatMessage[]>
  body?: TranscriptBody
  visible: boolean
}) {
  return (
    <PaneVisibleContext.Provider value={visible}>
      <div data-testid="scroller" style={{ overflow: 'auto' }}>
        <Body $messages={$messages} />
      </div>
    </PaneVisibleContext.Provider>
  )
}

describe('useMessagesWhileVisible', () => {
  afterEach(cleanup)

  const shown = () => screen.getByTestId('transcript').textContent

  it('freezes a hidden pane: store publishes never reach it', () => {
    const $messages = atom(transcript('a'))
    const { rerender } = render(<Pane $messages={$messages} visible={false} />)

    expect(shown()).toBe('a')

    act(() => $messages.set(transcript('a', 'b')))
    act(() => $messages.set(transcript('a', 'b', 'c')))
    rerender(<Pane $messages={$messages} visible={false} />)

    // The hidden tab never re-rendered the stream it missed.
    expect(shown()).toBe('a')
  })

  it('converges on the current store value after a reveal', async () => {
    const $messages = atom(transcript('a'))
    const { rerender } = render(<Pane $messages={$messages} visible={false} />)

    act(() => $messages.set(transcript('a', 'b', 'c')))
    rerender(<Pane $messages={$messages} visible />)

    await waitFor(() => expect(shown()).toBe(ids($messages.get())))
  })

  it('applies updates promptly while the pane is visible', () => {
    const $messages = atom(transcript('a'))

    render(<Pane $messages={$messages} visible />)

    // No waitFor: a live publish must still land on its own commit, or the
    // visible transcript would trail the stream by a scheduler tick per token.
    act(() => $messages.set(transcript('a', 'b')))
    expect(shown()).toBe('a,b')

    act(() => $messages.set(transcript('a', 'b', 'c')))
    expect(shown()).toBe('a,b,c')
  })

  it('re-freezes after a reveal → hide round-trip', async () => {
    const $messages = atom(transcript('a'))
    const { rerender } = render(<Pane $messages={$messages} visible />)

    act(() => $messages.set(transcript('a', 'b')))
    await waitFor(() => expect(shown()).toBe('a,b'))

    rerender(<Pane $messages={$messages} visible={false} />)
    act(() => $messages.set(transcript('a', 'b', 'c')))

    expect(shown()).toBe('a,b')
  })
})

interface PaneControls {
  /** Any unrelated urgent update — stands in for whatever else the app paints
   *  while the catch-up is still pending. */
  bump: () => void
  /** Reveal the pane, as the tab click does. */
  reveal: () => void
}

function LivePane({
  $messages,
  body,
  controls
}: {
  $messages: ReadableAtom<ChatMessage[]>
  body?: TranscriptBody
  controls: PaneControls
}) {
  const [visible, setVisible] = useState(false)
  const [bumps, setBumps] = useState(0)

  controls.reveal = () => setVisible(true)
  controls.bump = () => setBumps(n => n + 1)

  return (
    <>
      <span data-testid="bumps">{bumps}</span>
      <Pane $messages={$messages} body={body} visible={visible} />
    </>
  )
}

/**
 * The reveal driven outside React's act environment, so transition work stays
 * deferred instead of being drained by `act`.
 *
 * The observable that separates a transition catch-up from an eager one: an
 * URGENT update landing after the reveal. React renders it against the
 * still-committed (frozen) transcript and leaves the catch-up pending, whereas
 * an eager catch-up rides along on that same urgent commit — which is exactly
 * what makes it ride the tab-click frame in the app. jsdom has no paint, so
 * this stands in for "the click paints before the full-transcript commit".
 */
describe('useMessagesWhileVisible reveal (outside act)', () => {
  let container: HTMLDivElement
  let root: Root
  let actEnvironment: unknown
  const controls: PaneControls = { bump: () => undefined, reveal: () => undefined }

  const mount = (element: ReactElement) => {
    actEnvironment = (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT
    ;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = false
    container = window.document.createElement('div')
    window.document.body.append(container)
    root = createRoot(container)
    flushSync(() => root.render(element))
  }

  const transcriptNode = () => container.querySelector('[data-testid="transcript"]')
  const text = () => transcriptNode()?.textContent
  const bumps = () => container.querySelector('[data-testid="bumps"]')?.textContent
  const scroller = () => container.querySelector<HTMLElement>('[data-testid="scroller"]')

  /** Let the scheduler drain its pending transition work. */
  const settle = () => new Promise(resolve => setTimeout(resolve, 20))

  afterEach(() => {
    flushSync(() => root.unmount())
    container.remove()
    ;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = actEnvironment
  })

  it('keeps the frozen transcript rendered while urgent work paints, then catches up in place', async () => {
    const $messages = atom(transcript('a'))

    mount(<LivePane $messages={$messages} controls={controls} />)
    await settle()
    expect(text()).toBe('a')

    // The pane misses a long stream while hidden, then the user clicks its tab.
    $messages.set(transcript('a', 'b', 'c', 'd', 'e'))

    const frozenNode = transcriptNode()

    flushSync(() => controls.reveal())
    flushSync(() => controls.bump())

    // Urgent work painted; the pane still shows its pre-hide transcript. No
    // blanking, no skeleton, no full-transcript commit riding that frame.
    expect(bumps()).toBe('1')
    expect(text()).toBe('a')

    await settle()

    // ...and the catch-up lands in place, in the same node.
    expect(text()).toBe(ids($messages.get()))
    expect(transcriptNode()).toBe(frozenNode)
  })

  it('does not disturb a scroll made while the catch-up is pending', async () => {
    const $messages = atom(transcript('a', 'b'))

    mount(<LivePane $messages={$messages} controls={controls} />)
    await settle()

    $messages.set(transcript('a', 'b', 'c', 'd', 'e'))
    flushSync(() => controls.reveal())
    expect(text()).toBe('a,b')

    // The user scrolls the still-frozen transcript before the catch-up commits.
    const scrolledNode = scroller()

    if (!scrolledNode) {
      throw new Error('missing scroller')
    }

    scrolledNode.scrollTop = 120

    await settle()

    expect(text()).toBe(ids($messages.get()))
    // The same scroller element, still holding the user's position: the deferred
    // commit neither re-anchors nor replaces it. jsdom has no layout, so this
    // pins the structural half of the invariant — nothing on this path writes
    // scrollTop or remounts the scroll container. Real-layout clamping (a short
    // frozen transcript growing under a scrolled viewport) belongs to the
    // electron/e2e project.
    expect(scroller()).toBe(scrolledNode)
    expect(scrolledNode.scrollTop).toBe(120)
  })

  it('keeps live publishes on their own frame once the catch-up has settled', async () => {
    const $messages = atom(transcript('a'))

    mount(<LivePane $messages={$messages} controls={controls} />)
    await settle()

    $messages.set(transcript('a', 'b'))
    flushSync(() => controls.reveal())
    await settle()
    expect(text()).toBe('a,b')

    // Streaming resumes. These are not catch-ups and must not be deferred.
    flushSync(() => $messages.set(transcript('a', 'b', 'c')))
    expect(text()).toBe('a,b,c')
  })

  it('discriminates: an eager catch-up rides the urgent frame instead', async () => {
    // Harness validity. Without this the two assertions above ("still 'a' while
    // urgent work paints") could pass for the wrong reason — e.g. if the effect
    // had not run yet, or if jsdom deferred every commit anyway. Rendering the
    // pre-U2 body through the identical sequence proves the observable actually
    // separates the two behaviors.
    const $messages = atom(transcript('a'))

    mount(<LivePane $messages={$messages} body={EagerTranscript} controls={controls} />)
    await settle()

    $messages.set(transcript('a', 'b', 'c'))
    flushSync(() => controls.reveal())
    flushSync(() => controls.bump())

    expect(bumps()).toBe('1')
    expect(text()).toBe('a,b,c')
  })
})
