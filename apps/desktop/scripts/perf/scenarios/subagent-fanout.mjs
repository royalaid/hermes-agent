// Isolated report-only reproduction. This is the one perf scenario allowed to
// use CDP Input: every control below is a real renderer interaction, while the
// transcript and subagent updates still arrive through the production reducer.
import { SELECTORS, sleep } from '../lib/cdp.mjs'
import { percentile } from '../lib/stats.mjs'

export const firstVisibleElement = elements =>
  [...elements].find(element => {
    const box = element.getBoundingClientRect()
    const visible = element.checkVisibility?.({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })

    return box.width > 0 && box.height > 0 && visible !== false
  }) ?? null

const INSTALL_RECORDER = `(() => {
  const frames = []
  const longtasks = []
  let last = performance.now()
  let stopped = false
  const tick = now => {
    if (stopped) return
    frames.push({ at: now, duration: now - last })
    last = now
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  let observer
  try {
    observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) longtasks.push({ duration: entry.duration, startTime: entry.startTime })
    })
    observer.observe({ entryTypes: ['longtask'] })
  } catch {}
  window.__FANOUT_METRICS__ = {
    mark: () => performance.now(),
    slice: since => ({
      frames: frames.filter(frame => frame.at >= since).map(frame => frame.duration),
      longtasks: longtasks.filter(entry => entry.startTime >= since).map(entry => entry.duration)
    }),
    stop: () => { stopped = true; observer?.disconnect() }
  }
  return true
})()`

const PREPARE_PAINT = `((kind) => {
  const firstVisible = ${firstVisibleElement.toString()}
  const fanoutRoot = document.querySelector('[data-session-anchor="session-tile:perf-fanout-visible"]')
  const wait = (target, eventName) => {
    if (!target) return null
    const started = performance.now()
    window.__FANOUT_PAINT__ = new Promise(resolve => {
      let settled = false
      const finish = value => {
        if (!settled) { settled = true; resolve(value) }
      }
      target.addEventListener(eventName, () => {
        const deliveredAt = performance.now()
        requestAnimationFrame(() => {
          const paintedAt = performance.now()
          finish({
            kind,
            paintMs: paintedAt - deliveredAt,
            rendererTotalMs: paintedAt - started,
            rendererWaitMs: deliveredAt - started
          })
        })
      }, { once: true })
      setTimeout(() => finish(null), 1500)
    })
    const box = target.getBoundingClientRect()
    return { x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2) }
  }

  if (kind === 'transcript-scroll') {
    const viewport = firstVisible([...(fanoutRoot?.querySelectorAll(${JSON.stringify(SELECTORS.threadViewport)}) ?? [])].filter(
      node => node.scrollHeight > node.clientHeight
    ))
    return wait(viewport, 'scroll')
  }
  if (kind === 'composer-key') {
    const composer = firstVisible(fanoutRoot?.querySelectorAll(${JSON.stringify(SELECTORS.composer)}) ?? [])
    composer?.focus()
    return wait(composer, 'input')
  }
  if (kind === 'pane-switch') {
    return wait(firstVisible(document.querySelectorAll('[data-tree-tab="session-tile:perf-fanout-control"]')), 'click')
  }
  return null
})`

const stats = values => ({ p50: percentile(values, 0.5), p95: percentile(values, 0.95), p99: percentile(values, 0.99) })
const round = value => Math.round(value * 10) / 10
const numberOrZero = value => (typeof value === 'number' && Number.isFinite(value) ? round(value) : 0)
export const requiredInteractionMs = (receipt, label) => {
  const value = receipt?.hostMs

  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`subagent-fanout missing required ${label} host dispatch-to-paint metric`)
  }

  return round(value)
}
const boundedInteger = (value, fallback, min, max) => {
  const number = Number(value)
  const integer = Number.isFinite(number) ? Math.trunc(number) : fallback

  return Math.min(max, Math.max(min, integer))
}

async function waitFor(cdp, expression, label, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    if (await cdp.eval(expression)) {
      return
    }

    await sleep(50)
  }

  throw new Error(`subagent-fanout timed out waiting for ${label}`)
}

async function measureInputPaint(cdp, kind, input) {
  const point = await cdp.eval(`${PREPARE_PAINT}(${JSON.stringify(kind)})`)

  if (!point) {
    return null
  }

  const hostStartedAt = performance.now()
  await cdp.send('Input.dispatchMouseEvent', input(point))
  const receipt = await cdp.eval('window.__FANOUT_PAINT__')

  return receipt ? { ...receipt, hostMs: performance.now() - hostStartedAt } : null
}

