import { afterEach, expect, it } from 'vitest'

import type { TodoItem } from '@/lib/todos'

import {
  $todosBySession, clearAllSessionTodos, clearActiveSessionTodos,
  restoreSessionTodosFromSnapshot, setSessionTodos, todosForHydration
} from './todos'

const plan: TodoItem[] = [
  { id: 'done', content: 'Finished step', status: 'completed' },
  { id: 'remaining', content: 'Still needs doing', status: 'in_progress' }
]

afterEach(clearAllSessionTodos)

it('restores unfinished canonical history as remaining work in an idle session', () => {
  expect(todosForHydration(plan)).toEqual(plan)
  restoreSessionTodosFromSnapshot('idle', { todos: plan, revision: 7 }, false)
  expect($todosBySession.get().idle).toEqual(plan)
})

it('keeps the remaining plan when the turn stops without a final Todo call', () => {
  setSessionTodos('stopped', plan, 8)
  clearActiveSessionTodos('stopped')
  expect($todosBySession.get().stopped).toEqual(plan)
})

it('does not let an unversioned REST fallback replace newer channel authority', () => {
  restoreSessionTodosFromSnapshot('warm', { todos: plan, revision: 9 }, true)
  restoreSessionTodosFromSnapshot('warm', { todos: [plan[0]] }, false)
  expect($todosBySession.get().warm).toEqual(plan)
})

it('honors a newer explicit clear instead of reviving the previous plan', () => {
  restoreSessionTodosFromSnapshot('warm', { todos: plan, revision: 9 }, false)
  restoreSessionTodosFromSnapshot('warm', { todos: [], revision: 10 }, false)
  restoreSessionTodosFromSnapshot('warm', { todos: plan, revision: 9 }, false)
  expect($todosBySession.get().warm ?? []).toEqual([])
})
