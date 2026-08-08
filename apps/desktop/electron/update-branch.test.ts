import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { test } from 'vitest'

import { branchPublicationProbeArgs, publicationStateFromExitCode, resolveDefaultUpdateBranch } from './update-branch'

test('probes an exact branch ref instead of matching a nested branch suffix', () => {
  const root = mkdtempSync(join(tmpdir(), 'hermes-update-branch-'))
  const remote = join(root, 'remote.git')

  try {
    execFileSync('git', ['init', '--bare', '--quiet', remote])

    const tree = execFileSync('git', ['--git-dir', remote, 'mktree'], {
      encoding: 'utf8',
      input: ''
    }).trim()

    const commit = execFileSync('git', ['--git-dir', remote, 'commit-tree', tree, '-m', 'probe'], {
      encoding: 'utf8',
      env: {
        ...process.env,
        GIT_AUTHOR_EMAIL: 'hermes@example.invalid',
        GIT_AUTHOR_NAME: 'Hermes Test',
        GIT_COMMITTER_EMAIL: 'hermes@example.invalid',
        GIT_COMMITTER_NAME: 'Hermes Test'
      }
    }).trim()

    execFileSync('git', ['--git-dir', remote, 'update-ref', 'refs/heads/team/fork-integration', commit])

    assert.equal(
      spawnSync('git', ['ls-remote', '--exit-code', '--heads', remote, 'fork-integration']).status,
      0,
      'the old suffix pattern matches team/fork-integration'
    )
    assert.equal(spawnSync('git', branchPublicationProbeArgs(remote, 'fork-integration')).status, 2)
    assert.equal(spawnSync('git', branchPublicationProbeArgs(remote, 'team/fork-integration')).status, 0)
  } finally {
    rmSync(root, { force: true, recursive: true })
  }
})

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
