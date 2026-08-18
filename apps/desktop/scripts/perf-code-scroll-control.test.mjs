import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('independent code-scroll performance control', () => {
  it('owns horizontal code input separately from subagent fanout', () => {
    const fanout = readFileSync('scripts/perf/scenarios/subagent-fanout.mjs', 'utf8')
    const control = readFileSync('scripts/perf/scenarios/code-scroll-control.mjs', 'utf8')

    expect(fanout).not.toContain("measureInputPaint(cdp, 'code-scroll'")
    expect(fanout).not.toContain('code_card_scroll_to_paint_ms')
    expect(fanout).not.toContain('interactions.codeScroll')

    expect(control).toContain("name: 'code-scroll-control'")
    expect(control).toContain("tier: 'report'")
    expect(control).toContain("window.__PERF_DRIVE__.loadTranscript")
    expect(control).toContain("[data-slot=\"code-card\"]")
    expect(control).toContain("pre.style.minWidth = '4096px'")
    expect(control).toContain("Input.dispatchMouseEvent")
    expect(control).toContain("type: 'mouseWheel'")
    expect(control).toContain('hostMs')
    expect(control).toContain('rendererWaitMs')
    expect(control).toContain('paintMs')
    expect(control).toContain('rendererTotalMs')
    expect(control).toContain('requiredInteractionMs')
    expect(control).toContain('__CODE_SCROLL_DIAG__')
    expect(control).toContain('elementFromPoint')
    expect(control).toContain('wheelEvents')
    expect(control).toContain('scrollEvents')
    expect(control).toContain('scrollLeftBefore')
    expect(control).toContain('scrollLeftAfter')
    expect(control).toContain('maxScrollLeft')
    expect(control).toContain('viewportIntersection')
    expect(control).toContain("scroller.closest('[data-slot=\"code-card\"]')?.scrollIntoView")
    expect(control).toContain('hitInsideScroller || viewportIntersection')
  })
})