async function runInteractions(cdp) {
  const transcriptScroll = await measureInputPaint(cdp, 'transcript-scroll', point => ({
    deltaX: 0,
    deltaY: -180,
    type: 'mouseWheel',
    x: point.x,
    y: point.y
  }))
  const composerPoint = await cdp.eval(`${PREPARE_PAINT}('composer-key')`)
  let composerKey = null

  if (composerPoint) {
    const hostStartedAt = performance.now()
    await cdp.send('Input.dispatchKeyEvent', { text: 'p', type: 'char', unmodifiedText: 'p' })
    const receipt = await cdp.eval('window.__FANOUT_PAINT__')
    composerKey = receipt ? { ...receipt, hostMs: performance.now() - hostStartedAt } : null
  }

  // Return the intentionally empty perf composer to its starting state through
  // CDP Input as well; never set text or dispatch synthetic DOM events.
  await cdp.send('Input.dispatchKeyEvent', { code: 'Backspace', key: 'Backspace', type: 'keyDown', windowsVirtualKeyCode: 8 })
  await cdp.send('Input.dispatchKeyEvent', { code: 'Backspace', key: 'Backspace', type: 'keyUp', windowsVirtualKeyCode: 8 })

  const panePoint = await cdp.eval(`${PREPARE_PAINT}('pane-switch')`)
  let paneSwitch = null
  if (panePoint) {
    const hostStartedAt = performance.now()
    await cdp.send('Input.dispatchMouseEvent', {
      button: 'left',
      clickCount: 1,
      type: 'mousePressed',
      x: panePoint.x,
      y: panePoint.y
    })
    await cdp.send('Input.dispatchMouseEvent', {
      button: 'left',
      clickCount: 1,
      type: 'mouseReleased',
      x: panePoint.x,
      y: panePoint.y
    })
    const receipt = await cdp.eval('window.__FANOUT_PAINT__')
    paneSwitch = receipt ? { ...receipt, hostMs: performance.now() - hostStartedAt } : null
  }

  return { composerKey, paneSwitch, transcriptScroll }
}

async function prepareScrollableTranscriptControl(cdp) {
  return cdp.eval(`(() => {
    const root = document.querySelector('[data-session-anchor="session-tile:perf-fanout-visible"]')
    const viewport = root?.querySelector('[data-slot="aui_thread-viewport"]')
    const content = viewport?.querySelector('[data-slot="aui_thread-content"]')
    if (!viewport || !content) return null

    // Fixture-only geometry on the real thread viewport. The bounded first-paint
    // window can contain only two lightweight rows, so guarantee one wheelable
    // viewport without changing transcript-window production policy.
    content.style.minHeight = (viewport.clientHeight + 1024) + 'px'
    viewport.scrollTop = viewport.scrollHeight
    return {
      clientHeight: viewport.clientHeight,
      scrollHeight: viewport.scrollHeight,
      scrollTop: viewport.scrollTop
    }
  })()`)
}

async function expandActiveStatusSection(cdp) {
  const rowSelector =
    '[data-session-anchor="session-tile:perf-fanout-visible"] [data-slot="composer-status-stack"] [class~="group/status-row"]'
  const before = await cdp.eval(`(() => {
    const root = document.querySelector('[data-session-anchor="session-tile:perf-fanout-visible"]')
    const stack = root?.querySelector('[data-slot="composer-status-stack"]')
    const icon = stack?.querySelector('.codicon-agent')
    const button = icon?.closest('button')
    const box = button?.getBoundingClientRect()
    const hit = box ? document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) : null
    return {
      buttonText: button?.textContent?.trim() ?? null,
      hitTag: hit?.tagName ?? null,
      hitText: hit?.textContent?.trim() ?? null,
      rows: document.querySelectorAll(${JSON.stringify(rowSelector)}).length,
      target: box ? { height: box.height, width: box.width, x: box.left + box.width / 2, y: box.top + box.height / 2 } : null
    }
  })()`)

  if (before.rows > 0) return { after: before.rows, before, clicked: false }
  if (!before.target || before.target.width <= 0 || before.target.height <= 0) {
    return { after: 0, before, clicked: false }
  }

  await cdp.send('Input.dispatchMouseEvent', {
    button: 'left', buttons: 1, clickCount: 1, type: 'mousePressed', x: Math.round(before.target.x), y: Math.round(before.target.y)
  })
  await cdp.send('Input.dispatchMouseEvent', {
    button: 'left', buttons: 0, clickCount: 1, type: 'mouseReleased', x: Math.round(before.target.x), y: Math.round(before.target.y)
  })
  await sleep(100)

  return {
    after: await cdp.eval(`document.querySelectorAll(${JSON.stringify(rowSelector)}).length`),
    before,
    clicked: true
  }
}

