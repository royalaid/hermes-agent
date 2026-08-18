// Independent horizontal code-card control. This stays separate from
// subagent-fanout because both controls otherwise move the same thread viewport
// and invalidate each other's target geometry.
import { sleep } from '../lib/cdp.mjs'

const round = value => Math.round(value * 10) / 10

const boundedInteger = (value, fallback, min, max) => {
  const number = Number(value)
  const integer = Number.isFinite(number) ? Math.trunc(number) : fallback
  return Math.min(max, Math.max(min, integer))
}

export const requiredInteractionMs = (receipt, label) => {
  const value = receipt?.hostMs
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`code-scroll-control missing required ${label} host dispatch-to-paint metric`)
  }
  return round(value)
}

async function waitFor(cdp, expression, label, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await cdp.eval(expression)) return
    await sleep(50)
  }
  throw new Error(`code-scroll-control timed out waiting for ${label}`)
}

async function prepareCodeScroller(cdp) {
  return cdp.eval(`(() => {
    const card = [...document.querySelectorAll('[data-slot="code-card"]')].find(node => {
      const box = node.getBoundingClientRect()
      return box.width > 0 && box.height > 0 && node.checkVisibility?.({ contentVisibilityAuto: true }) !== false
    })
    const scroller = card ? [...card.querySelectorAll('*')].find(node => {
      const overflowX = getComputedStyle(node).overflowX
      return overflowX === 'auto' || overflowX === 'scroll'
    }) : null
    const pre = scroller?.querySelector('pre') ?? card?.querySelector('pre')
    if (!card || !scroller || !pre) return null

    pre.style.minWidth = '4096px'
    scroller.scrollLeft = 0
    scroller.dataset.perfCodeScrollControl = 'true'
    card.scrollIntoView({ block: 'center', inline: 'nearest' })
    const box = scroller.getBoundingClientRect()
    return {
      cardCount: document.querySelectorAll('[data-slot="code-card"]').length,
      clientWidth: scroller.clientWidth,
      scrollWidth: scroller.scrollWidth,
      x: Math.round(box.left + box.width / 2),
      y: Math.round(box.top + box.height / 2)
    }
  })()`)
}

