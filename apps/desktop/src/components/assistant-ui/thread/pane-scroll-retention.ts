import { type MutableRefObject, useLayoutEffect, useRef } from 'react'

/**
 * Keeps a scrolled-up reader's position across a keep-alive tab hide/reveal.
 *
 * Hidden panes now skip their contents (`content-visibility: hidden` on the
 * pane's content wrapper). The transcript's scroller is a DESCENDANT of that
 * wrapper, so its boxes go away while the tab is inactive — and
 * use-stick-to-bottom watches the transcript with a ResizeObserver. A
 * zero-size resize makes the library write `scrollTop` on a scroller that
 * reports 0/0, read `targetScrollTop` as -1 (which clamps to "near bottom" and
 * CLEARS the escaped-from-lock latch), and then treat the reveal's 0 -> full
 * growth as a positive resize and scroll to the bottom. Net effect for someone
 * parked mid-history: their tab comes back at the newest turn. The previous
 * `visibility: hidden` produced no resize events at all, which is why this is
 * new (R2 is the acceptance bar; KTD3 pre-authorised this explicit save/restore
 * as the fallback for the CSS-only vehicle).
 *
 * The library stays the single scroll owner — this does not fight it per frame.
 * It snapshots at the HIDE commit and, only when the reader had escaped the
 * bottom lock, re-asserts that position at the REVEAL commit: `stopScroll()`
 * clears `isAtBottom`, which makes the observer's reveal-time scroll abort on
 * its first animation frame, and then `scrollTop` is written back.
 *
 * Both halves are layout effects on purpose. React runs them after the DOM
 * mutation that hides/reveals the pane but before the browser lays out, so the
 * snapshot still sees pre-hide geometry and the restore lands ahead of the
 * ResizeObserver callback for that same layout.
 *
 * Correct whether or not the engine actually emits the zero-size resize: if it
 * does not, the snapshot equals the live position, so the restore is a no-op
 * write plus an idempotent `stopScroll()` on an already-escaped lock. A reader
 * who was AT the bottom is left entirely alone — following the bottom across a
 * reveal is the behaviour they already have.
 */
export function usePaneScrollRetention({
  isAtBottom,
  paneVisible,
  scrollRef,
  stopScroll
}: {
  isAtBottom: boolean
  paneVisible: boolean
  scrollRef: MutableRefObject<HTMLElement | null>
  stopScroll: () => void
}) {
  // Where the reader was when the tab went away; null when they were following
  // the bottom (nothing to protect) or the pane is visible.
  const parkedScrollTopRef = useRef<null | number>(null)
  const wasVisibleRef = useRef(paneVisible)

  useLayoutEffect(() => {
    const wasVisible = wasVisibleRef.current
    wasVisibleRef.current = paneVisible
    const el = scrollRef.current

    if (!el) {
      return
    }

    if (!paneVisible) {
      // Only on the visible -> hidden EDGE. Once hidden the geometry is already
      // gone, so a later run (isAtBottom flips while hidden, which is exactly
      // the damage this guards) would record a meaningless 0.
      if (wasVisible) {
        parkedScrollTopRef.current = isAtBottom ? null : el.scrollTop
      }

      return
    }

    const parked = parkedScrollTopRef.current
    parkedScrollTopRef.current = null

    if (parked == null) {
      return
    }

    // Order matters: drop the bottom lock first so the reveal resize can't win
    // a frame, then put the reader back.
    stopScroll()
    el.scrollTop = parked
  }, [isAtBottom, paneVisible, scrollRef, stopScroll])
}
