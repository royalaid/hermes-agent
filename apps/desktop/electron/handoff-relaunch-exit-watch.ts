export type HandoffRelaunchExitWatchDisposition = 'active' | 'idle' | 'stop'

interface ClosableWatch {
  close: () => void
}

export interface HandoffRelaunchExitWatchIdentity {
  currentExecutable: string
  currentPid: number
  currentProcessStartedAt: number | null
  currentRoot: string
}

export interface HandoffRelaunchExitWatchOptions {
  activePollMs: number
  debounceMs: number
  hermesHome: string
  idlePollMs: number
  inspect: (
    identity: HandoffRelaunchExitWatchIdentity
  ) => HandoffRelaunchExitWatchDisposition | Promise<HandoffRelaunchExitWatchDisposition>
  currentExecutable: string
  currentPid: number
  resolveCurrentProcessStartedAt: (pid: number) => number | null | Promise<number | null>
  resolveCurrentRoot: () => string
  watchDirectory: (
    directory: string,
    onChange: (filename: string | Uint8Array | null) => void
  ) => ClosableWatch
}

export interface HandoffRelaunchExitWatch {
  start: () => Promise<boolean>
  stop: () => void
}

const REQUEST_PREFIX = '.hermes-update-relaunch-request-'

function isRelevantChange(filename: string | Uint8Array | null): boolean {
  if (filename === null) {
    return true
  }

  return String(filename).startsWith(REQUEST_PREFIX)
}

function unref(timer: ReturnType<typeof setTimeout>): void {
  const handle = timer as unknown as { unref?: () => void }
  handle.unref?.()
}

function isPositiveExactTimestamp(value: number | null): value is number {
  return Number.isSafeInteger(value) && value > 0
}

export function createHandoffRelaunchExitWatch(options: HandoffRelaunchExitWatchOptions): HandoffRelaunchExitWatch {
  let identity: HandoffRelaunchExitWatchIdentity | null = null
  let inspectionRunning = false
  let pendingWake = false
  let started = false
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let watcher: ClosableWatch | null = null

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  const stop = () => {
    if (stopped) {
      return
    }

    stopped = true
    clearTimer()
    watcher?.close()
    watcher = null
  }

  const schedule = (delayMs: number) => {
    if (stopped) {
      return
    }

    clearTimer()
    timer = setTimeout(() => {
      timer = null
      void inspectAndSchedule()
    }, delayMs)
    unref(timer)
  }

  const inspectAndSchedule = async (): Promise<HandoffRelaunchExitWatchDisposition> => {
    if (stopped || !identity) {
      return 'stop'
    }

    if (inspectionRunning) {
      pendingWake = true

      return 'active'
    }

    inspectionRunning = true
    let disposition: HandoffRelaunchExitWatchDisposition

    try {
      if (!isPositiveExactTimestamp(identity.currentProcessStartedAt)) {
        let resolvedProcessStartedAt: number | null = null

        try {
          resolvedProcessStartedAt = await options.resolveCurrentProcessStartedAt(options.currentPid)
        } catch {
          // An inconclusive identity probe must fail closed for this inspection,
          // but a later poll can retry it.
        }

        if (isPositiveExactTimestamp(resolvedProcessStartedAt)) {
          identity.currentProcessStartedAt = resolvedProcessStartedAt
        }
      }

      disposition = await options.inspect(identity)
    } finally {
      inspectionRunning = false
    }

    if (disposition === 'stop') {
      stop()

      return disposition
    }

    if (pendingWake) {
      pendingWake = false
      schedule(options.debounceMs)
    } else {
      schedule(disposition === 'active' ? options.activePollMs : options.idlePollMs)
    }

    return disposition
  }

  const onDirectoryChange = (filename: string | Uint8Array | null) => {
    if (!isRelevantChange(filename) || stopped) {
      return
    }

    if (inspectionRunning) {
      pendingWake = true

      return
    }

    schedule(options.debounceMs)
  }

  const start = async (): Promise<boolean> => {
    if (started) {
      return !stopped
    }

    started = true

    const currentRoot = options.resolveCurrentRoot()

    identity = {
      currentExecutable: options.currentExecutable,
      currentPid: options.currentPid,
      currentProcessStartedAt: null,
      currentRoot
    }

    try {
      watcher = options.watchDirectory(options.hermesHome, onDirectoryChange)
    } catch {
      // The low-frequency safety poll preserves correctness when fs.watch is
      // unavailable (missing directory, host limit, or transient OS error).
    }

    return (await inspectAndSchedule()) !== 'stop'
  }

  return { start, stop }
}
