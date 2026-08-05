import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildPinArgs,
  buildPosixPinArgs,
  cachedScriptPath,
  hasExistingGitCheckout,
  installedAgentInstallScript,
  installRefForStamp,
  isPinnedCommit,
  resolveInstallScript,
  resolveMarkerPinnedCommit,
  runBootstrap,
  shouldIncludeRepositoryPin
} from './bootstrap-runner'

const SCRIPT_NAME = process.platform === 'win32' ? 'install.ps1' : 'install.sh'
const ZERO_COMMIT = '0000000000000000000000000000000000000000'
const REPOSITORY = 'royalaid/hermes-agent'

function mkTmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bootstrap-test-'))
}

test('runBootstrap bails immediately when the signal is already aborted', async () => {
  const controller = new AbortController()
  controller.abort()

  const events = []

  const result = await runBootstrap({
    installStamp: null,
    activeRoot: '/tmp/hermes-runner-test',
    sourceRepoRoot: null,
    hermesHome: '/tmp/hermes-runner-test',
    logRoot: '/tmp/hermes-runner-test',
    onEvent: ev => events.push(ev),
    abortSignal: controller.signal
  })

  // Cancelled before any install script is spawned.
  assert.deepEqual(result, { ok: false, cancelled: true })
  assert.ok(
    events.some(ev => ev.type === 'failed' && /cancelled/i.test(ev.error)),
    'should emit a cancelled failure event'
  )
})

test('installedAgentInstallScript resolves the installer in the agent checkout', () => {
  const home = mkTmpHome()

  try {
    assert.equal(installedAgentInstallScript(home), null, 'absent before the checkout exists')

    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const scriptPath = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(scriptPath, '#!/bin/sh\necho hi\n')

    assert.equal(installedAgentInstallScript(home), scriptPath)
    assert.equal(installedAgentInstallScript(null), null, 'null home -> null')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('existing checkout detection requires git metadata', () => {
  const home = mkTmpHome()

  try {
    const activeRoot = path.join(home, 'hermes-agent')
    assert.equal(hasExistingGitCheckout(activeRoot), false)

    fs.mkdirSync(path.join(activeRoot, '.git'), { recursive: true })
    assert.equal(hasExistingGitCheckout(activeRoot), true)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('repository-scoped bootstrap cache hashes refs without collisions', () => {
  const home = mkTmpHome()
  assert.notEqual(
    cachedScriptPath(home, 'feature/a', 'owner/repo'),
    cachedScriptPath(home, 'feature_a', 'owner/repo')
  )
  assert.notEqual(
    cachedScriptPath(home, 'main', 'owner/repo-a'),
    cachedScriptPath(home, 'main', 'owner-a/repo')
  )
  assert.throws(() => cachedScriptPath(home, 'main', '../escape'), /Invalid install-stamp repository/)
})

test('fresh bootstrap args include repository and packaged commit pins', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main', repository: REPOSITORY }

  assert.deepEqual(buildPinArgs(installStamp), [
    '-Repository',
    REPOSITORY,
    '-Commit',
    installStamp.commit,
    '-Branch',
    'main'
  ])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/hermes-agent',
      hermesHome: '/tmp/hermes'
    }),
    [
      '--dir',
      '/tmp/hermes-agent',
      '--hermes-home',
      '/tmp/hermes',
      '--repository',
      REPOSITORY,
      '--branch',
      'main',
      '--commit',
      installStamp.commit
    ]
  )
})

test('existing-checkout bootstrap args keep repository and branch but skip packaged commit', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main', repository: REPOSITORY }

  assert.deepEqual(buildPinArgs(installStamp, { pinCommit: false }), [
    '-Repository',
    REPOSITORY,
    '-Branch',
    'main'
  ])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/hermes-agent',
      hermesHome: '/tmp/hermes',
      pinCommit: false
    }),
    [
      '--dir',
      '/tmp/hermes-agent',
      '--hermes-home',
      '/tmp/hermes',
      '--repository',
      REPOSITORY,
      '--branch',
      'main'
    ]
  )
})

test('fallback install stamps use repository with an unpinned branch ref', () => {
  const stamp = { commit: ZERO_COMMIT, branch: 'main', repository: REPOSITORY }

  assert.equal(isPinnedCommit(ZERO_COMMIT), false)
  assert.deepEqual(installRefForStamp(stamp), {
    ref: 'main',
    cacheKey: 'branch:main',
    pinned: false
  })
  // Must NOT pass -Commit / --commit for the all-zero placeholder.
  assert.deepEqual(buildPinArgs(stamp), ['-Repository', REPOSITORY, '-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp: stamp,
      activeRoot: '/tmp/hermes',
      hermesHome: '/tmp/home'
    }),
    [
      '--dir',
      '/tmp/hermes',
      '--hermes-home',
      '/tmp/home',
      '--repository',
      REPOSITORY,
      '--branch',
      'main'
    ]
  )
})

