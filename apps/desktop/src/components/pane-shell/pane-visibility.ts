/**
 * Keep-alive visibility — the one policy every document-wide lookup must obey,
 * plus the containment policy that keeps hidden tabs out of layout.
 *
 * A tab group keeps each ever-active pane MOUNTED and hides the inactive ones
 * with `visibility: hidden` (see `tree/renderer/tree-group.tsx`), deliberately
 * preserving their layout box so scroll positions survive a tab round-trip. The
 * cost is that an inactive tab's rect is IDENTICAL to the visible tab's, so
 * neither selector order nor a rect hit-test can tell them apart. A lookup that
 * resolves "the chat surface / composer / viewport" from the document therefore
 * has to skip hidden panes, or it silently answers with the wrong tab.
 *
 * The second cost is layout: an invisible pane still lays out, so switching
 * tabs paid style+layout for EVERY kept transcript, not just the one being
 * shown. The pane layer therefore renders an inner CONTENT WRAPPER that skips
 * its contents while the tab is inactive. Its invariants:
 *
 * - `contain-intrinsic-size: auto <fallback>` is PERMANENT (both states). CSS
 *   only records an element's last-remembered size while that property is set
 *   AND the element is rendering its contents unskipped — applying it at hide
 *   time would record nothing and collapse every hide to the static fallback.
 *   Only `content-visibility: hidden` toggles.
 * - The wrapper grows intrinsically (`min-h-full`), never `h-full`: an
 *   extrinsic height would override the remembered-size placeholder, collapse
 *   the scroller's `scrollHeight` and clamp `scrollTop` to 0 on every hide.
 * - The wrapper is INSIDE the scroller, never the scroller itself, for the same
 *   reason — the scroll box and its `data-pane-hidden` marker stay exactly as
 *   they were, so the lookups below are unaffected.
 * - Skipped content is not laid out at all: nothing may MEASURE a hidden pane
 *   (rects read zero there). Hidden panes were already `visibility: hidden`, so
 *   nothing reads them today; the lookups here are how it stays that way.
 */

import { createContext, useContext, useState } from 'react'

import type { PaneLifecycle } from './pane-lifecycle'

/** Marks a mounted-but-hidden pane layer (an inactive tab in a stack). */
export const PANE_HIDDEN_ATTR = 'data-pane-hidden'

const HIDDEN_PANE = `[${PANE_HIDDEN_ATTR}]`

/** Spread onto a kept pane layer so the lookups below can skip it. */
export const hiddenPaneProps = (hidden: boolean): Record<string, string> => (hidden ? { [PANE_HIDDEN_ATTR]: '' } : {})

/** React face of the same policy: the pane layer provides its visibility so a
 *  kept-alive surface can gate hot subscriptions (streaming re-renders) off
 *  while it's an inactive tab. Default TRUE — surfaces outside a tab stack
 *  (secondary windows, plain routes) are always visible. */
export const PaneVisibleContext = createContext(true)

export const usePaneVisible = (): boolean => useContext(PaneVisibleContext)

/** Lifecycle face for expensive descendants. Outside a pane tree the surface is
 * visible; hot-hidden panes stay mounted but can lower their render budget. */
export const PaneLifecycleContext = createContext<PaneLifecycle>('visible')

export const usePaneLifecycle = (): PaneLifecycle => useContext(PaneLifecycleContext)

/** Fallback group key for a surface rendered outside the layout tree (secondary
 *  windows, plain routes) — one bucket, since there are no sibling zones there
 *  to tell apart. */
export const NO_PANE_GROUP = 'window'

/** The layout-tree GROUP (zone) a pane is rendered in — the identity of "this
 *  set of tabs". Panes stacked as tabs share one group; each split zone is its
 *  own. State that should be per-zone rather than per-window or per-tab keys off
 *  this (see the composer pop-out). Follows a pane dragged between zones,
 *  because the provider is the zone that renders it. */
export const PaneGroupContext = createContext(NO_PANE_GROUP)

export const usePaneGroup = (): string => useContext(PaneGroupContext)

/** Runtime disable for hidden-pane containment, no rebuild required — same
 *  idiom (and `'1'` value) as `hermes.desktop.inflightTurnJournal.disabled`.
 *  Set it and reload to get the pre-containment layer back verbatim. */
export const HIDDEN_PANE_CONTAINMENT_DISABLED_KEY = 'hermes.desktop.hiddenPaneContainment.disabled'

/** Permanent on the content wrapper — see the containment invariants above.
 *  The fallback only applies to a pane hidden before it ever rendered (no
 *  remembered size yet); once shown, the remembered size wins. */
export const PANE_CONTENT_INTRINSIC_SIZE_CLASS = '[contain-intrinsic-size:auto_60rem]'

/** Added to the content wrapper ONLY while the pane is an inactive tab. */
export const PANE_CONTENT_SKIPPED_CLASS = '[content-visibility:hidden]'

const containmentEnabled = (): boolean => {
  try {
    return typeof window === 'undefined' || window.localStorage.getItem(HIDDEN_PANE_CONTAINMENT_DISABLED_KEY) !== '1'
  } catch {
    // An unreadable kill switch leaves containment ON (the default build).
    return true
  }
}

/** Read the kill switch ONCE per mount: the class set must not change under a
 *  live pane (a mid-life flip would drop the remembered size), and re-reading
 *  localStorage on every zone render would put a sync storage hit on the
 *  tab-switch path this containment exists to make cheap. */
export const useHiddenPaneContainment = (): boolean => {
  const [enabled] = useState(containmentEnabled)

  return enabled
}

/** Whether an element belongs to an inactive keep-alive pane. */
export const isElementInHiddenPane = (element: Element): boolean => Boolean(element.closest(HIDDEN_PANE))

/** `querySelectorAll` minus anything inside an inactive tab. */
export const queryAllVisible = <T extends HTMLElement>(selector: string, root: ParentNode = document): T[] =>
  [...root.querySelectorAll<T>(selector)].filter(el => !isElementInHiddenPane(el))

/** `querySelector` minus anything inside an inactive tab. */
export const queryVisible = <T extends HTMLElement>(selector: string, root: ParentNode = document): null | T =>
  queryAllVisible<T>(selector, root)[0] ?? null
