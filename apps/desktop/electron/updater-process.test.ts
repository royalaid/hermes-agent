import assert from 'node:assert/strict'
import { execFileSync, spawn, type SpawnOptions } from 'node:child_process'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'

import { test, vi } from 'vitest'

import {
  captureSpawnedUpdaterCreatedAt,
  collectRelaunchArgs,
  formatPowerShellArgvForDisplay,
  isSpawnedUpdaterGenerationActive,
  launchWindowsUpdateTransport,
  observeUpdaterGeneration,
  observeUpdaterHandoff,
  resolvePosixScriptHandoff,
  resolveStagedUpdaterBinary,
  resolveUpdateScriptHandoff,
  resolveWindowsDevRelaunchAppPath,
  resolveWindowsUpdateTransport,
  sandboxFallbackFromEnv,
  spawnUpdaterProcess,
  STAGED_UPDATER_BRIDGE_LEASE_ENV,
  stagedUpdaterEnvironment,
  terminateSpawnedUpdaterIfExact,
  waitForWindowsHandoffOwnerIdentity,
  wrapHandoffForDetachedConsole
} from './updater-process'
import { queryWindowsProcessCreationFileTime } from './windows-process-identity'

async function waitForFile(filePath: string, timeoutMs = 10_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs

  while (!fs.existsSync(filePath) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 25))
  }

  return fs.existsSync(filePath)
}

function windowsPowerShellExecutable(): string {
  return path.join(
    process.env.SystemRoot || String.raw`C:\Windows`,
    'System32',
    'WindowsPowerShell',
    'v1.0',
    'powershell.exe'
  )
}

function hasVisibleTopLevelWindow(pid: number): boolean {
  const probe = String.raw`
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class HermesWindowProbe {
  private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr param);
  [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr param);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  public static bool HasVisibleWindow(uint target) {
    bool found = false;
    EnumWindows((hwnd, param) => {
      uint pid;
      GetWindowThreadProcessId(hwnd, out pid);
      if (pid == target && IsWindowVisible(hwnd)) { found = true; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
'@
if ([HermesWindowProbe]::HasVisibleWindow([uint32]$env:HERMES_WINDOW_PROBE_PID)) { 'visible' } else { 'hidden' }
`.trim()

  const encoded = Buffer.from(probe, 'utf16le').toString('base64')

  const result = execFileSync(
    windowsPowerShellExecutable(),
    ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
    {
      encoding: 'utf8',
      env: { ...process.env, HERMES_WINDOW_PROBE_PID: String(pid) },
      stdio: ['ignore', 'pipe', 'ignore'],
      windowsHide: true
    }
  ).trim()

  return result === 'visible'
}

test('resolveStagedUpdaterBinary returns the staged updater on Windows without guessing its generation', () => {
  assert.equal(
    resolveStagedUpdaterBinary('C:\\Hermes', {
      fileExists: () => true,
      isWindows: true
    }),
    path.join('C:\\Hermes', 'hermes-setup.exe')
  )
})

test('spawnUpdaterProcess hides the updater console and detaches the child on Windows', () => {
  const calls: Array<{ args: string[]; command: string; options: SpawnOptions }> = []
  let unrefCalls = 0

  const child = {
    kill: () => true,
    pid: 4242,
    unref: () => {
      unrefCalls += 1
    }
  }

  const result = spawnUpdaterProcess(
    'hermes-setup.exe',
    ['--update', '--branch', 'main'],
    { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        calls.push({ args, command, options })

        return child
      }
    }
  )

  assert.equal(result, child)
  assert.equal(unrefCalls, 1)
  assert.deepEqual(calls, [
    {
      args: ['--update', '--branch', 'main'],
      command: 'hermes-setup.exe',
      options: { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore', windowsHide: true }
    }
  ])
})

test('spawnUpdaterProcess preserves updater options off Windows', () => {
  let capturedOptions: SpawnOptions | undefined

  spawnUpdaterProcess(
    'hermes-setup',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    {
      isWindows: false,
      spawnProcess: (_command, _args, options) => {
        capturedOptions = options

        return { kill: () => true, unref: () => {} }
      }
    }
  )

  assert.deepEqual(capturedOptions, { detached: true, stdio: 'ignore' })
})

test('staged bridge capability travels only through the private child environment', () => {
  const token = 'unguessable-lease-id'

  const base = {
    PATH: 'C:\\Windows',
    Hermes_Desktop_Update_Test: '1',
    hermes_deferred_gateway_startup_gate: 'C:\\stale\\startup.gate',
    hermes_update_bridge_lease_id: 'stale-bridge-lease-id'
  }

  const env = stagedUpdaterEnvironment(base, token)

  assert.deepEqual(base, {
    PATH: 'C:\\Windows',
    Hermes_Desktop_Update_Test: '1',
    hermes_deferred_gateway_startup_gate: 'C:\\stale\\startup.gate',
    hermes_update_bridge_lease_id: 'stale-bridge-lease-id'
  })
  assert.equal(env[STAGED_UPDATER_BRIDGE_LEASE_ENV], token)
  assert.deepEqual(env, { PATH: 'C:\\Windows', [STAGED_UPDATER_BRIDGE_LEASE_ENV]: token })
  assert.equal(Object.values(base).includes(token), false)
})

test('captures and terminates only the exact spawned updater PID generation', async () => {
  const terminated: number[] = []
  const query = async () => 1_723_330_000

  const child = {
    pid: 42,
    unref: () => {},
    kill: () => {
      terminated.push(42)

      return true
    }
  }

  assert.equal(await captureSpawnedUpdaterCreatedAt(42, { queryCreatedAt: query }), 1_723_330_000)
  assert.equal(await terminateSpawnedUpdaterIfExact(child, 1_723_330_000, { queryCreatedAt: query }), true)
  assert.deepEqual(terminated, [42])
})

