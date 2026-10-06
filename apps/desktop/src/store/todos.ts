import { atom, computed } from 'nanostores'

import { keyedTimeouts } from '@/lib/keyed-timeouts'
import { stableRecord } from '@/lib/stable-array'
import { parseTodoRevision, parseTodos, type TodoItem } from '@/lib/todos'

import { $sessions, lineageAliases } from './session'
import { $sessionStates } from './session-states'

/**
 * Current plan per runtime session, rendered by the composer status stack
 * (the inline transcript panel is gone). Fed from two places:
 *
 * - live `todo` tool events (use-message-stream)
 * - stored-session hydration from canonical history. Remaining work survives
 *   turn end; the session's confirmed turn state owns execution indicators.
 */
export const $todosBySession = atom<Record<string, TodoItem[]>>({})
export const $todoRevisionsBySession = atom<Record<string, number>>({})
/** Last authoritative snapshot, separate from the transient live panel. */
export const $retainedTodosBySession = atom<Record<string, TodoItem[]>>({})

export const todoListActive = (todos: readonly TodoItem[]) =>
  todos.some(t => t.status === 'pending' || t.status === 'in_progress')

let todoProgress: Readonly<Record<string, string>> = {}

/** Live "X/Y" per STORED session id, for the sidebar's inbox cards. The live
 *  map keys on runtime ids; this projects through the same storedSessionId +
 *  lineage-alias fallback as the working/attention projections, so the card
 *  finds its count under the id the sidebar knows. Cancelled items don't
 *  count toward either side of the fraction. Values are the rendered "X/Y"
 *  string — primitives, so stableRecord can suppress no-op emits. */
export const $todoProgressBySession = computed(
  [$todosBySession, $sessionStates, $sessions],
  (todosMap, states, sessions) => {
    const next: Record<string, string> = {}

    for (const [runtimeId, todos] of Object.entries(todosMap)) {
      const counted = todos.filter(t => t.status !== 'cancelled')

      if (counted.length === 0) {
        continue
      }

      const progress = `${counted.filter(t => t.status === 'completed').length}/${counted.length}`

      for (const alias of lineageAliases(states[runtimeId]?.storedSessionId ?? runtimeId, sessions)) {
        next[alias] = progress
      }
    }

    return (todoProgress = stableRecord(todoProgress, next))
  }
)

// A plan's unfinished rows are remaining work, not evidence of a live turn.
// Restore canonical history without manufacturing execution liveness.
export function todosForHydration(todos: readonly TodoItem[] | null): TodoItem[] | null {
  return todos ? [...todos] : null
}

// Once a list finishes (every item completed/cancelled), the final state
// lingers just long enough to see the last checkmark land, then the group
// drops out of the stack on its own.
const FINISHED_LINGER_MS = 4_000
const clearTimers = keyedTimeouts()

const hasSessionTodos = (map: Record<string, TodoItem[]>, sid: string): boolean => Object.hasOwn(map, sid)

const getSessionTodos = (map: Record<string, TodoItem[]>, sid: string): TodoItem[] | undefined =>
  hasSessionTodos(map, sid) ? map[sid] : undefined

function acceptRevision(sid: string, revision?: null | number): boolean {
  const revisions = $todoRevisionsBySession.get()
  const current = revisions[sid]

  // tool.start has no revision. Apply the merge locally and leave the
  // watermark alone so a later todo.updated / tool.complete can still win.
  if (revision == null) {
    return true
  }

  if (current != null && revision < current) {
    return false
  }

  if (current !== revision) {
    $todoRevisionsBySession.set({ ...revisions, [sid]: revision })
  }

  return true
}

function retainSessionTodos(sid: string, todos: TodoItem[]) {
  const current = $retainedTodosBySession.get()

  if (todos.length) {
    $retainedTodosBySession.set({ ...current, [sid]: todos })
  } else if (sid in current) {
    const { [sid]: _drop, ...rest } = current
    $retainedTodosBySession.set(rest)
  }
}

export function setSessionTodos(sid: string, todos: TodoItem[], revision?: null | number) {
  if (!sid) {
    return
  }

  if (!acceptRevision(sid, revision)) {
    return
  }

  // An unversioned tool.start is optimistic, not a durable result.
  if (revision != null) {
    retainSessionTodos(sid, todos)
  }

  clearTimers.cancel(sid)
  $todosBySession.set({ ...$todosBySession.get(), [sid]: todos })

  if (!todoListActive(todos)) {
    clearTimers.schedule(sid, FINISHED_LINGER_MS, () => dropSessionTodos(sid, false))
  }
}

function dropSessionTodos(sid: string, forgetRevision: boolean) {
  clearTimers.cancel(sid)

  const map = $todosBySession.get()

  if (hasSessionTodos(map, sid)) {
    const { [sid]: _drop, ...rest } = map
    $todosBySession.set(rest)
  }

  if (forgetRevision) {
    const revisions = $todoRevisionsBySession.get()

    if (Object.hasOwn(revisions, sid)) {
      const { [sid]: _drop, ...rest } = revisions
      $todoRevisionsBySession.set(rest)
    }

    retainSessionTodos(sid, [])
  }
}

export function clearSessionTodos(sid: string) {
  dropSessionTodos(sid, true)
}

export function clearAllSessionTodos() {
  const ids = new Set([
    ...Object.keys($todosBySession.get()),
    ...Object.keys($todoRevisionsBySession.get()),
    ...Object.keys($retainedTodosBySession.get())
  ])

  for (const sid of ids) {
    clearSessionTodos(sid)
  }
}

// Turn end retires execution, not the remaining plan. Preserve unfinished rows
// for later review; finished lists retain their normal short linger.
export function clearActiveSessionTodos(sid: string) {
  const todos = getSessionTodos($todosBySession.get(), sid)

  if (!todos || !todoListActive(todos)) {
    return
  }

  retainSessionTodos(sid, todos)
}

/** Apply a session.resume/activate or todo.updated full snapshot. The channel's
 * revisioned authority outranks an unversioned display-page fallback. */
export function restoreSessionTodosFromSnapshot(sid: string, snapshot: unknown, running: boolean) {
  const todos = parseTodos(snapshot)

  if (!sid || todos === null) {
    return
  }

  const revision = parseTodoRevision(snapshot)
  if (revision == null && Object.hasOwn($todoRevisionsBySession.get(), sid)) {
    return
  }

  // An unused store serializes as {todos: [], revision: 0}. That is not a
  // real snapshot. Applying it would stamp watermark 0 and leave an empty
  // list in the map.
  if (todos.length === 0 && (revision == null || revision === 0)) {
    return
  }

  const visible = running ? todos : todosForHydration(todos)

  if (visible !== null) {
    setSessionTodos(sid, visible, revision)
  }
}
