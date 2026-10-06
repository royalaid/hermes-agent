import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'

import { setBusy } from '@/store/session'
import { clearAllSessionTodos, restoreSessionTodosFromSnapshot, setSessionTodos } from '@/store/todos'

import { ComposerStatusStack } from './index'

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { disconnect() {} observe() {} })
})

afterEach(() => {
  cleanup()
  setBusy(false)
  clearAllSessionTodos()
})

it('shows the restored remaining plan without a stale running indicator', () => {
  setBusy(false)
  restoreSessionTodosFromSnapshot('remaining', {
    revision: 7,
    todos: [{ id: 'task', content: 'Still needs doing', status: 'in_progress' }]
  }, false)
  render(<MemoryRouter><ComposerStatusStack queue={null} sessionId="remaining" /></MemoryRouter>)
  expect(screen.getByText('Still needs doing')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
})

it('does not claim a cached in-progress item is executing while its turn is idle', () => {
  setBusy(false)
  setSessionTodos('remaining', [{ id: 'task', content: 'Paused step', status: 'in_progress' }], 7)
  render(<MemoryRouter><ComposerStatusStack queue={null} sessionId="remaining" /></MemoryRouter>)
  expect(screen.getByText('Paused step')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
})