test('probes liveness through the retained updater handle', () => {
  const signals: Array<NodeJS.Signals | number | undefined> = []

  const child = {
    pid: 42,
    unref: () => {},
    kill: (signal?: NodeJS.Signals | number) => {
      signals.push(signal)

      return true
    }
  }

  assert.equal(isSpawnedUpdaterGenerationActive(child), true)
  assert.deepEqual(signals, [0])
})

test('treats a false or throwing retained-handle liveness probe as inactive', () => {
  assert.equal(isSpawnedUpdaterGenerationActive({ pid: 42, unref: () => {}, kill: () => false }), false)
  assert.equal(
    isSpawnedUpdaterGenerationActive({
      pid: 42,
      unref: () => {},
      kill: () => {
        throw new Error('handle closed')
      }
    }),
    false
  )
})

test('never terminates after PID reuse or an unknown creation-time probe', async () => {
  for (const observed of [1_723_330_001, null]) {
    const kill = vi.fn(() => true)
    assert.equal(
      await terminateSpawnedUpdaterIfExact({ pid: 42, unref: () => {}, kill }, 1_723_330_000, {
        queryCreatedAt: async () => observed
      }),
      false
    )
    assert.equal(kill.mock.calls.length, 0)
  }
})

test('terminates through the retained child handle when the PID is reused after identity proof', async () => {
  const expectedCreatedAt = 1_723_330_000
  let currentGeneration = 'spawned-updater'
  const reopenedGenerations: string[] = []
  const exactHandleKills: string[] = []

  const child = {
    pid: 42,
    unref: () => {},
    kill: () => {
      exactHandleKills.push('spawned-updater')

      return true
    }
  }

  const reopenPid = vi.spyOn(process, 'kill').mockImplementation(() => {
    reopenedGenerations.push(currentGeneration)

    return true
  })

  try {
    assert.equal(
      await terminateSpawnedUpdaterIfExact(child, expectedCreatedAt, {
        queryCreatedAt: async () => {
          const observedCreatedAt = expectedCreatedAt

          // Model the updater exiting and its numeric PID being reused after
          // the creation-time proof, but before termination is attempted.
          currentGeneration = 'replacement-process'

          return observedCreatedAt
        }
      }),
      true
    )
    assert.deepEqual(exactHandleKills, ['spawned-updater'])
    assert.deepEqual(reopenedGenerations, [])
  } finally {
    reopenPid.mockRestore()
  }
})

test('resolveStagedUpdaterBinary hands Windows the staged installer it finds', () => {
  const home = 'C:\\Users\\hermes\\AppData\\Local\\hermes'
  const staged = path.join(home, 'hermes-setup.exe')
  const probed: string[] = []

  const resolved = resolveStagedUpdaterBinary(home, {
    fileExists: candidate => {
      probed.push(candidate)

      return candidate === staged
    },
    isWindows: true
  })

  assert.equal(resolved, staged)
  assert.deepEqual(probed, [staged])
})

test('resolveStagedUpdaterBinary returns null off Windows even when hermes-setup is staged (#74836)', () => {
  const home = '/Users/hermes/.hermes'
  let probes = 0

  const resolved = resolveStagedUpdaterBinary(home, {
    // The installer stages hermes-setup on macOS/Linux too, so "it exists" is
    // the normal case — and precisely the one that must not win.
    fileExists: () => {
      probes += 1

      return true
    },
    isWindows: false
  })

  assert.equal(resolved, null)
  assert.equal(probes, 0)
})

test('resolveStagedUpdaterBinary returns null on Windows when nothing is staged', () => {
  const resolved = resolveStagedUpdaterBinary('C:\\Users\\hermes\\AppData\\Local\\hermes', {
    fileExists: () => false,
    isWindows: true
  })

  assert.equal(resolved, null)
})

test('resolveUpdateScriptHandoff chooses the hardened flat script when both implementations exist', () => {
  const root = String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`
  const expected = path.join(root, 'scripts', 'desktop-update.ps1')

  const handoff = resolveUpdateScriptHandoff(root, {
    isWindows: true,
    fileExists: () => true
  })

  assert.ok(handoff)
  assert.equal(handoff.command, 'powershell')
  assert.equal(handoff.scriptPath, expected)
  assert.deepEqual(handoff.args, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', expected])
})

test('resolveUpdateScriptHandoff selects the hardened flat script by itself', () => {
  const root = String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`
  const legacy = path.join(root, 'scripts', 'desktop-update.ps1')

  const handoff = resolveUpdateScriptHandoff(root, {
    isWindows: true,
    fileExists: candidate => candidate === legacy
  })

  assert.ok(handoff)
  assert.equal(handoff.scriptPath, legacy)
})

test('resolveUpdateScriptHandoff fails closed when only the incompatible nested script exists', () => {
  const root = String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`
  const nested = path.join(root, 'scripts', 'desktop-update', 'windows.ps1')

  const handoff = resolveUpdateScriptHandoff(root, {
    isWindows: true,
    fileExists: candidate => candidate === nested
  })

  assert.equal(handoff, null)
})

test('resolveUpdateScriptHandoff returns null when the checkout predates the script', () => {
  const handoff = resolveUpdateScriptHandoff(String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`, {
    isWindows: true,
    fileExists: () => false
  })

  assert.equal(handoff, null)
})

test('resolveUpdateScriptHandoff is Windows-only (POSIX has a separate detached handoff)', () => {
  const handoff = resolveUpdateScriptHandoff('/home/hermes/.hermes/hermes-agent', {
    isWindows: false,
    fileExists: () => true
  })

  assert.equal(handoff, null)
})

