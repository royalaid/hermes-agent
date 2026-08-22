import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import { buildExactTerminateScript } from './windows-process-terminate'
import { buildRestartManagerScript } from './windows-restart-manager'

describe('direct native exact-holder boundary', () => {
  it('does not delegate mutation to a detached watcher or target Job Object', () => {
    const script = buildExactTerminateScript()

    assert.doesNotMatch(script, /CreateJobObject|AssignProcessToJobObject|watcher/i)
    assert.match(script, /TerminateProcess/)
    assert.match(script, /WaitForSingleObject/)
    assert.match(script, /RmGetList/)
  })

  it('uses the same retained termination handle for final validation, mutation, and bounded drain', () => {
    const script = buildExactTerminateScript()

    assert.doesNotMatch(script, /queryHandle|terminateHandle/)
    assert.match(script, /ValidateHandle\(\s*processHandle,/)
    assert.match(script, /TerminateProcess\(processHandle,/)
    assert.match(script, /WaitForSingleObject\(processHandle, \(uint\)remaining\)/)
    assert.doesNotMatch(script, /INFINITE/)
  })

  it('gets leaf-order evidence from an exact current RM generation', () => {
    const script = buildRestartManagerScript([String.raw`C:\Hermes\venv\locked.pyd`])

    assert.match(script, /ParentPidForExactProcess/)
    assert.match(script, /parentPid/)
    assert.doesNotMatch(script, /Math\.Abs/)
  })
})
