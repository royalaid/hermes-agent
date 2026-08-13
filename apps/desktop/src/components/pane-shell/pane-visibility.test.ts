import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  HIDDEN_PANE_CONTAINMENT_DISABLED_KEY,
  hiddenPaneProps,
  isElementInHiddenPane,
  PANE_CONTENT_INTRINSIC_SIZE_CLASS,
  PANE_CONTENT_SKIPPED_CLASS,
  PANE_HIDDEN_ATTR,
  queryAllVisible,
  queryVisible,
  useHiddenPaneContainment
} from './pane-visibility'

/**
 * Inactive tabs stay mounted with their layout box intact, so they answer
 * document-wide lookups exactly like the visible tab. These helpers are the one
 * place that difference is decided.
 */

const COMPOSER = '[data-slot="composer-root"]'

const tab = (id: string, hidden = false) => `
  <div ${hidden ? PANE_HIDDEN_ATTR : ''}>
    <section><div data-slot="composer-root" id="${id}"></div></section>
  </div>
`

/** The same layer, with the containment wrapper the zone renderer puts between
 *  the marked layer and the pane's content. */
const containedTab = (id: string, hidden = false) => `
  <div ${hidden ? PANE_HIDDEN_ATTR : ''}>
    <div class="${PANE_CONTENT_INTRINSIC_SIZE_CLASS}${hidden ? ` ${PANE_CONTENT_SKIPPED_CLASS}` : ''}" data-pane-content>
      <section><div data-slot="composer-root" id="${id}"></div></section>
    </div>
  </div>
`

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('pane visibility lookups', () => {
  it('resolves the foreground element even when a hidden tab matches first', () => {
    document.body.innerHTML = tab('background', true) + tab('foreground')

    expect(queryVisible(COMPOSER)?.id).toBe('foreground')
    expect(queryAllVisible(COMPOSER).map(el => el.id)).toEqual(['foreground'])
  })

  it('answers normally when nothing is hidden', () => {
    document.body.innerHTML = tab('only')

    expect(queryVisible(COMPOSER)?.id).toBe('only')
  })

  it('marks a pane hidden only while it is inactive', () => {
    expect(hiddenPaneProps(true)).toEqual({ [PANE_HIDDEN_ATTR]: '' })
    expect(hiddenPaneProps(false)).toEqual({})
  })

  it('answers the same with the containment wrapper in the walk', () => {
    document.body.innerHTML = containedTab('background', true) + containedTab('foreground')

    expect(queryVisible(COMPOSER)?.id).toBe('foreground')
    expect(queryAllVisible(COMPOSER).map(el => el.id)).toEqual(['foreground'])
    // The wrapper is an extra ancestor between the marker and the content; it
    // must neither hide a visible pane nor expose a hidden one.
    expect(isElementInHiddenPane(document.querySelector('#background')!)).toBe(true)
    expect(isElementInHiddenPane(document.querySelector('#foreground')!)).toBe(false)
    expect(isElementInHiddenPane(document.querySelectorAll('[data-pane-content]')[0]!)).toBe(true)
    expect(isElementInHiddenPane(document.querySelectorAll('[data-pane-content]')[1]!)).toBe(false)
  })

  it('mixes contained and plain layers without changing the answer', () => {
    document.body.innerHTML = containedTab('background', true) + tab('foreground')

    expect(queryVisible(COMPOSER)?.id).toBe('foreground')
  })
})

describe('hidden-pane containment kill switch', () => {
  it('defaults to containment ON', () => {
    expect(renderHook(() => useHiddenPaneContainment()).result.current).toBe(true)
  })

  it('is off only for the exact opt-out value', () => {
    window.localStorage.setItem(HIDDEN_PANE_CONTAINMENT_DISABLED_KEY, '1')
    expect(renderHook(() => useHiddenPaneContainment()).result.current).toBe(false)

    window.localStorage.setItem(HIDDEN_PANE_CONTAINMENT_DISABLED_KEY, 'true')
    expect(renderHook(() => useHiddenPaneContainment()).result.current).toBe(true)
  })

  it('reads once per mount, so a live pane never changes class set mid-life', () => {
    const { rerender, result } = renderHook(() => useHiddenPaneContainment())

    expect(result.current).toBe(true)

    window.localStorage.setItem(HIDDEN_PANE_CONTAINMENT_DISABLED_KEY, '1')
    rerender()

    // Flipping the switch under a mounted pane would drop the size CSS
    // remembered while it was visible — the next mount picks it up instead.
    expect(result.current).toBe(true)
    expect(renderHook(() => useHiddenPaneContainment()).result.current).toBe(false)
  })
})