test('Windows flat handoff keeps branch, paths, and bridge capability out of native arguments', () => {
  const root = String.raw`C:\Hermes & 100%\agent`
  const expected = path.join(root, 'scripts', 'desktop-update.ps1')
  const branch = 'fork/&|%integration'
  const bridgeLeaseId = 'unguessable-bridge-lease-id'
  const relaunchAppPath = String.raw`C:\Hermes & 100%\agent\apps\desktop`
  const relaunchExe = String.raw`C:\Program Files\Hermes & 100%\Hermes.exe`

  const transport = resolveWindowsUpdateTransport(root, {
    isWindows: true,
    fileExists: candidate => candidate === expected
  })

  assert.equal(transport.kind, 'script')

  if (transport.kind !== 'script') {
    assert.fail('expected the hardened flat script transport')
  }

  const wrapped = wrapHandoffForDetachedConsole(transport.handoff, {
    bridgeLeaseId,
    branch,
    desktopPid: 4242,
    installRoot: root,
    relaunchAppPath,
    relaunchExe
  })

  assert.match(wrapped.command, /powershell(?:\.exe)?$/i)
  assert.deepEqual(wrapped.args.slice(0, -1), [
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-EncodedCommand'
  ])
  assert.match(wrapped.args.at(-1) ?? '', /^[A-Za-z0-9+/=]+$/)
  const launcher = Buffer.from(wrapped.args.at(-1) ?? '', 'base64').toString('utf16le')

  assert.match(launcher, /Start-Process -FilePath \$powerShell/)
  assert.match(launcher, /-WindowStyle Hidden -PassThru/)
  assert.match(launcher, /HERMES_UPDATE_HANDOFF_PID=.*FILETIME=/)
  const encodedRunner = launcher.match(/'([A-Za-z0-9+/=]{100,})'/)?.[1]

  assert.ok(encodedRunner)
  const runner = Buffer.from(encodedRunner, 'base64').toString('utf16le')

  assert.match(runner, /Test-Path -LiteralPath \$scriptPath -PathType Leaf/)
  assert.match(runner, /& \$scriptPath @scriptArgs/)
  assert.match(runner, /HERMES_UPDATE_BRIDGE_LEASE_ID -notmatch/)
  assert.deepEqual(wrapped.env, {
    HERMES_UPDATE_BRIDGE_LEASE_ID: bridgeLeaseId,
    HERMES_UPDATE_HANDOFF_BRANCH: branch,
    HERMES_UPDATE_HANDOFF_DESKTOP_PID: '4242',
    HERMES_UPDATE_HANDOFF_INSTALL_ROOT: root,
    HERMES_UPDATE_HANDOFF_RELAUNCH_APP_PATH: relaunchAppPath,
    HERMES_UPDATE_HANDOFF_RELAUNCH_EXE: relaunchExe,
    HERMES_UPDATE_HANDOFF_SCRIPT: expected
  })

  const nativeArgv = wrapped.args.join(' ')

  for (const value of [expected, root, branch, bridgeLeaseId, relaunchAppPath, relaunchExe]) {
    assert.equal(nativeArgv.includes(value), false, `${value} must not reach native arguments`)
  }

  const alternate = wrapHandoffForDetachedConsole(transport.handoff, {
    bridgeLeaseId: 'another-valid-lease-id',
    branch: 'another/&|%branch',
    desktopPid: 9001,
    installRoot: String.raw`C:\Another & 100%\agent`,
    relaunchExe: String.raw`C:\Another & 100%\Hermes.exe`
  })

  assert.deepEqual(alternate.args, wrapped.args)
})

test('Windows development relaunch preserves the Electron app entry only for default-app launches', () => {
  const electron = String.raw`C:\repo\apps\desktop\node_modules\electron\dist\electron.exe`
  const appEntry = String.raw`C:\repo\apps\desktop`

  assert.equal(resolveWindowsDevRelaunchAppPath(true, [electron, appEntry]), appEntry)
  assert.equal(resolveWindowsDevRelaunchAppPath(false, [electron, appEntry]), undefined)
  assert.equal(resolveWindowsDevRelaunchAppPath(true, [electron, '--inspect']), undefined)
})

test('Windows flat handoff rejects invalid private payload values before spawn', () => {
  const root = String.raw`C:\Hermes\agent`
  const flat = path.join(root, 'scripts', 'desktop-update.ps1')

  const transport = resolveWindowsUpdateTransport(root, {
    isWindows: true,
    fileExists: candidate => candidate === flat
  })

  assert.equal(transport.kind, 'script')

  if (transport.kind !== 'script') {
    assert.fail('expected the hardened flat script transport')
  }

  const valid = {
    bridgeLeaseId: 'valid-bridge-lease-id',
    branch: 'fork/integration',
    desktopPid: 4242,
    installRoot: root,
    relaunchExe: String.raw`C:\Program Files\Hermes\Hermes.exe`
  }

  assert.throws(
    () => wrapHandoffForDetachedConsole(transport.handoff, { ...valid, bridgeLeaseId: 'too-short' }),
    /valid bridge lease ID/
  )
  assert.throws(
    () => wrapHandoffForDetachedConsole(transport.handoff, { ...valid, desktopPid: 0 }),
    /positive desktop PID/
  )
  assert.throws(
    () => wrapHandoffForDetachedConsole(transport.handoff, { ...valid, branch: '' }),
    /every environment value/
  )
})

test('ordinary Windows updates refuse staged and nested fallbacks when the flat script is absent', () => {
  const root = String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`
  const nested = path.join(root, 'scripts', 'desktop-update', 'windows.ps1')
  const staged = String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-setup.exe`

  const transport = resolveWindowsUpdateTransport(root, {
    isWindows: true,
    fileExists: candidate => candidate === nested || candidate === staged
  })

  const spawnProcess = vi.fn(() => ({ kill: () => true, unref: () => {} }))

  const launch = launchWindowsUpdateTransport(
    transport,
    {
      bridgeLeaseId: 'unguessable-bridge-lease-id',
      branch: 'fork/&|%integration',
      desktopPid: 4242,
      installRoot: root,
      relaunchExe: String.raw`C:\Program Files\Hermes\Hermes.exe`
    },
    { env: { PATH: String.raw`C:\Windows` } },
    { isWindows: true, spawnProcess }
  )

  assert.deepEqual(transport, { kind: 'manual' })
  assert.deepEqual(launch, { kind: 'manual' })
  assert.equal(spawnProcess.mock.calls.length, 0)
})

