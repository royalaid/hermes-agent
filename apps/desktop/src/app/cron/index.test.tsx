import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal()),
  getCronJobRuns: vi.fn(),
  getLatestSessionMessages: vi.fn()
}))

const { getCronJobRuns, getLatestSessionMessages } = await import('@/hermes')

import { en } from '@/i18n/en'
import type { SessionInfo, SessionMessage } from '@/types/hermes'

import { ACTIVE_RUN_PROGRESS_POLL_INTERVAL_MS, CronJobRuns, lastNonEmptyLine } from './index'

afterEach(cleanup)

const session = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const ACTIVE_RUN_ID = 'cron_job-1_20260101_000000'
const INACTIVE_RUN_ID = 'cron_job-1_20251231_000000'

function messagesResponse(assistantContent: string) {
  return {
    messages: [
      { content: 'no_agent script: sleep_canary.py', role: 'user' },
      { content: assistantContent, role: 'assistant' }
    ] satisfies SessionMessage[],
    session_id: ACTIVE_RUN_ID
  }
}

const TWO_STAGE_DOC = [
  '# Cron Job: sleep-canary',
  '',
  '**Job ID:** job-1',
  '**Mode:** no_agent (script) — running',
  '**Status:** 2 stage(s) so far',
  '',
  '---',
  '',
  '- [ok] warmup: sleep-canary stage 1/4 ok',
  '- [ok] probe: sleep-canary stage 2/4 ok'
].join('\n')

const THREE_STAGE_DOC = `${TWO_STAGE_DOC}\n- [ok] verify: sleep-canary stage 3/4 ok`

describe('CronJobRuns live progress (U8)', () => {
  beforeEach(() => {
    vi.mocked(getCronJobRuns).mockReset()
    vi.mocked(getLatestSessionMessages).mockReset()
  })

  it('shows the last stage line for an is_active run from the in-place-updated message', async () => {
    vi.mocked(getCronJobRuns).mockResolvedValue([
      session({
        id: ACTIVE_RUN_ID,
        is_active: true,
        last_active: 1_700_000_000,
        preview: 'no_agent script: sleep_canary.py',
        started_at: 1_700_000_000,
        title: null
      })
    ])
    vi.mocked(getLatestSessionMessages).mockResolvedValue(messagesResponse(TWO_STAGE_DOC))

    render(<CronJobRuns c={en.cron} jobId="job-1" />)

    expect(await screen.findByText('- [ok] probe: sleep-canary stage 2/4 ok')).toBeTruthy()
    expect(getLatestSessionMessages).toHaveBeenCalledWith(ACTIVE_RUN_ID)
  })

  it('updates the displayed line as the progress message keeps growing (real poll)', async () => {
    vi.mocked(getCronJobRuns).mockResolvedValue([
      session({
        id: ACTIVE_RUN_ID,
        is_active: true,
        last_active: 1_700_000_000,
        preview: 'no_agent script: sleep_canary.py',
        started_at: 1_700_000_000,
        title: null
      })
    ])
    vi.mocked(getLatestSessionMessages)
      .mockResolvedValueOnce(messagesResponse(TWO_STAGE_DOC))
      .mockResolvedValue(messagesResponse(THREE_STAGE_DOC))

    render(<CronJobRuns c={en.cron} jobId="job-1" />)

    expect(await screen.findByText('- [ok] probe: sleep-canary stage 2/4 ok')).toBeTruthy()
    // The component's own poll (ACTIVE_RUN_PROGRESS_POLL_INTERVAL_MS) picks
    // up the next in-place update on its own — no manual re-render/refetch
    // trigger from the test. Real timers, generous timeout.
    expect(
      await screen.findByText(
        '- [ok] verify: sleep-canary stage 3/4 ok',
        {},
        { timeout: ACTIVE_RUN_PROGRESS_POLL_INTERVAL_MS + 2000 }
      )
    ).toBeTruthy()
  })

  it('never fetches or shows progress for a non-active run', async () => {
    vi.mocked(getCronJobRuns).mockResolvedValue([
      session({
        id: INACTIVE_RUN_ID,
        is_active: false,
        last_active: 1_700_000_000,
        preview: 'no_agent script: sleep_canary.py',
        started_at: 1_700_000_000,
        title: 'finished run'
      })
    ])

    render(<CronJobRuns c={en.cron} jobId="job-1" />)

    expect(await screen.findByText('finished run')).toBeTruthy()
    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect(screen.queryByText(/^- \[ok\]/)).toBeNull()
  })
})

describe('lastNonEmptyLine', () => {
  it('returns the last non-blank line of a growing progress doc', () => {
    expect(lastNonEmptyLine(TWO_STAGE_DOC)).toBe('- [ok] probe: sleep-canary stage 2/4 ok')
    expect(lastNonEmptyLine(THREE_STAGE_DOC)).toBe('- [ok] verify: sleep-canary stage 3/4 ok')
  })

  it('ignores trailing blank lines', () => {
    expect(lastNonEmptyLine('a\nb\n\n\n')).toBe('b')
  })

  it('returns an empty string for blank input', () => {
    expect(lastNonEmptyLine('')).toBe('')
    expect(lastNonEmptyLine('\n\n')).toBe('')
  })
})
