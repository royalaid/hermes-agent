import assert from 'node:assert/strict'
import path from 'node:path'

import { describe, it } from 'vitest'

import { readCorrelatedInstallStamp, validateHandoffDesktopIdentity } from './handoff-desktop-proof'

const root = path.resolve('C:/Hermes')
// Packaged/NSIS Desktop is allowed outside the managed source install root.
const executable = path.resolve('C:/Users/Test/AppData/Local/Programs/Hermes/Hermes.exe')
const resources = path.join(path.dirname(executable), 'resources')
const buildId = 'a'.repeat(40)
const normalize = (value: string) => path.normalize(value)

describe('validateHandoffDesktopIdentity', () => {
  it('requires the exact PID, executable, resources directory, and install root', () => {
    const valid = {
      currentPid: 42,
      currentProcessStartedAt: 1_723_330_000,
      expectedPid: 42,
      expectedProcessStartedAt: 1_723_330_000,
      execPath: executable,
      expectedExecutable: executable,
      expectedRoot: root,
      resourcesPath: resources
    }

    const comparable = (value: string) =>
      process.platform === 'win32' ? normalize(value).toLowerCase() : normalize(value)

    assert.deepEqual(validateHandoffDesktopIdentity(valid, { realpath: normalize }), {
      executable: comparable(executable),
      root: comparable(root)
    })
    assert.equal(validateHandoffDesktopIdentity({ ...valid, currentPid: 43 }, { realpath: normalize }), null)
    assert.equal(
      validateHandoffDesktopIdentity(
        { ...valid, currentProcessStartedAt: valid.expectedProcessStartedAt + 2 },
        { realpath: normalize }
      ),
      null
    )
    assert.equal(
      validateHandoffDesktopIdentity({ ...valid, execPath: path.resolve('C:/Other/Hermes.exe') }, { realpath: normalize }),
      null
    )
    assert.equal(
      validateHandoffDesktopIdentity(
        { ...valid, resourcesPath: path.resolve('C:/Other/resources') },
        { realpath: normalize }
      ),
      null
    )
  })
})

describe('readCorrelatedInstallStamp', () => {
  const stamp = (overrides: Record<string, unknown> = {}) =>
    JSON.stringify({
      schemaVersion: 1,
      commit: buildId,
      branch: 'main',
      builtAt: '2026-08-10T20:00:00.000Z',
      dirty: false,
      source: 'ci',
      ...overrides
    })

  it('accepts only an exact clean non-fallback stamp matching the receipt build', () => {
    assert.deepEqual(readCorrelatedInstallStamp(resources, buildId, { readFile: () => stamp() }), {
      buildId,
      buildSource: 'install-stamp'
    })

    for (const raw of [
      stamp({ commit: 'b'.repeat(40) }),
      stamp({ commit: 'a'.repeat(7) }),
      stamp({ dirty: true }),
      stamp({ source: 'fallback' }),
      stamp({ extra: true }),
      stamp({ builtAt: 'not-a-date' })
    ]) {
      assert.equal(readCorrelatedInstallStamp(resources, buildId, { readFile: () => raw }), null)
    }
  })

  it('accepts an archive digest only when the stamp contains the same full digest', () => {
    const archiveId = 'b'.repeat(64)
    assert.deepEqual(
      readCorrelatedInstallStamp(resources, archiveId, {
        readFile: () => stamp({ commit: archiveId, source: 'local' })
      }),
      { buildId: archiveId, buildSource: 'install-stamp' }
    )
  })
})
