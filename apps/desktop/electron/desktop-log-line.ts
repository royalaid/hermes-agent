/**
 * Desktop log line formatting shared by every desktop log surface:
 * `desktop.log`, the in-app "RECENT LOGS" view, and crash forensics.
 *
 * Historically each line was prefixed with just `[hermes] `, so lines from
 * different moments were indistinguishable. Every surface now carries an
 * ISO-8601 UTC timestamp, matching the Python-side `agent.log` /
 * `gateway.log` convention (`2026-07-12 16:22:17,540 INFO ...`). See #84405.
 */

/**
 * Format one desktop log line with an ISO-8601 UTC timestamp.
 *
 * `stamp` defaults to now; callers that batch multiple lines (a single
 * stdout chunk) pass one shared stamp so the group reads as one event.
 */
export function formatDesktopLogLine(text: string, stamp = new Date().toISOString()): string {
  return `[${stamp}] [hermes] ${text}`
}

export const HERMES_LOG_CAP = 300

/**
 * Append formatted lines to the in-memory desktop log.
 *
 * Bundlers lower `const hermesLog = []` past module-scope callers of
 * `rememberLog` (pool-limits load runs at import). A missing sink must not
 * throw — that crash is an uncaught main-process exception and the window
 * never opens.
 */
export function appendCappedLogLines(
  log: string[] | null | undefined,
  lines: readonly string[],
  cap = HERMES_LOG_CAP
): boolean {
  if (!Array.isArray(log) || lines.length === 0) {
    return false
  }

  log.push(...lines)

  if (log.length > cap) {
    log.splice(0, log.length - cap)
  }

  return true
}