test('production Windows transport composes flat script, non-main branch, and BridgeLeaseId at spawn', () => {
  const root = String.raw`C:\Hermes & 100%\agent`
  const flat = path.join(root, 'scripts', 'desktop-update.ps1')
  const bridgeLeaseId = 'unguessable-bridge-lease-id'
  const branch = 'fork/&|%integration'
  let captured: { args: string[]; command: string; options: SpawnOptions } | undefined

  const transport = resolveWindowsUpdateTransport(root, {
    isWindows: true,
    fileExists: candidate => candidate === flat
  })

  const launch = launchWindowsUpdateTransport(
    transport,
    {
      bridgeLeaseId,
      branch,
      desktopPid: 4242,
      installRoot: root,
      relaunchExe: String.raw`C:\Program Files\Hermes & 100%\Hermes.exe`
    },
    { cwd: root, env: { PATH: String.raw`C:\Windows` }, detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        captured = { args, command, options }

        return { pid: 55, kill: () => true, unref: () => {} }
      }
    }
  )

  assert.equal(launch.kind, 'spawned')
  assert.ok(captured)
  assert.match(captured.command, /powershell(?:\.exe)?$/i)
  assert.equal(captured.options.detached, false)
  assert.equal(captured.options.windowsHide, true)
  assert.deepEqual(captured.options.stdio, ['ignore', 'pipe', 'ignore'])
  assert.equal(captured.options.env?.PATH, String.raw`C:\Windows`)
  assert.equal(captured.options.env?.HERMES_UPDATE_HANDOFF_SCRIPT, flat)
  assert.equal(captured.options.env?.HERMES_UPDATE_HANDOFF_BRANCH, branch)
  assert.equal(captured.options.env?.HERMES_UPDATE_BRIDGE_LEASE_ID, bridgeLeaseId)
  assert.equal(captured.args.join(' ').includes(branch), false)
  assert.equal(captured.args.join(' ').includes(bridgeLeaseId), false)
})

test('production Windows transport strips inherited fault controls case-insensitively at spawn', () => {
  const root = String.raw`C:\Hermes\agent`
  const flat = path.join(root, 'scripts', 'desktop-update.ps1')
  let capturedEnv: NodeJS.ProcessEnv | undefined

  const transport = resolveWindowsUpdateTransport(root, {
    isWindows: true,
    fileExists: candidate => candidate === flat
  })

  const launch = launchWindowsUpdateTransport(
    transport,
    {
      bridgeLeaseId: 'unguessable-bridge-lease-id',
      branch: 'fork/integration',
      desktopPid: 4242,
      installRoot: root,
      relaunchExe: String.raw`C:\Program Files\Hermes\Hermes.exe`
    },
    {
      env: {
        PATH: String.raw`C:\Windows`,
        HERMES_HOME: String.raw`C:\Hermes`,
        hermes_desktop_update_test: '1',
        Hermes_Internal_Update_Stage_Timeout_Seconds: '1',
        hermes_selftest_fail: '1',
        Hermes_Test_Result_Publish_Fail: '1',
        hermes_force_release_force_snapshot_failure: '1',
        Hermes_Terminate_Watcher_Log: String.raw`C:\sensitive\watcher.log`,
        hermes_deferred_gateway_startup_gate: String.raw`C:\stale\startup.gate`,
        hermes_update_handoff_branch: 'stale/branch',
        hermes_update_bridge_lease_id: 'stale-bridge-lease-id'
      }
    },
    {
      isWindows: true,
      spawnProcess: (_command, _args, options) => {
        capturedEnv = options.env

        return { pid: 55, kill: () => true, unref: () => {} }
      }
    }
  )

  assert.equal(launch.kind, 'spawned')
  assert.ok(capturedEnv)
  assert.equal(capturedEnv.PATH, String.raw`C:\Windows`)
  assert.equal(capturedEnv.HERMES_HOME, String.raw`C:\Hermes`)
  assert.equal(capturedEnv.HERMES_UPDATE_HANDOFF_SCRIPT, flat)
  assert.equal(capturedEnv.HERMES_UPDATE_HANDOFF_BRANCH, 'fork/integration')
  assert.equal(capturedEnv.HERMES_UPDATE_BRIDGE_LEASE_ID, 'unguessable-bridge-lease-id')
  assert.equal(Object.keys(capturedEnv).filter(name => name.toUpperCase() === 'HERMES_UPDATE_HANDOFF_BRANCH').length, 1)
  assert.equal(
    Object.keys(capturedEnv).filter(name => name.toUpperCase() === 'HERMES_UPDATE_BRIDGE_LEASE_ID').length,
    1
  )

  for (const name of Object.keys(capturedEnv)) {
    assert.doesNotMatch(
      name,
      /^(?:HERMES_DESKTOP_UPDATE_TEST|HERMES_INTERNAL_UPDATE_STAGE_TIMEOUT_SECONDS|HERMES_SELFTEST_|HERMES_TEST_|HERMES_FORCE_RELEASE_(?:FORCE_|TEST_)|HERMES_TERMINATE_|HERMES_DEFERRED_GATEWAY_STARTUP_GATE)/i
    )
  }
})

