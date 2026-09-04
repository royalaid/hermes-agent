import { describe, expect, it } from 'vitest'

import { appendCappedLogLines, formatDesktopLogLine } from './desktop-log-line'

describe('formatDesktopLogLine', () => {
  it('prefixes each line with an ISO-8601 timestamp and the hermes tag', () => {
    const line = formatDesktopLogLine('[boot] Resolving Hermes backend')

    // Shape contract (not a snapshot): every desktop log line starts with
    // an ISO timestamp so multi-surface logs are chronologically readable.
    // See #84405.
    expect(line).toMatch(
      /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] \[hermes\] \[boot\] Resolving Hermes backend$/
    )
  })

  it('keeps the message verbatim after the prefix', () => {
    const line = formatDesktopLogLine('Hermes backend exited (0)')

    expect(line).toMatch(/^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] \[hermes\] Hermes backend exited \(0\)$/)
  })
})

describe('appendCappedLogLines', () => {
  it('does not throw when the log sink has not been initialized yet', () => {
    expect(appendCappedLogLines(undefined, ['[pool-limits] no saved file'])).toBe(false)
    expect(appendCappedLogLines(null, ['[pool-limits] no saved file'])).toBe(false)
  })

  it('appends and caps the live sink', () => {
    const log: string[] = ['old']

    expect(appendCappedLogLines(log, ['a', 'b'], 3)).toBe(true)
    expect(log).toEqual(['old', 'a', 'b'])
    expect(appendCappedLogLines(log, ['c'], 3)).toBe(true)
    expect(log).toEqual(['a', 'b', 'c'])
  })
})
