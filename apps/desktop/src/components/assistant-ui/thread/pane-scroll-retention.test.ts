import { cleanup, renderHook } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { usePaneScrollRetention } from './pane-scroll-retention'

/**
 * A hidden keep-alive tab skips its contents, which collapses the transcript
 * scroller's boxes and lets use-stick-to-bottom's ResizeObserver zero the
 * reader's position and clear the bottom-lock latch — then scroll to the bottom
 * on reveal. These tests pin the save/restore contract: what is snapshotted,
 * what is re-asserted, and that a reader who was following the bottom is not
 * touched.
 *
 * jsdom has no layout, so the library's observer never fires here. The zeroing
 * is therefore SIMULATED (the scroller is mutated while hidden, exactly as the
 * observer would) — that is the part jsdom can express. Whether real layout
 * emits the zero-size resize at all, and whether the restore lands ahead of the
 * observer callback in a real frame, is an electron/e2e check.
 */

const scroller = (scrollTop = 0) => {
  const el = document.createElement('div')
  el.scrollTop = scrollTop

  return { current: el } as MutableRefObject<HTMLElement | null>
}

function mount(scrollRef: MutableRefObject<HTMLElement | null>, stopScroll: () => void, isAtBottom: boolean) {
  return renderHook(
    ({ atBottom, visible }: { atBottom: boolean; visible: boolean }) =>
      usePaneScrollRetention({ isAtBottom: atBottom, paneVisible: visible, scrollRef, stopScroll }),
    { initialProps: { atBottom: isAtBottom, visible: true } }
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('hidden-pane transcript scroll retention', () => {
  it('restores a scrolled-up reader and drops the bottom lock on reveal', () => {
    const scrollRef = scroller(840)
    const stopScroll = vi.fn()
    const { rerender } = mount(scrollRef, stopScroll, false)

    rerender({ atBottom: false, visible: false })
    expect(stopScroll).not.toHaveBeenCalled()

    // What the observer does to a scroller with no boxes: scrollTop written
    // against a zero target, and the escaped-from-lock latch cleared because
    // -1 reads as "near bottom".
    scrollRef.current!.scrollTop = 0
    rerender({ atBottom: true, visible: false })

    rerender({ atBottom: true, visible: true })

    // stopScroll re-asserts the escaped latch, which is also what makes the
    // reveal's positive resize abort instead of animating to the bottom.
    expect(stopScroll).toHaveBeenCalledTimes(1)
    expect(scrollRef.current!.scrollTop).toBe(840)
  })

  it('snapshots the position the reader had at the hide, not a later one', () => {
    const scrollRef = scroller(500)
    const stopScroll = vi.fn()
    const { rerender } = mount(scrollRef, stopScroll, false)

    rerender({ atBottom: false, visible: false })

    // Two rounds of observer noise while hidden must not re-snapshot.
    scrollRef.current!.scrollTop = 0
    rerender({ atBottom: true, visible: false })
    scrollRef.current!.scrollTop = 0
    rerender({ atBottom: false, visible: false })

    rerender({ atBottom: false, visible: true })

    expect(scrollRef.current!.scrollTop).toBe(500)
  })

  it('leaves a reader who was following the bottom alone', () => {
    const scrollRef = scroller(1200)
    const stopScroll = vi.fn()
    const { rerender } = mount(scrollRef, stopScroll, true)

    rerender({ atBottom: true, visible: false })
    scrollRef.current!.scrollTop = 0
    rerender({ atBottom: true, visible: true })

    // No restore write and no lock drop: following the bottom across a reveal
    // is the behaviour they already have, and the library owns it.
    expect(stopScroll).not.toHaveBeenCalled()
    expect(scrollRef.current!.scrollTop).toBe(0)
  })

  it('does not touch the scroller while the pane simply stays visible', () => {
    const scrollRef = scroller(300)
    const stopScroll = vi.fn()
    const { rerender } = mount(scrollRef, stopScroll, false)

    rerender({ atBottom: true, visible: true })
    rerender({ atBottom: false, visible: true })
    scrollRef.current!.scrollTop = 310

    rerender({ atBottom: false, visible: true })

    expect(stopScroll).not.toHaveBeenCalled()
    expect(scrollRef.current!.scrollTop).toBe(310)
  })
})