test('Windows handoff receipt resolves only an independently verified PID and exact FILETIME', async () => {
  const stdout = new Readable({ read() {} })
  const events = new EventEmitter()

  const identityPromise = waitForWindowsHandoffOwnerIdentity(
    {
      pid: 10,
      kill: () => true,
      stdout,
      unref: () => {},
      once: events.once.bind(events),
      removeListener: events.removeListener.bind(events)
    },
    {
      queryCreatedAt: async pid => (pid === 2222 ? 1_777_777_777 : null),
      queryCreationFileTime: async pid => (pid === 2222 ? '133456736000000123' : null)
    }
  )

  stdout.push('HERMES_UPDATE_HANDOFF_PID=2222;FILETIME=133456736000000123\n')

  assert.deepEqual(await identityPromise, {
    pid: 2222,
    createdAt: 1_777_777_777,
    creationFileTime: '133456736000000123'
  })
})

test('Windows handoff receipt fails closed immediately when the launcher exits before publishing', async () => {
  const stdout = new Readable({ read() {} })
  const events = new EventEmitter()

  const identityPromise = waitForWindowsHandoffOwnerIdentity(
    {
      pid: 10,
      kill: () => true,
      stdout,
      unref: () => {},
      once: events.once.bind(events),
      removeListener: events.removeListener.bind(events)
    },
    { timeoutMs: 10_000 }
  )

  events.emit('exit', 22, null)

  assert.equal(await identityPromise, null)
})

test('Windows handoff receipt fails closed immediately on an asynchronous spawn error', async () => {
  const stdout = new Readable({ read() {} })
  const events = new EventEmitter()

  const identityPromise = waitForWindowsHandoffOwnerIdentity(
    {
      pid: 10,
      kill: () => true,
      stdout,
      unref: () => {},
      once: events.once.bind(events),
      removeListener: events.removeListener.bind(events)
    },
    { timeoutMs: 10_000 }
  )

  events.emit('error', Object.assign(new Error('access denied'), { code: 'EACCES' }))

  assert.equal(await identityPromise, null)
})

test('Windows handoff receipt rejects a writer whose live FILETIME no longer matches', async () => {
  const stdout = new Readable({ read() {} })

  const identityPromise = waitForWindowsHandoffOwnerIdentity(
    { pid: 10, kill: () => true, stdout, unref: () => {} },
    {
      queryCreatedAt: async () => 1_777_777_777,
      queryCreationFileTime: async () => '133456736000000124'
    }
  )

  stdout.push('HERMES_UPDATE_HANDOFF_PID=2222;FILETIME=133456736000000123\n')

  assert.equal(await identityPromise, null)
})

test('observeUpdaterGeneration requires the exact writer generation after the settle interval', async () => {
  const waits: number[] = []

  assert.equal(
    await observeUpdaterGeneration(55, '133456736000000123', 2500, {
      queryCreationFileTime: async pid => (pid === 55 ? '133456736000000123' : null),
      wait: async delayMs => {
        waits.push(delayMs)
      }
    }),
    true
  )
  assert.deepEqual(waits, [2500])

  assert.equal(
    await observeUpdaterGeneration(55, '133456736000000123', 0, {
      queryCreationFileTime: async () => '133456736000000124'
    }),
    false
  )
})

test.runIf(process.platform === 'win32')(
  'production Windows transport binds named PowerShell script parameters in a real child',
  async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-windows-handoff-'))
    const scriptPath = path.join(directory, 'probe.ps1')
    const sentinel = path.join(directory, 'result.json')
    const branch = 'fork/&|%integration'
    const installRoot = String.raw`C:\Hermes & 100%\agent`
    const relaunchAppPath = String.raw`C:\Hermes & 100%\agent\apps\desktop`
    const relaunchExe = String.raw`C:\Hermes & 100%\Hermes.exe`
    const bridgeLeaseId = 'unguessable-bridge-lease-id'

    fs.writeFileSync(
      scriptPath,
      String.raw`param(
  [Parameter(Mandatory = $true)][string]$InstallRoot,
  [Parameter(Mandatory = $true)][string]$Branch,
  [Parameter(Mandatory = $true)][int]$DesktopPid,
  [string]$RelaunchAppPath,
  [Parameter(Mandatory = $true)][string]$RelaunchExe,
  [Parameter(Mandatory = $true)][string]$BridgeLeaseId
)
$payload = [ordered]@{
  branch = $Branch
  desktop_pid = $DesktopPid
  install_root = $InstallRoot
  lease_id = $BridgeLeaseId
  relaunch_app_path = $RelaunchAppPath
  relaunch_exe = $RelaunchExe
}
[IO.File]::WriteAllText(
  $env:HERMES_TRANSPORT_TEST_SENTINEL,
  ($payload | ConvertTo-Json -Compress),
  [Text.UTF8Encoding]::new($false)
)
exit 0
`,
      'utf8'
    )

    try {
      const launch = launchWindowsUpdateTransport(
        {
          kind: 'script',
          handoff: {
            command: 'powershell',
            args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath],
            scriptPath
          }
        },
        { bridgeLeaseId, branch, desktopPid: 4242, installRoot, relaunchAppPath, relaunchExe },
        {
          cwd: os.tmpdir(),
          detached: true,
          env: { ...process.env, HERMES_TRANSPORT_TEST_SENTINEL: sentinel },
          stdio: 'ignore'
        },
        { isWindows: true }
      )

      assert.equal(launch.kind, 'spawned')

      const deadline = Date.now() + 10_000

      while (!fs.existsSync(sentinel) && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 50))
      }

      assert.equal(fs.existsSync(sentinel), true, 'detached PowerShell script did not run')
      assert.deepEqual(JSON.parse(fs.readFileSync(sentinel, 'utf8')), {
        branch,
        desktop_pid: 4242,
        install_root: installRoot,
        lease_id: bridgeLeaseId,
        relaunch_app_path: relaunchAppPath,
        relaunch_exe: relaunchExe
      })
    } finally {
      fs.rmSync(directory, { force: true, maxRetries: 5, recursive: true, retryDelay: 50 })
    }
  },
  15_000
)

