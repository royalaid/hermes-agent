import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  _resetSessionBindingsForTests,
  bindingsEqual,
  bindRuntimeToSession,
  claimSessionBinding,
  clearMainSessionBinding,
  detachRuntimeForSession,
  getMainSessionBinding,
  nextSessionBindingGeneration,
  normalizeSessionBinding,
  runtimeForExactSessionBinding,
  sessionBindingKey,
  sessionBindingOwnsGeneration,
  setMainSessionBinding,
  setSessionBindingRuntimeAdapter
} from './session-binding'

const binding = (overrides: Partial<Parameters<typeof normalizeSessionBinding>[0]> = {}) => ({
  storedSessionId: ' shared-id ',
  ownerRoute: {
    connectionId: ' source-a ',
    mode: 'local' as const,
    profile: ' ',
    targetProfile: ''
  },
  ...overrides
})

describe('session binding', () => {
  beforeEach(() => {
    localStorage.clear()
    _resetSessionBindingsForTests()
  })

  it('normalizes and keys the durable owner tuple while excluding mode', () => {
    const normalized = normalizeSessionBinding(binding())

    expect(normalized).toEqual({
      storedSessionId: 'shared-id',
      ownerRoute: {
        connectionId: 'source-a',
        mode: 'local',
        profile: 'default',
        targetProfile: 'default'
      }
    })
    expect(sessionBindingKey(normalized!)).toBe('["shared-id","source-a","default","default"]')
    expect(
      bindingsEqual(normalized, {
        storedSessionId: 'shared-id',
        ownerRoute: {
          connectionId: 'source-a',
          mode: 'remote',
          profile: 'default',
          targetProfile: 'default'
        }
      })
    ).toBe(true)
  })

  it('rejects empty durable ids and treats target-profile changes as a different owner', () => {
    expect(normalizeSessionBinding(binding({ storedSessionId: ' ' }))).toBeNull()
    expect(
      normalizeSessionBinding({ storedSessionId: 'x', ownerRoute: { connectionId: ' ', profile: 'default' } })
    ).toBeNull()
    expect(
      bindingsEqual(
        normalizeSessionBinding(binding())!,
        normalizeSessionBinding({
          storedSessionId: 'shared-id',
          ownerRoute: { connectionId: 'source-a', profile: 'default', targetProfile: 'other' }
        })!
      )
    ).toBe(false)
  })

  it('keeps same-owner runtime warm and atomically detaches a competing owner', () => {
    const exactA = normalizeSessionBinding(binding())!

    const exactB = normalizeSessionBinding({
      storedSessionId: 'shared-id',
      ownerRoute: { connectionId: 'source-b', profile: 'profile-b', targetProfile: 'target-b' }
    })!

    const invalidated: string[] = []

    setSessionBindingRuntimeAdapter({
      detach(storedSessionId) {
        invalidated.push(storedSessionId)

        return 'runtime-a'
      }
    })
    const generation = nextSessionBindingGeneration('shared-id')
    bindRuntimeToSession(exactA, 'runtime-a', generation)

    expect(runtimeForExactSessionBinding(exactA)).toBe('runtime-a')
    expect(runtimeForExactSessionBinding({ ...exactA, ownerRoute: { ...exactA.ownerRoute, mode: 'remote' } })).toBe(
      'runtime-a'
    )
    expect(runtimeForExactSessionBinding(exactB)).toBeNull()
    expect(detachRuntimeForSession(exactB, exactA)).toEqual({ detached: true, runtimeId: 'runtime-a' })
    expect(invalidated).toEqual(['shared-id'])
    expect(runtimeForExactSessionBinding(exactA)).toBeNull()
    expect(sessionBindingOwnsGeneration(exactA, generation)).toBe(false)
  })

  it('does not detach or advance a same-owner binding', () => {
    const exact = normalizeSessionBinding(binding())!
    const detach = vi.fn()
    setSessionBindingRuntimeAdapter({ detach })
    const generation = nextSessionBindingGeneration('shared-id')
    bindRuntimeToSession(exact, 'runtime-a', generation)

    expect(detachRuntimeForSession({ ...exact, ownerRoute: { ...exact.ownerRoute, mode: 'remote' } }, exact)).toEqual({
      detached: false,
      runtimeId: 'runtime-a'
    })
    expect(detach).not.toHaveBeenCalled()
    expect(sessionBindingOwnsGeneration(exact, generation)).toBe(true)
  })

  it('rejects a late runtime bind after a competing owner claims the same stored id', () => {
    const exactA = normalizeSessionBinding(binding())!

    const exactB = normalizeSessionBinding({
      storedSessionId: 'shared-id',
      ownerRoute: { connectionId: 'source-b', profile: 'profile-b', targetProfile: 'target-b' }
    })!

    const generationA = claimSessionBinding(exactA)
    const generationB = claimSessionBinding(exactB)

    expect(generationB).toBeGreaterThan(generationA)
    expect(bindRuntimeToSession(exactA, 'runtime-a-late', generationA)).toBe(false)
    expect(bindRuntimeToSession(exactB, 'runtime-b', generationB)).toBe(true)
    expect(runtimeForExactSessionBinding(exactA)).toBeNull()
    expect(runtimeForExactSessionBinding(exactB)).toBe('runtime-b')
  })

  it('persists exact main authority by remembered-navigation scope and validates the restored id', () => {
    const exact = normalizeSessionBinding(binding())!

    setMainSessionBinding(exact, 'profile-a', '.connection.source-a')

    expect(getMainSessionBinding('shared-id', 'profile-a', '.connection.source-a')).toEqual(exact)
    expect(getMainSessionBinding('other-id', 'profile-a', '.connection.source-a')).toBeNull()
    expect(getMainSessionBinding('shared-id', 'profile-a', '.connection.source-b')).toBeNull()

    clearMainSessionBinding('profile-a', '.connection.source-a')
    expect(getMainSessionBinding('shared-id', 'profile-a', '.connection.source-a')).toBeNull()
  })
})