test('fallback install refs preserve slash-containing branch identity in the cache key', () => {
  const slash = installRefForStamp({
    commit: ZERO_COMMIT,
    branch: 'feature/a',
    repository: REPOSITORY
  })

  const underscore = installRefForStamp({
    commit: ZERO_COMMIT,
    branch: 'feature_a',
    repository: REPOSITORY
  })

  assert.notEqual(slash.cacheKey, underscore.cacheKey)
  assert.notEqual(
    cachedScriptPath('/tmp/hermes', slash.cacheKey, REPOSITORY),
    cachedScriptPath('/tmp/hermes', underscore.cacheKey, REPOSITORY)
  )
})

test('legacy installers omit the repository argument but retain branch and commit pins', () => {
  const stamp = { commit: 'a'.repeat(40), branch: 'main', repository: 'NousResearch/hermes-agent' }

  assert.deepEqual(buildPinArgs(stamp, { includeRepository: false }), [
    '-Commit',
    stamp.commit,
    '-Branch',
    'main'
  ])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp: stamp,
      activeRoot: '/tmp/hermes-agent',
      hermesHome: '/tmp/hermes',
      includeRepository: false
    }),
    ['--dir', '/tmp/hermes-agent', '--hermes-home', '/tmp/hermes', '--branch', 'main', '--commit', stamp.commit]
  )
})

test('legacy installer fallback verifies the existing checkout repository', () => {
  const canonicalStamp = {
    commit: 'a'.repeat(40),
    branch: 'main',
    repository: 'NousResearch/hermes-agent'
  }

  const legacyScript = { supportsRepositoryPin: false }

  assert.equal(
    shouldIncludeRepositoryPin(legacyScript, canonicalStamp, '/tmp/checkout', {
      execGit: () => 'https://github.com/NousResearch/hermes-agent.git'
    }),
    false
  )
  assert.throws(
    () =>
      shouldIncludeRepositoryPin(legacyScript, canonicalStamp, '/tmp/checkout', {
        execGit: () => 'https://github.com/royalaid/hermes-agent.git'
      }),
    /checkout repository royalaid\/hermes-agent does not match/
  )
  assert.throws(
    () =>
      shouldIncludeRepositoryPin(legacyScript, canonicalStamp, '/tmp/checkout', {
        execGit: () => 'https://evil.example/github.com/NousResearch/hermes-agent.git'
      }),
    /repository could not be verified/
  )
  assert.throws(
    () =>
      shouldIncludeRepositoryPin(
        legacyScript,
        { ...canonicalStamp, repository: REPOSITORY },
        '/tmp/checkout',
        { execGit: () => 'https://github.com/NousResearch/hermes-agent.git' }
      ),
    /does not support repository pinning/
  )
})

test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {
  const realHead = 'c'.repeat(40)
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    realHead
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: 'd'.repeat(40), branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    'd'.repeat(40),
    'packaged real pin wins over checkout HEAD'
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/missing', {
      resolveHead: () => null
    }),
    null
  )
})

test('resolveInstallScript downloads fallback stamps by branch instead of zero commit', async () => {
  const home = mkTmpHome()

  try {
    const logs = []
    const refs = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main', repository: REPOSITORY },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      _download: async (repository, ref, destPath) => {
        refs.push([repository, ref])
        fs.mkdirSync(path.dirname(destPath), { recursive: true })
        fs.writeFileSync(destPath, '#!/bin/sh\necho fallback branch\n')

        return destPath
      }
    })

    assert.deepEqual(refs, [[REPOSITORY, 'main']])
    assert.equal(result.source, 'download')
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'branch:main', REPOSITORY))
    assert.ok(
      logs.some(ev => /fallback, unpinned/.test(ev.line || '')),
      'emits an unpinned fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript prefers a cached script without touching the network', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    fs.writeFileSync(cached, '#!/bin/sh\necho cached\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev)
    })

    assert.equal(result.source, 'cache')
    assert.equal(result.path, cached)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript falls back to the installed agent checkout on a 404', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // Seed the installed agent checkout so the fallback has something to resolve.
    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const installed = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(installed, '#!/bin/sh\necho fallback\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      // Simulate GitHub returning a 404 for the pinned commit.
      _download: async () => {
        throw new Error('Failed to download install.sh: HTTP 404')
      }
    })

    assert.equal(result.source, 'installed-agent')
    assert.equal(result.supportsRepositoryPin, false)
    assert.equal(result.path, installed)
    assert.equal(
      fs.existsSync(cachedScriptPath(home, commit)),
      false,
      'legacy fallback must not poison the immutable commit cache'
    )
    assert.ok(
      logs.some(ev => /falling back to installed agent/.test(ev.line || '')),
      'emits a fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript rethrows when the 404 fallback is unavailable', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // No installed agent checkout seeded -> nothing to fall back to.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          throw new Error('Failed to download install.sh: HTTP 404')
        }
      }),
      /HTTP 404|Failed to download/
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
