// Read-only CDP watcher for an already-running, explicitly selected renderer.
// It never drives the app or starts instrumentation: Runtime.evaluate only reads
// counters and DOM state that are already present.
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

import { CDP, sleep } from './lib/cdp.mjs'

const usage = () => 'usage: node scripts/perf/watch.mjs --port <cdp-port> [--samples 6] [--interval-ms 1000]'

export const parseWatchArgs = argv => {
  const flags = {}

  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]

    if (key.startsWith('--')) {
      flags[key.slice(2)] = argv[i + 1]?.startsWith('--') || argv[i + 1] == null ? true : argv[++i]
    }
  }

  if (!/^\d+$/.test(String(flags.port ?? ''))) {
    throw new Error(usage())
  }

  return {
    intervalMs: Math.min(10000, Math.max(100, Number(flags['interval-ms'] ?? 1000))),
    port: Number(flags.port),
    samples: Math.min(60, Math.max(1, Number(flags.samples ?? 6)))
  }
}

export const selectSingleWatchTarget = (targets, port) => {
  const pages = targets.filter(target => target?.type === 'page' && typeof target.webSocketDebuggerUrl === 'string')

  if (pages.length !== 1) {
    throw new Error(`ambiguous CDP page targets on explicit port ${port}: expected 1, found ${pages.length}`)
  }

  return pages[0]
}

export const safeTargetIdentity = (target, port) => ({
  port,
  targetId: String(target.id),
  type: String(target.type)
})

async function discoverSingleWatchTarget(port, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs

  for (;;) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      return selectSingleWatchTarget(targets, port)
    } catch (error) {
      if (String(error?.message ?? '').startsWith('ambiguous CDP page targets')) {
        throw error
      }

      if (Date.now() >= deadline) {
        throw new Error(`no unambiguous CDP page target on explicit port ${port}`)
      }

      await sleep(100)
    }
  }
}

const SNAPSHOT = `(() => {
  const visible = selector => [...document.querySelectorAll(selector)].filter(node => {
    const box = node.getBoundingClientRect()
    return box.width > 0 && box.height > 0
  }).length
  const live = window.__PERF_LIVE__?.last()
  const renderRows = window.__RENDER_COUNTS__?.report?.() ?? []
  const render = renderRows.reduce((total, row) => ({
    renders: total.renders + (Number(row.renders) || 0),
    wasted: total.wasted + (Number(row.wasted) || 0)
  }), { renders: 0, wasted: 0 })
  const viewport = document.querySelector('[data-slot="aui_thread-viewport"]')
  const memory = performance.memory

  return {
    dom: {
      assistantMessages: document.querySelectorAll('[data-slot="aui_assistant-message-root"]').length,
      codeCards: document.querySelectorAll('[data-slot="code-card"]').length,
      composer: document.querySelectorAll('[data-slot="composer-rich-input"]').length,
      delegateCards: document.querySelectorAll('[data-delegate-card]').length,
      delegateRows: document.querySelectorAll('[data-delegate-card] [data-conversation-scaffold]').length,
      statusRows: document.querySelectorAll('[role="status"][data-conversation-scaffold]').length,
      visibleCodeCards: visible('[data-slot="code-card"]'),
      visibleComposers: visible('[data-slot="composer-rich-input"]'),
      visibleDelegateCards: visible('[data-delegate-card]'),
      visibleThreadViewports: visible('[data-slot="aui_thread-viewport"]')
    },
    heapMb: memory ? Math.round(memory.usedJSHeapSize / 1048576) : null,
    live: live ? {
      commits: Number(live.commits) || 0,
      fps: Number(live.fps) || 0,
      frames: Number(live.frames) || 0,
      longFrames: Array.isArray(live.longFrames) ? live.longFrames.length : 0,
      ms: Number(live.ms) || 0,
      p95: Number(live.p95) || 0,
      slow33: Number(live.slow33) || 0,
      worst: Number(live.worst) || 0
    } : null,
    render: {
      commits: Number(window.__RENDER_COUNTS__?.commits?.()) || 0,
      components: renderRows.length,
      renders: render.renders,
      wasted: render.wasted
    },
    scroll: viewport ? {
      height: Math.round(viewport.scrollHeight),
      top: Math.round(viewport.scrollTop),
      viewport: Math.round(viewport.clientHeight)
    } : null
  }
})()`

export async function collectWatchSnapshots(cdp, opts, write = line => process.stdout.write(`${line}\n`), wait = sleep) {
  for (let index = 0; index < opts.samples; index += 1) {
    const snapshot = await cdp.eval(SNAPSHOT)
    write(JSON.stringify({ sample: index + 1, snapshot, target: opts.target }))

    if (index + 1 < opts.samples) {
      await wait(opts.intervalMs)
    }
  }
}

async function main() {
  const opts = parseWatchArgs(process.argv.slice(2))
  const target = await discoverSingleWatchTarget(opts.port)
  const targetIdentity = safeTargetIdentity(target, opts.port)
  const cdp = await CDP.open(target.webSocketDebuggerUrl)

  try {
    await collectWatchSnapshots(cdp, { ...opts, target: targetIdentity })
  } finally {
    cdp.close()
  }
}

export const isWatchMainModule = (moduleUrl, entryPath, cwd = process.cwd()) =>
  Boolean(entryPath) && moduleUrl === pathToFileURL(resolve(cwd, entryPath)).href

if (isWatchMainModule(import.meta.url, process.argv[1])) {
  main().catch(error => {
    console.error(error.message)
    process.exit(2)
  })
}
