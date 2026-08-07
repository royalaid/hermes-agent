import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveDefaultUpdateBranch } from './update-branch'

test('uses the checked-out branch when it is published and no user branch is configured', () => {
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: '', currentBranch: 'fork-integration', published: true }),
    'fork-integration'
  )
})

test('keeps an explicit user branch even when the checkout has another published branch', () => {
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: 'release', currentBranch: 'fork-integration', published: true }),
    'release'
  )
})

test('falls back to main for a detached or unpublished checkout', () => {
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: '', currentBranch: 'HEAD', published: true }),
    'main'
  )
  assert.equal(
    resolveDefaultUpdateBranch({ configuredBranch: '', currentBranch: 'experiment', published: false }),
    'main'
  )
})
