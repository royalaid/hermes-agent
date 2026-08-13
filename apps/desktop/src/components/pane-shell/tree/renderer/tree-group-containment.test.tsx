import { useStore } from '@nanostores/react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import {
  HIDDEN_PANE_CONTAINMENT_DISABLED_KEY,
  isElementInHiddenPane,
  PANE_CONTENT_INTRINSIC_SIZE_CLASS,
  PANE_CONTENT_SKIPPED_CLASS,
  PANE_HIDDEN_ATTR,
  queryAllVisible,
  queryVisible
} from '../../pane-visibility'
import { group, type GroupNode } from '../model'
import { $layoutTree, activateTreePane } from '../store'

import { TreeGroup } from './tree-group'

/**
 * Hidden keep-alive tabs must stop paying style+layout for their transcript
 * WITHOUT losing the scroll box that keeps their scroll position — so the
 * containment lives on an inner wrapper, never on the scroller itself.
 *
 * jsdom has no layout: it can prove the structural contract (which element
 * carries which class, and that neither the scroller nor the mounted pane is
 * replaced across a tab round-trip) but it cannot prove that `scrollTop`
 * actually survives, because `scrollHeight` is always 0 here. Real-layout
 * scroll retention is an electron/e2e check.
 */

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  // jsdom lacks CSS.escape, which tab-strip-scroll uses in a layout effect.
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

const ZONE = 'grp-tabs'
const disposers: (() => void)[] = []

/** A pane body that answers the document-wide lookups exactly like the real
 *  chat surface does (composer slot), so R3 is exercised on real markup. */
const paneBody = (id: string) => (
  <div data-testid={`body-${id}`}>
    <div data-slot="composer-root" id={`composer-${id}`} />
  </div>
)

beforeEach(async () => {
  window.localStorage.clear()

  const { $dismissedPanes, $hiddenTreePanes } = await import('../store')
  $dismissedPanes.set(new Set())
  $hiddenTreePanes.set(new Set())

  for (const id of ['alpha', 'beta']) {
    disposers.push(registry.register({ area: 'panes', data: {}, id, render: () => paneBody(id), title: id }))
  }

  $layoutTree.set(group(['alpha', 'beta'], { active: 'alpha', id: ZONE }))
})

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

/** Renders the REAL zone renderer against the live tree, so activating a tab
 *  goes through the production store path. */
function Zone() {
  const tree = useStore($layoutTree)

  return tree ? <TreeGroup node={tree as GroupNode} /> : null
}

const bodyOf = (id: string) => document.querySelector<HTMLElement>(`[data-testid="body-${id}"]`)

/** Class names here are Tailwind arbitrary properties (brackets and colons),
 *  which are not selector-safe — scan classLists instead. */
const elementsWithClass = (className: string) =>
  [...document.querySelectorAll<HTMLElement>('*')].filter(el => el.classList.contains(className))

const wrapperOf = (id: string) => bodyOf(id)?.closest<HTMLElement>('[data-pane-content]') ?? null

/** The keep-alive LAYER: the absolutely positioned scroll box per pane. */
const layerOf = (id: string) =>
  (wrapperOf(id) ?? bodyOf(id))?.closest<HTMLElement>('.overflow-auto') ?? bodyOf(id)?.parentElement ?? null

const showTab = (paneId: string) => act(() => activateTreePane(ZONE, paneId))

/** Both tabs mounted (keep-alive only keeps EVER-ACTIVE panes), back on alpha. */
function renderBothTabs() {
  const view = render(<Zone />)

  showTab('beta')
  showTab('alpha')

  return view
}

