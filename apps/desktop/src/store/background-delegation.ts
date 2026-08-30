import { computed, type ReadableAtom } from 'nanostores'

import { $subagentsBySession, type SubagentProgress } from './subagents'

export interface BackgroundResume {
  /** Latest live activity from the primary child (its newest stream line), or
   *  null when nothing readable has arrived yet — the UI then falls back to the
   *  generic "will resume" copy. */
  activity: string | null
  /** Running/queued background children for this mounted session surface. */
  count: number
}

const RUNNING = (s: SubagentProgress) => s.status === 'running' || s.status === 'queued'

/**
 * "Parked" background-delegation signal for one mounted session surface.
 *
 * A top-level `delegate_task` always runs in the background: the parent turn
 * ends while the subagent keeps running, and its result re-enters the
 * conversation as a fresh turn when it finishes. During that window the
 * surface is genuinely idle but work is still happening elsewhere, so we
 * surface a calm status line instead of a spinner that reads as "stuck."
 *
 * The runtime-id and busy stores must come from the mounted `SessionView`.
 * Several session surfaces may be mounted simultaneously; binding this signal
 * to global active-session state would let one surface paint another's work.
 */
export function backgroundResumeForSession(
  $runtimeId: ReadableAtom<null | string>,
  $surfaceBusy: ReadableAtom<boolean>
): ReadableAtom<BackgroundResume | null> {
  const $sessionSubagents = computed([$subagentsBySession, $runtimeId], (bySession, sid) =>
    sid ? (bySession[sid] ?? []) : []
  )

  return computed([$sessionSubagents, $surfaceBusy], (subagents, busy): BackgroundResume | null => {
    if (busy) {
      return null
    }

    const running = subagents.filter(RUNNING)

    if (running.length === 0) {
      return null
    }

    const activity = (running[0]!.stream.at(-1)?.text ?? '').trim() || null

    return { activity, count: running.length }
  })
}
