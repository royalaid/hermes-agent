import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  FALLBACK_REPOSITORY,
  fromCI,
  fromFallback,
  fromLocalGit,
  isFallbackCommit,
  repositoryFromRemote,
  resolveStamp
} from './write-build-stamp.mjs'

test('fromCI reads commit, branch, and repository identity', () => {
  assert.deepEqual(
    fromCI({
      GITHUB_SHA: 'a'.repeat(40),
      GITHUB_REF_NAME: 'release',
      GITHUB_REPOSITORY: 'royalaid/hermes-agent'
    }),
    {
      commit: 'a'.repeat(40),
      branch: 'release',
      repository: 'royalaid/hermes-agent',
      dirty: false,
      source: 'ci'
    }
  )
  assert.equal(fromCI({}), null)
})

test('fromLocalGit returns null when git rev-parse fails', () => {
  const stamp = fromLocalGit('/tmp/not-a-repo', () => null)
  assert.equal(stamp, null)
})

test('fromLocalGit reads HEAD, branch, repository, and dirty status', () => {
  const calls = []
  const execFn = cmd => {
    calls.push(cmd)
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git status --porcelain -uno') return ' M apps/desktop/package.json'
    if (cmd === 'git rev-parse --abbrev-ref --symbolic-full-name @{upstream}') return 'fork/main'
    if (cmd === 'git remote get-url fork') return 'https://github.com/royalaid/hermes-agent.git'
    if (cmd === 'git remote get-url origin') return 'https://github.com/NousResearch/hermes-agent.git'
    return null
  }
  assert.deepEqual(fromLocalGit('/repo', execFn), {
    commit: 'b'.repeat(40),
    branch: 'main',
    repository: 'royalaid/hermes-agent',
    dirty: true,
    source: 'local'
  })
  assert.ok(calls.includes('git rev-parse HEAD'))
  assert.ok(calls.includes('git remote get-url fork'))
})

test('fromFallback uses canonical repository with the all-zero commit', () => {
  assert.deepEqual(fromFallback(), {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
  assert.equal(isFallbackCommit(FALLBACK_COMMIT), true)
  assert.equal(isFallbackCommit('a'.repeat(40)), false)
})

test('resolveStamp prefers CI over local git over fallback', () => {
  const ci = resolveStamp({
    env: {
      GITHUB_SHA: 'c'.repeat(40),
      GITHUB_REF_NAME: 'main',
      GITHUB_REPOSITORY: 'royalaid/hermes-agent'
    },
    execFn: () => 'should-not-run'
  })
  assert.equal(ci.source, 'ci')
  assert.equal(ci.commit, 'c'.repeat(40))
  assert.equal(ci.repository, 'royalaid/hermes-agent')
  assert.throws(
    () =>
      resolveStamp({
        env: {
          GITHUB_SHA: 'c'.repeat(40),
          GITHUB_REF_NAME: 'main',
          GITHUB_REPOSITORY: '../escape'
        },
        execFn: () => 'should-not-run'
      }),
    /Invalid GITHUB_REPOSITORY/
  )

  const local = resolveStamp({
    env: {},
    execFn: cmd => {
      if (cmd === 'git rev-parse HEAD') return 'd'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
      if (cmd === 'git status --porcelain -uno') return ''
      if (cmd === 'git rev-parse --abbrev-ref --symbolic-full-name @{upstream}') return 'fork/main'
      if (cmd === 'git remote get-url fork') return 'git@github.com:royalaid/hermes-agent.git'
      if (cmd === 'git remote get-url origin') return 'git@github.com:NousResearch/hermes-agent.git'
      return null
    }
  })
  assert.equal(local.source, 'local')
  assert.equal(local.commit, 'd'.repeat(40))
  assert.equal(local.repository, 'royalaid/hermes-agent')
  assert.equal(local.dirty, false)

  const overridden = resolveStamp({
    env: { HERMES_BUILD_PIN_REPOSITORY: 'integration/hermes-agent' },
    execFn: cmd => {
      if (cmd === 'git rev-parse HEAD') return 'e'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'integration'
      if (cmd === 'git status --porcelain -uno') return ''
      if (cmd === 'git remote get-url origin') return 'https://github.com/NousResearch/hermes-agent.git'
      return null
    }
  })
  assert.equal(overridden.repository, 'integration/hermes-agent')
  assert.throws(
    () => resolveStamp({ env: { HERMES_BUILD_PIN_REPOSITORY: '../escape' }, execFn: () => null }),
    /Invalid HERMES_BUILD_PIN_REPOSITORY/
  )
})

test('resolveStamp falls back when neither CI nor git is available', () => {
  const stamp = resolveStamp({ env: {}, execFn: () => null })
  assert.deepEqual(stamp, {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
})

test('repositoryFromRemote accepts GitHub HTTPS/SSH and rejects other remotes', () => {
  assert.equal(repositoryFromRemote('https://github.com/royalaid/hermes-agent.git'), 'royalaid/hermes-agent')
  assert.equal(repositoryFromRemote('git@github.com:NousResearch/hermes-agent.git'), 'NousResearch/hermes-agent')
  assert.equal(repositoryFromRemote('https://example.com/owner/repo.git'), null)
  assert.equal(repositoryFromRemote('https://evil.example/github.com/owner/repo.git'), null)
})