test.runIf(process.platform === 'win32')(
  'real Windows handoff publishes the hidden writer PID and exact FILETIME with no top-level window',
  async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-hidden-handoff-'))
    const scriptPath = path.join(directory, 'probe.ps1')
    const ready = path.join(directory, 'ready.json')
    const sentinel = path.join(directory, 'finished.txt')
    const branch = 'fork/&|%integration'
    const bridgeLeaseId = 'unguessable-bridge-lease-id'
    let writerPid: number | null = null

    fs.writeFileSync(
      scriptPath,
      String.raw`param(
  [Parameter(Mandatory = $true)][string]$InstallRoot,
  [Parameter(Mandatory = $true)][string]$Branch,
  [Parameter(Mandatory = $true)][int]$DesktopPid,
  [string]$RelaunchAppPath,
  [Parameter(Mandatory = $true)][string]$RelaunchExe,
  [Parameter(Mandatory = $true)][string]$BridgeLeaseId
)
try { $Host.UI.RawUI.WindowTitle = $env:WINDOW_PROBE_TITLE } catch {}
[IO.File]::WriteAllText(
  $env:WINDOW_PROBE_READY,
  ([ordered]@{
    pid = $PID
    branch = $Branch
    lease = $BridgeLeaseId
    toxic = -not [string]::IsNullOrEmpty($env:HERMES_DESKTOP_UPDATE_TEST) -or
      -not [string]::IsNullOrEmpty($env:HERMES_DEFERRED_GATEWAY_STARTUP_GATE) -or
      -not [string]::IsNullOrEmpty($env:HERMES_TERMINATE_WATCHER_LOG)
  } | ConvertTo-Json -Compress),
  [Text.UTF8Encoding]::new($false)
)
Start-Sleep -Milliseconds 1800
[IO.File]::WriteAllText($env:WINDOW_PROBE_SENTINEL, 'finished', [Text.UTF8Encoding]::new($false))
exit 0
`,
      'utf8'
    )

    const wrapped = wrapHandoffForDetachedConsole(
      { command: windowsPowerShellExecutable(), args: [], scriptPath },
      {
        bridgeLeaseId,
        branch,
        desktopPid: 4242,
        installRoot: directory,
        relaunchExe: path.join(directory, 'Hermes.exe')
      }
    )

    // This assertion intentionally precedes the real spawn: the old cmd/start
    // recipe fails RED without creating the visible minimized console it used.
    assert.match(wrapped.command, /powershell(?:\.exe)?$/i)
    assert.equal(
      wrapped.args.some(value => /^(?:start|\/min)$/i.test(value)),
      false
    )

    try {
      const launch = launchWindowsUpdateTransport(
        { kind: 'script', handoff: { command: windowsPowerShellExecutable(), args: [], scriptPath } },
        {
          bridgeLeaseId,
          branch,
          desktopPid: 4242,
          installRoot: directory,
          relaunchExe: path.join(directory, 'Hermes.exe')
        },
        {
          cwd: directory,
          detached: true,
          env: {
            ...process.env,
            TEMP: directory,
            TMP: directory,
            WINDOW_PROBE_READY: ready,
            WINDOW_PROBE_SENTINEL: sentinel,
            WINDOW_PROBE_TITLE: `Hermes hidden updater ${Date.now()}`,
            hermes_desktop_update_test: '1',
            Hermes_Deferred_Gateway_Startup_Gate: String.raw`C:\stale\startup.gate`,
            hermes_terminate_watcher_log: String.raw`C:\sensitive\watcher.log`
          },
          stdio: 'ignore'
        },
        { isWindows: true }
      )

      assert.equal(launch.kind, 'spawned')
      assert.equal(Number.isInteger(launch.child.pid), true)
      assert.equal(hasVisibleTopLevelWindow(Number(launch.child.pid)), false, 'launcher exposed a top-level window')

      const identity = await waitForWindowsHandoffOwnerIdentity(launch.child, { timeoutMs: 5_000 })

      assert.ok(identity, 'launcher did not publish an authenticated writer receipt')
      writerPid = identity.pid
      assert.equal(await waitForFile(ready), true, 'hidden writer never entered the probe script')

      const payload = JSON.parse(fs.readFileSync(ready, 'utf8')) as {
        branch: string
        lease: string
        pid: number
        toxic: boolean
      }

      assert.equal(payload.pid, identity.pid)
      assert.equal(payload.branch, branch)
      assert.equal(payload.lease, bridgeLeaseId)
      assert.equal(payload.toxic, false)
      assert.equal(await queryWindowsProcessCreationFileTime(identity.pid), identity.creationFileTime)
      assert.equal(await observeUpdaterGeneration(identity.pid, identity.creationFileTime, 100), true)
      assert.equal(hasVisibleTopLevelWindow(identity.pid), false, 'writer exposed a top-level window')
      assert.equal(await waitForFile(sentinel), true, 'writer did not survive its hidden launcher')
    } finally {
      if (writerPid && !fs.existsSync(sentinel)) {
        try {
          process.kill(writerPid)
        } catch {
          /* The exact writer already exited. */
        }
      }

      fs.rmSync(directory, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 })
    }
  },
  20_000
)

