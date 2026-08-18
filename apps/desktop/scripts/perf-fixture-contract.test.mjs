import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('subagent fanout isolated fixture contract', () => {
  it('keeps provider onboarding outside explicit perf-probe builds', () => {
    const source = readFileSync('src/app/contrib/wiring.tsx', 'utf8')

    expect(source).toContain("!isAuxiliaryWindow() && import.meta.env.VITE_PERF_PROBE !== '1' && (")
    expect(source).toContain('<DesktopOnboardingOverlay')
  })

  it('keeps transcript geometry and strict non-code interactions in fanout', () => {
    const probe = readFileSync('src/app/chat/perf-probe.tsx', 'utf8')
    const scenario = readFileSync('scripts/perf/scenarios/subagent-fanout.mjs', 'utf8')

    expect(probe).toContain('PERF_WIDE_CODE_MARKER')
    expect(scenario).toContain("[data-slot=\"code-card\"]")
    expect(scenario).not.toContain('code_card_scroll_to_paint_ms')
    expect(scenario).not.toContain('interactions.codeScroll')
    expect(scenario).toContain('prepareScrollableTranscriptControl')
    expect(scenario).toContain("content.style.minHeight = (viewport.clientHeight + 1024) + 'px'")
    expect(scenario).toContain('buttons: 1')
    expect(scenario).toContain('buttons: 0')
    expect(scenario).toContain('statusGroupsObserved')
    expect(scenario).toContain('.codicon-agent')
    expect(scenario).toContain("holdTerminal: true")
    expect(scenario).toContain("const interactionPhaseStart = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')")
    expect(scenario).toContain('!interactionPhaseStart.fanoutActive')
    expect(scenario).toContain("const interactionPhaseEnd = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')")
    expect(scenario).toContain('interactionPhaseEnd.gatewayDispatches <= interactionPhaseStart.gatewayDispatches')
    expect(scenario).toContain('interactionPhaseStart,')
    expect(scenario).toContain('interactionPhaseEnd,')
    expect(scenario).toContain("window.__PERF_DRIVE__.releaseFanoutTerminal()")
    expect(scenario.indexOf('const interactions = await runInteractions(cdp)')).toBeLessThan(
      scenario.indexOf("const interactionPhaseEnd = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')")
    )
    expect(scenario.indexOf("const interactionPhaseEnd = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')")).toBeLessThan(
      scenario.indexOf("window.__PERF_DRIVE__.releaseFanoutTerminal()")
    )
    expect(scenario).toContain('const hostStartedAt = performance.now()')
    expect(scenario).toContain('hostMs: performance.now() - hostStartedAt')
    expect(scenario).toContain('rendererWaitMs')
    expect(scenario).toContain('paintMs')
    expect(scenario).not.toContain('numberOrZero(interactions.codeScroll?.ms)')
    expect(scenario).not.toContain('numberOrZero(interactions.composerKey?.ms)')
    expect(scenario).not.toContain('numberOrZero(interactions.paneSwitch?.ms)')
    expect(scenario).not.toContain('numberOrZero(interactions.transcriptScroll?.ms)')
  })
})