async function measureCodeScroll(cdp) {
  const prepared = await prepareCodeScroller(cdp)
  if (!prepared || prepared.cardCount === 0 || prepared.scrollWidth <= prepared.clientWidth) {
    throw new Error(`code-scroll-control missing DOM/overflow proof: ${JSON.stringify(prepared)}`)
  }

  await cdp.eval('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
  const armed = await cdp.eval(`(async () => {
    const scroller = document.querySelector('[data-perf-code-scroll-control="true"]')
    if (!scroller) return null

    scroller.closest('[data-slot="code-card"]')?.scrollIntoView({ block: 'center', inline: 'nearest' })
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))

    const box = scroller.getBoundingClientRect()
    const x = Math.round(box.left + box.width / 2)
    const y = Math.round(box.top + box.height / 2)
    const hit = document.elementFromPoint(x, y)
    const hitInsideScroller = Boolean(hit && scroller.contains(hit))
    const intersectionRect = {
      bottom: Math.min(box.bottom, innerHeight),
      left: Math.max(box.left, 0),
      right: Math.min(box.right, innerWidth),
      top: Math.max(box.top, 0)
    }
    const viewportIntersection =
      intersectionRect.right > intersectionRect.left && intersectionRect.bottom > intersectionRect.top
    window.__CODE_SCROLL_DIAG__ = {
      bounds: { bottom: box.bottom, height: box.height, left: box.left, right: box.right, top: box.top, width: box.width },
      hitInsideScroller,
      hitTag: hit?.tagName ?? null,
      intersectionRect,
      maxScrollLeft: scroller.scrollWidth - scroller.clientWidth,
      overflowX: getComputedStyle(scroller).overflowX,
      scrollEvents: 0,
      scrollLeftAfter: scroller.scrollLeft,
      scrollLeftBefore: scroller.scrollLeft,
      viewportIntersection,
      wheelEvents: 0
    }

    if (!(hitInsideScroller || viewportIntersection)) {
      return { invalid: true, x, y }
    }

    scroller.addEventListener('wheel', () => { window.__CODE_SCROLL_DIAG__.wheelEvents += 1 }, { passive: true })
    scroller.addEventListener('scroll', () => { window.__CODE_SCROLL_DIAG__.scrollEvents += 1 })

    const started = performance.now()
    window.__CODE_SCROLL_PAINT__ = new Promise(resolve => {
      let settled = false
      const finish = value => {
        if (!settled) { settled = true; resolve(value) }
      }
      scroller.addEventListener('scroll', () => {
        const deliveredAt = performance.now()
        requestAnimationFrame(() => {
          const paintedAt = performance.now()
          finish({
            paintMs: paintedAt - deliveredAt,
            rendererTotalMs: paintedAt - started,
            rendererWaitMs: deliveredAt - started
          })
        })
      }, { once: true })
      setTimeout(() => finish(null), 1500)
    })
    return { invalid: false, x, y }
  })()`)

  if (!armed || armed.invalid) {
    const diagnostic = await cdp.eval('window.__CODE_SCROLL_DIAG__ ?? null')
    throw new Error(`code-scroll-control could not arm a visible code scroller: ${JSON.stringify(diagnostic)}`)
  }

  const hostStartedAt = performance.now()
  await cdp.send('Input.dispatchMouseEvent', {
    deltaX: 220,
    deltaY: 0,
    type: 'mouseWheel',
    x: armed.x,
    y: armed.y
  })
  const receipt = await cdp.eval('window.__CODE_SCROLL_PAINT__')
  const diagnostic = await cdp.eval(`(() => {
    const scroller = document.querySelector('[data-perf-code-scroll-control="true"]')
    if (window.__CODE_SCROLL_DIAG__ && scroller) {
      window.__CODE_SCROLL_DIAG__.scrollLeftAfter = scroller.scrollLeft
    }
    return window.__CODE_SCROLL_DIAG__ ?? null
  })()`)
  if (!receipt) {
    throw new Error(`code-scroll-control did not receive a real scroll event: ${JSON.stringify({ diagnostic, prepared })}`)
  }

  return { ...receipt, diagnostic, hostMs: performance.now() - hostStartedAt, prepared }
}

export default {
  name: 'code-scroll-control',
  tier: 'report',
  description: 'Independent real code-card horizontal scroll input-to-paint control.',
  async run(cdp, opts = {}) {
    const turns = boundedInteger(opts.turns, 20, 1, 200)
    await cdp.send('Runtime.enable')

    const ready = await cdp.eval('!!window.__PERF_DRIVE__?.loadTranscript')
    if (!ready) throw new Error('code-scroll-control needs the explicit performance probe renderer')

    try {
      await cdp.eval(`window.__PERF_DRIVE__.loadTranscript(${turns})`)
      await waitFor(cdp, `document.querySelectorAll('[data-slot="code-card"]').length > 0`, 'real code card')
      await waitFor(cdp, `document.querySelectorAll('[data-slot="code-card"] pre').length > 0`, 'code pre')
      const interaction = await measureCodeScroll(cdp)

      return {
        metrics: {
          code_card_scroll_to_paint_ms: requiredInteractionMs(interaction, 'code scroll')
        },
        detail: {
          dom: { codeCards: interaction.prepared.cardCount },
          interaction,
          turns
        }
      }
    } finally {
      await cdp.eval(`(() => {
        delete window.__CODE_SCROLL_PAINT__
        delete window.__CODE_SCROLL_DIAG__
        window.__PERF_DRIVE__?.reset()
        return 'cleaned'
      })()`)
    }
  }
}