test.runIf(process.platform === 'win32')(
  'hidden writer survives a short-lived nested parent after the authenticated receipt',
  async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-hidden-parent-'))
    const scriptPath = path.join(directory, 'probe.ps1')
    const parentPath = path.join(directory, 'parent.cjs')
    const bridgePidPath = path.join(directory, 'bridge-pid.txt')
    const receiptPath = path.join(directory, 'receipt.txt')
    const ready = path.join(directory, 'ready.txt')
    const sentinel = path.join(directory, 'finished.txt')
    let writerPid: number | null = null

    fs.writeFileSync(
      scriptPath,
      String.raw`param(
  [Parameter(Mandatory = $true)][string]$InstallRoot,
  [Parameter(Mandatory = $true)][string]$Branch,
  [Parameter(Mandatory = $true)][int]$DesktopPid,
  [string]$RelaunchAppPath,
  [Parameter(Mandatory = $true)][string]$RelaunchExe,
  [Parameter(Mandatory = $true)][string]$BridgeLeaseId
)
[IO.File]::WriteAllText($env:WINDOW_PROBE_READY, "$PID", [Text.UTF8Encoding]::new($false))
Start-Sleep -Milliseconds 1800
[IO.File]::WriteAllText($env:WINDOW_PROBE_SENTINEL, 'finished', [Text.UTF8Encoding]::new($false))
exit 0
`,
      'utf8'
    )
    fs.writeFileSync(
      parentPath,
      String.raw`const { spawn } = require('node:child_process')
const fs = require('node:fs')
const payload = JSON.parse(process.env.HANDOFF_PARENT_PAYLOAD)
const child = spawn(payload.command, payload.args, payload.options)
fs.writeFileSync(process.env.HANDOFF_PARENT_BRIDGE_PID, String(child.pid))
let buffer = ''
child.stdout.on('data', chunk => {
  buffer += String(chunk)
  if (/^HERMES_UPDATE_HANDOFF_PID=[1-9][0-9]{0,9};FILETIME=[1-9][0-9]{0,19}\r?\n?$/.test(buffer)) {
    fs.writeFileSync(process.env.HANDOFF_PARENT_RECEIPT, buffer)
    process.exit(0)
  }
})
child.once('error', error => {
  fs.writeFileSync(process.env.HANDOFF_PARENT_RECEIPT, 'spawn-error:' + String(error && error.code || 'unknown'))
  process.exit(1)
})
child.once('exit', (code, signal) => {
  if (!fs.existsSync(process.env.HANDOFF_PARENT_RECEIPT)) {
    fs.writeFileSync(process.env.HANDOFF_PARENT_RECEIPT, 'early-exit:' + String(code) + ':' + String(signal))
    process.exit(1)
  }
})
`,
      'utf8'
    )

    const wrapped = wrapHandoffForDetachedConsole(
      { command: windowsPowerShellExecutable(), args: [], scriptPath },
      {
        bridgeLeaseId: 'unguessable-bridge-lease-id',
        branch: 'main',
        desktopPid: 4242,
        installRoot: directory,
        relaunchExe: path.join(directory, 'Hermes.exe')
      }
    )

    // Fail safely on the legacy visible wrapper before starting any process.
    assert.match(wrapped.command, /powershell(?:\.exe)?$/i)
    assert.equal(
      wrapped.args.some(value => /^(?:start|\/min)$/i.test(value)),
      false
    )

    const parentPayload = JSON.stringify({
      command: wrapped.command,
      args: wrapped.args,
      options: {
        cwd: directory,
        detached: false,
        env: {
          ...process.env,
          ...wrapped.env,
          TEMP: directory,
          TMP: directory,
          WINDOW_PROBE_READY: ready,
          WINDOW_PROBE_SENTINEL: sentinel
        },
        stdio: ['ignore', 'pipe', 'ignore'],
        windowsHide: true
      }
    })

    try {
      const nestedParent = spawn(process.execPath, [parentPath], {
        env: {
          ...process.env,
          HANDOFF_PARENT_PAYLOAD: parentPayload,
          HANDOFF_PARENT_BRIDGE_PID: bridgePidPath,
          HANDOFF_PARENT_RECEIPT: receiptPath
        },
        stdio: 'ignore',
        windowsHide: true
      })

      const parentExit = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
        nestedParent.once('exit', (code, signal) => resolve({ code, signal }))
      })

      assert.equal(
        hasVisibleTopLevelWindow(Number(nestedParent.pid)),
        false,
        'nested parent exposed a top-level window'
      )
      assert.equal(await waitForFile(bridgePidPath), true, 'nested parent did not publish the bridge PID')
      assert.equal(
        hasVisibleTopLevelWindow(Number(fs.readFileSync(bridgePidPath, 'utf8'))),
        false,
        'nested bridge exposed a top-level window'
      )
      assert.equal(await waitForFile(receiptPath), true, 'nested parent did not receive the writer receipt')
      assert.deepEqual(await parentExit, { code: 0, signal: null })

      const receipt = /^HERMES_UPDATE_HANDOFF_PID=([1-9][0-9]{0,9});FILETIME=([1-9][0-9]{0,19})\r?\n?$/.exec(
        fs.readFileSync(receiptPath, 'utf8')
      )

      assert.ok(receipt, `invalid nested receipt: ${fs.readFileSync(receiptPath, 'utf8')}`)
      writerPid = Number(receipt[1])
      assert.equal(await waitForFile(ready), true, 'writer did not survive nested parent exit')
      assert.equal(Number(fs.readFileSync(ready, 'utf8')), writerPid)
      assert.equal(await queryWindowsProcessCreationFileTime(writerPid), receipt[2])
      assert.equal(await observeUpdaterGeneration(writerPid, receipt[2], 100), true)
      assert.equal(hasVisibleTopLevelWindow(writerPid), false, 'nested writer exposed a top-level window')
      assert.equal(await waitForFile(sentinel), true, 'writer stopped after its nested parent exited')
    } finally {
      if (writerPid && !fs.existsSync(sentinel)) {
        try {
          process.kill(writerPid)
        } catch {
          /* The exact writer already exited. */
        }
      }

      fs.rmSync(directory, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 })
    }
  },
  20_000
)

test('formatPowerShellArgvForDisplay quotes a non-main git ref with shell metacharacters', () => {
  assert.equal(
    formatPowerShellArgvForDisplay(['hermes', 'update', '--branch', 'fork/&|%integration']),
    "hermes update --branch 'fork/&|%integration'"
  )
  assert.equal(
    formatPowerShellArgvForDisplay(['hermes', 'update', '--branch', "fork/o'hare"]),
    "hermes update --branch 'fork/o''hare'"
  )
})