describe('hidden-pane containment', () => {
  it('skips only the inactive tab, and keeps the intrinsic size on both', () => {
    renderBothTabs()

    // The remembered-size property is PERMANENT: CSS only records a size while
    // the element renders unskipped, so a wrapper that gained it at hide time
    // would have nothing to remember and would collapse to the fallback.
    for (const id of ['alpha', 'beta']) {
      expect(wrapperOf(id)?.classList.contains(PANE_CONTENT_INTRINSIC_SIZE_CLASS)).toBe(true)
    }

    expect(wrapperOf('alpha')?.classList.contains(PANE_CONTENT_SKIPPED_CLASS)).toBe(false)
    expect(wrapperOf('beta')?.classList.contains(PANE_CONTENT_SKIPPED_CLASS)).toBe(true)

    showTab('beta')

    for (const id of ['alpha', 'beta']) {
      expect(wrapperOf(id)?.classList.contains(PANE_CONTENT_INTRINSIC_SIZE_CLASS)).toBe(true)
    }

    expect(wrapperOf('alpha')?.classList.contains(PANE_CONTENT_SKIPPED_CLASS)).toBe(true)
    expect(wrapperOf('beta')?.classList.contains(PANE_CONTENT_SKIPPED_CLASS)).toBe(false)
  })

  it('never gives the wrapper an extrinsic height', () => {
    renderBothTabs()

    // An extrinsic height overrides the remembered-size placeholder — the
    // hidden pane would then collapse and clamp its scroll position.
    for (const id of ['alpha', 'beta']) {
      expect(wrapperOf(id)?.classList.contains('h-full')).toBe(false)
      expect(wrapperOf(id)?.style.height).toBe('')
    }
  })

  it('contains the CONTENT, never the scroll box', () => {
    renderBothTabs()

    const layer = layerOf('beta')!

    expect(layer.classList.contains(PANE_CONTENT_SKIPPED_CLASS)).toBe(false)
    expect(layer.classList.contains(PANE_CONTENT_INTRINSIC_SIZE_CLASS)).toBe(false)
    expect(layer.contains(wrapperOf('beta'))).toBe(true)
    // Still the scroller, still keeping its box (visibility, not display).
    expect(layer.classList.contains('overflow-auto')).toBe(true)
    expect(layer.classList.contains('invisible')).toBe(true)
  })

  it('keeps the data-pane-hidden contract on the layer', () => {
    renderBothTabs()

    expect(layerOf('beta')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(true)
    expect(layerOf('alpha')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(false)
    expect(layerOf('beta')?.getAttribute('aria-hidden')).toBe('true')

    // The marker stays on the LAYER — a wrapper carrying it too would be
    // harmless, but a wrapper carrying it INSTEAD would break every lookup
    // that walks up from a pane's content.
    expect(wrapperOf('beta')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(false)

    showTab('beta')

    expect(layerOf('alpha')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(true)
    expect(layerOf('beta')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(false)
  })

  it('answers document-wide lookups from the visible tab only', () => {
    renderBothTabs()

    expect(queryVisible('[data-slot="composer-root"]')?.id).toBe('composer-alpha')
    expect(queryAllVisible('[data-slot="composer-root"]').map(el => el.id)).toEqual(['composer-alpha'])
    expect(isElementInHiddenPane(bodyOf('beta')!)).toBe(true)
    expect(isElementInHiddenPane(bodyOf('alpha')!)).toBe(false)
    // The wrapper is a new ancestor in every lookup's walk — it must not hide
    // or expose anything on its own.
    expect(isElementInHiddenPane(wrapperOf('beta')!)).toBe(true)
    expect(isElementInHiddenPane(wrapperOf('alpha')!)).toBe(false)

    showTab('beta')

    expect(queryVisible('[data-slot="composer-root"]')?.id).toBe('composer-beta')
    expect(queryAllVisible('[data-slot="composer-root"]').map(el => el.id)).toEqual(['composer-beta'])
  })

  it('keeps the scroll box and the mounted pane across a hide/reveal round-trip', () => {
    renderBothTabs()

    const layer = layerOf('alpha')
    const wrapper = wrapperOf('alpha')
    const body = bodyOf('alpha')

    showTab('beta')
    showTab('alpha')

    // Same nodes, so the scroller's scrollTop was never reset by a remount and
    // the pane never re-ran its mount effects. (jsdom cannot prove the scroll
    // VALUE survives — with no layout, scrollHeight is 0 and scrollTop clamps
    // to 0; that check belongs to the electron/e2e project.)
    expect(layerOf('alpha')).toBe(layer)
    expect(wrapperOf('alpha')).toBe(wrapper)
    expect(bodyOf('alpha')).toBe(body)
  })
})

describe('containment kill switch', () => {
  beforeEach(() => {
    window.localStorage.setItem(HIDDEN_PANE_CONTAINMENT_DISABLED_KEY, '1')
  })

  it('renders the pre-containment layer with keep-alive intact', () => {
    renderBothTabs()

    expect(document.querySelector('[data-pane-content]')).toBeNull()
    expect(elementsWithClass(PANE_CONTENT_SKIPPED_CLASS)).toEqual([])
    expect(elementsWithClass(PANE_CONTENT_INTRINSIC_SIZE_CLASS)).toEqual([])

    // Keep-alive semantics are untouched: both tabs stay mounted and the
    // hidden one still answers the marker contract.
    expect(bodyOf('alpha')).toBeTruthy()
    expect(bodyOf('beta')).toBeTruthy()
    expect(layerOf('beta')?.hasAttribute(PANE_HIDDEN_ATTR)).toBe(true)
    expect(queryVisible('[data-slot="composer-root"]')?.id).toBe('composer-alpha')
  })
})