export default {
  name: 'subagent-fanout',
  tier: 'report',
  description: 'Production-reducer fanout with transcript, composer, and pane input-to-paint controls.',
  async run(cdp, opts = {}) {
    const input = {
      intervalMs: boundedInteger(opts['interval-ms'], 33, 1, 1000),
      seed: boundedInteger(opts.seed, 1, 0, 0x7fffffff),
      turns: boundedInteger(opts.turns, 20, 1, 400),
      updates: boundedInteger(opts.updates, 12, 1, 240),
      workers: boundedInteger(opts.workers, 8, 1, 8)
    }

    await cdp.send('Runtime.enable')
    const ready = await cdp.eval(
      '!!window.__PERF_DRIVE__?.subagentFanout && !!window.__PERF_DRIVE__?.ensureFanoutRuntime'
    )

    if (!ready) {
      throw new Error('subagent-fanout needs a dev renderer or --prod build with VITE_PERF_PROBE=1.')
    }

    try {
      const runtimeId = await cdp.eval('window.__PERF_DRIVE__.ensureFanoutRuntime()')
      // Let route/session effects settle before the probe seeds the transcript;
      // otherwise a fresh runtime's initial empty state can win the same frame.
      await sleep(1500)
      await cdp.eval(INSTALL_RECORDER)
      const burstMark = await cdp.eval('window.__FANOUT_METRICS__.mark()')
      await cdp.eval(
        `window.__PERF_DRIVE__.subagentFanout(${JSON.stringify({ ...input, holdTerminal: true, runtimeId: '__RUNTIME_ID__' }).replace(
          '"__RUNTIME_ID__"',
          JSON.stringify(runtimeId)
        )})`
      )
      await waitFor(cdp, '!!window.__HERMES_SESSION_TILES__ && !!window.__HERMES_LAYOUT_TREE__', 'tile hooks')
      await waitFor(
        cdp,
        `!!document.querySelector('[data-tree-tab="session-tile:perf-fanout-visible"]') && !!document.querySelector('[data-tree-tab="session-tile:perf-fanout-control"]')`,
        'fanout session tiles'
      )
      await cdp.eval("window.__HERMES_LAYOUT_TREE__.reveal('session-tile:perf-fanout-visible')")
      await waitFor(cdp, 'window.__PERF_DRIVE__.stateSnapshot().subagentRows > 0', 'first reducer update')
      await waitFor(
        cdp,
        `document.querySelectorAll('[data-session-anchor="session-tile:perf-fanout-visible"] [data-slot="composer-status-stack"]').length > 0`,
        'rendered composer status stack'
      )
      const statusGroupsObserved = await cdp.eval(
        `document.querySelectorAll('[data-session-anchor="session-tile:perf-fanout-visible"] [data-slot="composer-status-stack"] .codicon-agent').length`
      )
      await waitFor(cdp, `document.querySelectorAll(${JSON.stringify(SELECTORS.assistantMessage)}).length > 0`, 'rendered transcript')
      await waitFor(cdp, `document.querySelectorAll('[data-delegate-card]').length > 0`, 'rendered delegate card')
      await waitFor(cdp, `document.querySelectorAll('[data-slot="code-card"]').length > 0`, 'rendered code card', 15000)
      await waitFor(
        cdp,
        `document.querySelectorAll('[data-session-anchor="session-tile:perf-fanout-visible"] [data-slot="code-card"] pre').length > 0`,
        'rendered code pre',
        15000
      )
      await sleep(Math.max(300, input.workers * input.intervalMs))
      const burst = await cdp.eval(`window.__FANOUT_METRICS__.slice(${burstMark})`)

      const dom = await cdp.eval(`({
        assistantMessages: document.querySelectorAll(${JSON.stringify(SELECTORS.assistantMessage)}).length,
        codeCards: document.querySelectorAll('[data-slot="code-card"]').length,
        delegateCards: document.querySelectorAll('[data-delegate-card]').length,
        delegateRows: document.querySelectorAll('[data-delegate-card] [data-conversation-scaffold]').length,
        statusRows: document.querySelectorAll('[data-slot="composer-status-stack"] [data-conversation-scaffold]').length
      })`)
      const steadyMark = await cdp.eval('window.__FANOUT_METRICS__.mark()')
      // The independent code-scroll-control scenario owns horizontal code
      // input. Fanout prepares only the real transcript viewport here.
      const preparedTranscript = await prepareScrollableTranscriptControl(cdp)
      if (!preparedTranscript || preparedTranscript.scrollHeight <= preparedTranscript.clientHeight) {
        throw new Error(`subagent-fanout could not prepare transcript scroll control: ${JSON.stringify(preparedTranscript)}`)
      }
      const controls = await cdp.eval(`(() => {
        const root = document.querySelector('[data-session-anchor="session-tile:perf-fanout-visible"]')
        const viewport = root?.querySelector(${JSON.stringify(SELECTORS.threadViewport)})
        const controlTab = document.querySelector('[data-tree-tab="session-tile:perf-fanout-control"]')
        const controlTabBox = controlTab?.getBoundingClientRect()
        return {
          controlTab: controlTabBox ? {
            height: controlTabBox.height,
            selected: controlTab?.getAttribute('aria-selected'),
            width: controlTabBox.width,
            x: controlTabBox.x,
            y: controlTabBox.y
          } : null,
          composers: root?.querySelectorAll(${JSON.stringify(SELECTORS.composer)}).length ?? 0,
          controlTabs: document.querySelectorAll('[data-tree-tab="session-tile:perf-fanout-control"]').length,
          root: Boolean(root),
          viewport: viewport ? {
            clientHeight: viewport.clientHeight,
            scrollHeight: viewport.scrollHeight,
            scrollTop: viewport.scrollTop
          } : null
        }
      })()`)
      const interactionPhaseStart = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')
      if (!interactionPhaseStart.fanoutActive) {
        throw new Error(
          `subagent-fanout interactions began after active fanout ended: ${JSON.stringify({ controls, dom, input, interactionPhaseStart, statusGroupsObserved })}`
        )
      }
      const interactions = await runInteractions(cdp)
      const interactionPhaseEnd = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')
      if (interactionPhaseEnd.gatewayDispatches <= interactionPhaseStart.gatewayDispatches) {
        throw new Error(
          `subagent-fanout received no non-terminal gateway updates during interactions: ${JSON.stringify({ input, interactionPhaseStart, interactionPhaseEnd, interactions })}`
        )
      }
      const terminalReleased = await cdp.eval('window.__PERF_DRIVE__.releaseFanoutTerminal()')
      if (!terminalReleased) {
        throw new Error('subagent-fanout could not release the held terminal batch')
      }
      await sleep(Math.max(500, input.workers * input.intervalMs))
      const steady = await cdp.eval(`window.__FANOUT_METRICS__.slice(${steadyMark})`)

      await waitFor(cdp, '!window.__PERF_DRIVE__.stateSnapshot().fanoutActive', 'fanout completion', 30000)
      await waitFor(cdp, `window.__PERF_DRIVE__.stateSnapshot().subagentRows === ${input.workers}`, 'reduced worker rows')
      const recoveryMark = await cdp.eval('window.__FANOUT_METRICS__.mark()')
      await sleep(2000)
      const recovery = await cdp.eval(`window.__FANOUT_METRICS__.slice(${recoveryMark})`)
      await cdp.eval('window.__FANOUT_METRICS__.stop()')

      const state = await cdp.eval('window.__PERF_DRIVE__.stateSnapshot()')

      if (state.gatewayDispatchFailures || state.gatewayDispatches === 0 || state.subagentRows !== input.workers) {
        throw new Error('subagent-fanout did not reach the production gateway reducer contract')
      }

      dom.statusRows = statusGroupsObserved

      const missingDomProof =
        dom.assistantMessages === 0 ||
        dom.codeCards === 0 ||
        dom.delegateCards === 0 ||
        dom.delegateRows === 0 ||
        statusGroupsObserved === 0 ||
        interactions.composerKey === null ||
        interactions.paneSwitch === null ||
        interactions.transcriptScroll === null

      if (missingDomProof) {
        throw new Error(
          `subagent-fanout missing required DOM/interaction proof: ${JSON.stringify({ controls, dom, interactions, state, statusGroupsObserved })}`
        )
      }

      return {
        metrics: {
          burst_frame_p95_ms: numberOrZero(stats(burst.frames).p95),
          burst_longtask_count: burst.longtasks.length,
          composer_key_to_paint_ms: requiredInteractionMs(interactions.composerKey, 'composer'),
          pane_switch_to_paint_ms: requiredInteractionMs(interactions.paneSwitch, 'pane switch'),
          recovery_frame_p95_ms: numberOrZero(stats(recovery.frames).p95),
          recovery_longtask_count: recovery.longtasks.length,
          steady_frame_p95_ms: numberOrZero(stats(steady.frames).p95),
          steady_longtask_count: steady.longtasks.length,
          transcript_scroll_to_paint_ms: requiredInteractionMs(interactions.transcriptScroll, 'transcript scroll')
        },
        detail: {
          burst,
          controls,
          dom,
          input,
          interactionPhaseEnd,
          interactionPhaseStart,
          interactions,
          recovery,
          runtimeId,
          state,
          statusGroupsObserved,
          steady
        }
      }
    } finally {
      await cdp.eval(`(() => {
        window.__FANOUT_METRICS__?.stop()
        window.__PERF_DRIVE__?.reset()
        return 'cleaned'
      })()`)
    }
  }
}
