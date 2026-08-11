import assert from 'node:assert/strict'
import path from 'node:path'

import { describe, it } from 'vitest'

import {
  canonicalPathIdentity,
  hasExactKeys,
  hasHandoffCapabilitySyntax,
  resolveCanonicalAbsolutePath,
  sameCanonicalPath
} from './handoff-wire-validation'

describe('handoff wire validation', () => {
  it('requires the exact object keys without depending on key order', () => {
    const expected = ['first', 'second']

    assert.equal(hasExactKeys({ second: 2, first: 1 }, expected), true)
    assert.equal(hasExactKeys({ first: 1 }, expected), false)
    assert.equal(hasExactKeys({ first: 1, second: 2, extra: 3 }, expected), false)
    assert.equal(hasExactKeys(['first', 'second'], expected), false)
    assert.equal(hasExactKeys(null, expected), false)
    assert.deepEqual(expected, ['first', 'second'])
  })

  it('accepts only bounded handoff capability syntax', () => {
    assert.equal(hasHandoffCapabilitySyntax('a'.repeat(16)), true)
    assert.equal(hasHandoffCapabilitySyntax('A0._-capability-id'), true)
    assert.equal(hasHandoffCapabilitySyntax('a'.repeat(128)), true)
    assert.equal(hasHandoffCapabilitySyntax('a'.repeat(15)), false)
    assert.equal(hasHandoffCapabilitySyntax('a'.repeat(129)), false)
    assert.equal(hasHandoffCapabilitySyntax('capability/123456'), false)
  })

  it('resolves only canonical absolute paths through the injected realpath', () => {
    const absolute = path.resolve('Hermes', 'Hermes.exe')
    const calls: string[] = []

    const realpath = (candidate: string): string => {
      calls.push(candidate)

      return `${candidate}.canonical`
    }

    assert.equal(resolveCanonicalAbsolutePath(absolute, realpath), `${absolute}.canonical`)
    assert.deepEqual(calls, [absolute])
    assert.equal(resolveCanonicalAbsolutePath('relative/Hermes.exe', realpath), null)
    assert.deepEqual(calls, [absolute])
    assert.equal(
      resolveCanonicalAbsolutePath(absolute, () => {
        throw new Error('unreadable')
      }),
      null
    )
  })

  it('uses case-insensitive canonical identity only on Windows', () => {
    assert.equal(canonicalPathIdentity('C:\\Hermes\\Hermes.EXE', 'win32'), 'c:\\hermes\\hermes.exe')
    assert.equal(canonicalPathIdentity('/Hermes/Hermes', 'linux'), '/Hermes/Hermes')
    assert.equal(sameCanonicalPath('C:\\Hermes\\Hermes.EXE', 'c:\\hermes\\hermes.exe', 'win32'), true)
    assert.equal(sameCanonicalPath('/Hermes/Hermes', '/hermes/hermes', 'linux'), false)
  })
})