test('resolvePosixScriptHandoff returns the bash recipe when the script exists', () => {
  const root = '/home/hermes/.hermes/hermes-agent'
  const expected = path.join(root, 'scripts', 'desktop-update', 'posix.sh')

  const handoff = resolvePosixScriptHandoff(root, {
    isWindows: false,
    fileExists: candidate => candidate === expected
  })

  assert.ok(handoff)
  assert.equal(handoff.command, '/bin/bash')
  assert.deepEqual(handoff.args, [expected])
})

test('resolvePosixScriptHandoff is null when the checkout predates the script', () => {
  const handoff = resolvePosixScriptHandoff('/home/hermes/.hermes/hermes-agent', {
    isWindows: false,
    fileExists: () => false
  })

  assert.equal(handoff, null)
})

test('resolvePosixScriptHandoff is null on Windows', () => {
  const handoff = resolvePosixScriptHandoff(String.raw`C:\Users\hermes\AppData\Local\hermes\hermes-agent`, {
    isWindows: true,
    fileExists: () => true
  })

  assert.equal(handoff, null)
})

test('collectRelaunchArgs drops Electron internals, keeps user/launcher args', () => {
  const argv = [
    '--type=renderer',
    '--user-data-dir=/tmp/x',
    '--enable-features=A,B',
    '--field-trial-handle=123',
    '--enable-logging',
    '--log-file=/tmp/log',
    '--lang=en-US',
    '--inspect=9229',
    '--remote-debugging-port=9222',
    '--no-sandbox',
    'hermes://open/session/abc',
    '--profile=work'
  ]

  assert.deepEqual(collectRelaunchArgs(argv), ['--no-sandbox', 'hermes://open/session/abc', '--profile=work'])
  assert.deepEqual(collectRelaunchArgs(undefined), [])
})

test('sandboxFallbackFromEnv: ELECTRON_DISABLE_SANDBOX / --no-sandbox opt out', () => {
  assert.equal(sandboxFallbackFromEnv({ ELECTRON_DISABLE_SANDBOX: '1' }, []), true)
  assert.equal(sandboxFallbackFromEnv({ ELECTRON_DISABLE_SANDBOX: 'true' }, []), true)
  assert.equal(sandboxFallbackFromEnv({}, ['--no-sandbox']), true)
  assert.equal(sandboxFallbackFromEnv({ ELECTRON_DISABLE_SANDBOX: '0' }, []), false)
  assert.equal(sandboxFallbackFromEnv({}, []), false)
})

// ── observeUpdaterHandoff (#66753) ──────────────────────────────────────────

class FakeChild {
  pid = 1234
  listeners = new Map<string, Array<(...args: unknown[]) => void>>()
  removed: string[] = []

  unref() {}

  kill() {
    return true
  }

  once(event: string, listener: (...args: unknown[]) => void) {
    const arr = this.listeners.get(event) ?? []

    arr.push(listener)
    this.listeners.set(event, arr)

    return this
  }

  removeListener(event: string, _listener: (...args: unknown[]) => void) {
    this.removed.push(event)

    return this
  }

  emit(event: string, ...args: unknown[]) {
    for (const listener of this.listeners.get(event) ?? []) {
      listener(...args)
    }
  }
}

function manualTimer() {
  const pending: Array<() => void> = []

  return {
    deps: {
      setTimeoutFn: (callback: () => void, _ms: number) => {
        pending.push(callback)

        return 0
      },
      clearTimeoutFn: () => {}
    },
    fire: () => {
      for (const callback of pending.splice(0)) {
        callback()
      }
    }
  }
}

test('observeUpdaterHandoff reports a spawn error instead of settling ok', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  const err: Error & { code?: string } = new Error('spawn ENOENT')

  err.code = 'ENOENT'
  child.emit('error', err)

  const outcome = await outcomePromise

  assert.equal(outcome.ok, false)
  assert.equal(outcome.reason, 'spawn-error')
  assert.match(outcome.message ?? '', /ENOENT/)
})

test('observeUpdaterHandoff reports a non-zero early exit', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  child.emit('exit', 127, null)

  const outcome = await outcomePromise

  assert.equal(outcome.ok, false)
  assert.equal(outcome.reason, 'early-exit')
  assert.equal(outcome.code, 127)
})

test('observeUpdaterHandoff reports a signal death inside the window', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  child.emit('exit', null, 'SIGTERM')

  const outcome = await outcomePromise

  assert.equal(outcome.ok, false)
  assert.equal(outcome.reason, 'early-exit')
  assert.equal(outcome.signal, 'SIGTERM')
})

test('observeUpdaterHandoff accepts a clean launcher exit 0', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  child.emit('exit', 0, null)

  const outcome = await outcomePromise

  assert.equal(outcome.ok, true)
  assert.equal(outcome.code, 0)
})

test('observeUpdaterHandoff settles ok when the child survives the window', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  timer.fire()

  const outcome = await outcomePromise

  assert.equal(outcome.ok, true)
  assert.equal(outcome.reason, undefined)
  // Listeners must be detached so a post-quit late exit can't fire them.
  assert.deepEqual(child.removed.sort(), ['error', 'exit'])
})

test('observeUpdaterHandoff ignores events after the first settle', async () => {
  const child = new FakeChild()
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff(child, 2500, timer.deps)

  child.emit('exit', 1, null)
  child.emit('error', new Error('late'))
  timer.fire()

  const outcome = await outcomePromise

  assert.equal(outcome.ok, false)
  assert.equal(outcome.reason, 'early-exit')
})

test('observeUpdaterHandoff settles ok for children without an event interface', async () => {
  const timer = manualTimer()
  const outcomePromise = observeUpdaterHandoff({ pid: 1, kill: () => true, unref: () => {} }, 2500, timer.deps)

  timer.fire()

  const outcome = await outcomePromise

  assert.equal(outcome.ok, true)
})
