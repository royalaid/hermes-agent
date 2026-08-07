import assert from 'node:assert/strict'

import { test } from 'vitest'

import { publicationStateFromExitCode, resolveDefaultUpdateBranch } from './update-branch'

test('maps git publication probes without collapsing transient failures into absence', () => {
  assert.equal(publicationStateFromExitCode(0), 'present')
  assert.equal(publicationStateFromExitCode(2), 'missing')
  assert.equal(publicationStateFromExitCode(1), 'unknown')
  assert.equal(publicationStateFromExitCode(null), 'unknown')
})

test('uses the checked-out branch when it is published and no user branch is configured', () => {
  assert.equal(
    resolveDefaultUpdateBranch({
      configuredBranch: '',
      currentBranch: 'fork-integration',
      publication: 'present'
    }),
    'fork-integration'
  )
})

test('keeps an explicit user branch even when the checkout has another published branch', () => {
  assert.equal(
    resolveDefaultUpdateBranch({
      configuredBranch: 'release',
      currentBranch: 'fork-integration',
      publication: 'missing'
    }),
    'release'
  )
})

test('falls back to main for a detached or unpublished checkout', () => {
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: '', currentBranch: 'HEAD', publication: 'present' }),
    'main'
  )
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: '', currentBranch: 'experiment', publication: 'missing' }),
    'main'
  )
})

test('keeps the checked-out branch when publication cannot be determined', () => {
  assert.equal(
    resolveDefaultUpdateBranch({
      configuredBranch: '',
      currentBranch: 'fork-integration',
      publication: 'unknown'
    }),
    'fork-integration'
  )
})
