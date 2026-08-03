import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  DEFAULT_REPOSITORY,
  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  fromCI,
  fromFallback,
  fromLocalGit,
  isFallbackCommit,
  resolveStamp
} from './write-build-stamp.mjs'

test('fromCI reads GITHUB_SHA / GITHUB_REF_NAME', () => {
  assert.deepEqual(
    fromCI({ GITHUB_SHA: 'a'.repeat(40), GITHUB_REF_NAME: 'release' }),
    {
      commit: 'a'.repeat(40),
      branch: 'release',
      repository: DEFAULT_REPOSITORY,
      dirty: false,
      source: 'ci'
    }
  )
  assert.equal(fromCI({}), null)
})

test('fromCI carries a validated GitHub repository into the install stamp', () => {
  assert.equal(
    fromCI({
      GITHUB_SHA: 'a'.repeat(40),
      GITHUB_REF_NAME: 'local/openai-native-windows',
      GITHUB_REPOSITORY: 'royalaid/hermes-agent'
    }).repository,
    'royalaid/hermes-agent'
  )
  assert.equal(
    fromCI({ GITHUB_SHA: 'a'.repeat(40), GITHUB_REPOSITORY: '../invalid' }).repository,
    DEFAULT_REPOSITORY
  )
})

test('fromLocalGit returns null when git rev-parse fails', () => {
  const stamp = fromLocalGit('/tmp/not-a-repo', () => null)
  assert.equal(stamp, null)
})

test('fromLocalGit reads HEAD + branch + dirty status', () => {
  const calls = []
  const execFn = (cmd) => {
    calls.push(cmd)
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git status --porcelain -uno') return ' M apps/desktop/package.json'
    return null
  }
  assert.deepEqual(fromLocalGit('/repo', execFn), {
    commit: 'b'.repeat(40),
    branch: 'main',
    repository: DEFAULT_REPOSITORY,
    dirty: true,
    source: 'local'
  })
  assert.ok(calls.includes('git rev-parse HEAD'))
})

test('fromLocalGit derives repository from the branch upstream remote', () => {
  const execFn = cmd => {
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'local/openai-native-windows'
    if (cmd === 'git status --porcelain -uno') return ''
    if (cmd === 'git rev-parse --abbrev-ref --symbolic-full-name @{upstream}') {
      return 'fork/local/openai-native-windows'
    }
    if (cmd === 'git remote get-url --push fork') return 'https://github.com/royalaid/hermes-agent.git'
    return null
  }

  assert.equal(fromLocalGit('/repo', execFn).repository, 'royalaid/hermes-agent')
})

test('fromFallback uses the all-zero placeholder commit', () => {
  assert.deepEqual(fromFallback(), {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: DEFAULT_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
  assert.equal(isFallbackCommit(FALLBACK_COMMIT), true)
  assert.equal(isFallbackCommit('a'.repeat(40)), false)
})

test('resolveStamp prefers CI over local git over fallback', () => {
  const ci = resolveStamp({
    env: { GITHUB_SHA: 'c'.repeat(40), GITHUB_REF_NAME: 'main' },
    execFn: () => 'should-not-run'
  })
  assert.equal(ci.source, 'ci')
  assert.equal(ci.commit, 'c'.repeat(40))

  const local = resolveStamp({
    env: {},
    execFn: (cmd) => {
      if (cmd === 'git rev-parse HEAD') return 'd'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
      if (cmd === 'git status --porcelain -uno') return ''
      return null
    }
  })
  assert.equal(local.source, 'local')
  assert.equal(local.commit, 'd'.repeat(40))
  assert.equal(local.dirty, false)
})

test('resolveStamp falls back when neither CI nor git is available', () => {
  const stamp = resolveStamp({ env: {}, execFn: () => null })
  assert.deepEqual(stamp, {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: DEFAULT_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
})
