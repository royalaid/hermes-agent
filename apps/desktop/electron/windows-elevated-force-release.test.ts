import assert from 'node:assert/strict'
import { execFile, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import fsNative from 'node:fs'
import osNative from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { describe, it, vi } from 'vitest'

import {
  buildForceReleaseRequest,
  canonicalForceReleasePayload,
  canonicalNumericToken,
  ELEVATED_FORCE_RELEASE_BOOTSTRAP_TEMPLATE,
  ELEVATED_FORCE_RELEASE_HELPER_SHA256,
  ELEVATED_FORCE_RELEASE_JOB_JOIN_TEMPLATE,
  formatElevatedForceReleaseFailure,
  parseForceReleaseResponse,
  verifyForceReleaseRequest
} from './windows-elevated-force-release'
import { parseTerminateScriptOutput } from './windows-process-terminate'
import {
  buildRestartManagerScript,
  parseRestartManagerOutput,
  RESTART_MANAGER_ROW_SPLIT_EXPRESSION
} from './windows-restart-manager'

const execFileAsync = promisify(execFile)

describe('elevated force-release request contract', () => {
  it('binds request MAC to install root + exact holder claims', () => {
    const secret = 's'.repeat(32)

    const request = buildForceReleaseRequest({
      installRoot: 'C:\\Users\\gwmai\\AppData\\Local\\hermes',
      holders: [{ pid: 9, createdAt: 100, name: 'hermes.exe', cmdline: 'hermes.exe tools', source: 'scanner' }],
      secret,
      now: 1_000,
      ttlMs: 60_000,
      nonce: 'abc123'
    })

    assert.equal(request.nonce, 'abc123')
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\hermes', 1_500).ok,
      true
    )
    assert.equal(
      verifyForceReleaseRequest(request, secret, 'C:\\Users\\gwmai\\AppData\\Local\\other', 1_500).ok,
      false
    )
    assert.equal(verifyForceReleaseRequest(request, 'wrong', request.installRoot, 1_500).ok, false)
    assert.equal(verifyForceReleaseRequest(request, secret, request.installRoot, 100_000).ok, false)
  })

  it('canonical payload is stable for helper MAC verification', () => {
    const payload = canonicalForceReleasePayload({
      schemaVersion: 1,
      nonce: 'n',
      issuedAt: 1,
      expiresAt: 2,
      installRoot: 'C:\\h',
      installRootHash: 'abc',
      holders: [{ pid: 1, createdAt: 2, name: 'x', resource: 'y' }]
    })

    assert.equal(payload, ['1', 'n', '1', '2', 'C:\\h', 'abc', '1\t2\tx\ty', ''].join('\n'))
  })

  it('keeps TS/PowerShell MAC numeric parity for fractional create times', async () => {
    const createdAt = 1755738237.4531252

    const body = {
      schemaVersion: 1,
      nonce: 'n',
      issuedAt: 10,
      expiresAt: 20,
      installRoot: 'C:\\h',
      installRootHash: 'abc',
      holders: [{ pid: 1, createdAt, name: 'x', resource: 'y' }]
    }

    const canonical = canonicalForceReleasePayload(body)
    assert.match(canonical, new RegExp(`1\\t${canonicalNumericToken(createdAt)}\\tx\\ty`))

    if (process.platform !== 'win32') {return}
    const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

    const script = `
function Format-CanonicalNumber([double]$Value) {
  if ([double]::IsNaN($Value) -or [double]::IsInfinity($Value)) { return '0' }
  if ($Value -eq 0) { return '0' }
  $truncated = [math]::Truncate($Value)
  if ($Value -eq $truncated -and [math]::Abs($Value) -lt 9007199254740991) {
    return [string][int64]$truncated
  }
  return $Value.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture)
}
$created = [double]1755738237.4531252
$line = ("{0}\`t{1}\`t{2}\`t{3}" -f 1, (Format-CanonicalNumber $created), 'x', 'y')
$canonical = @('1','n','10','20','C:\\h','abc',$line,'') -join "\`n"
$expected = (@('1','n','10','20','C:\\h','abc',("1\`t1755738237.4531252\`tx\`ty"),'') -join "\`n")
if ($canonical -ne $expected) {
  Write-Output 'CANON_MISMATCH'
  Write-Output $canonical
  exit 2
}
Write-Output 'mac-parity-ok'
exit 0
`.trim()

    const { stdout } = await execFileAsync(
      ps,
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { encoding: 'utf8', windowsHide: true, timeout: 10_000 }
    )

    assert.match(String(stdout), /mac-parity-ok/)
  })

  it('rejects response nonce mismatch', () => {
    const raw = JSON.stringify({ schemaVersion: 1, nonce: 'other', ok: true, cleared: true })
    assert.equal(parseForceReleaseResponse(raw, 'expected'), null)
  })

  it('formats elevated survivor failures with pid/resource/win32', () => {
    const failure = formatElevatedForceReleaseFailure({
      schemaVersion: 1,
      nonce: 'n',
      ok: true,
      cleared: false,
      survivors: [{ pid: 9, detail: 'win32=5', resource: 'C:\\h\\venv\\Scripts\\hermes.exe', win32Error: 5 }]
    })

    assert.match(failure.message, /PID 9/)
    assert.match(failure.message, /hermes\.exe/)
    assert.equal(failure.protectedHolders, true)
  })
})

describe('terminate script output parser', () => {
  it('classifies create-time mismatch, access denied, and protected', () => {
    assert.deepEqual(parseTerminateScriptOutput('CREATE_TIME_MISMATCH actual=1 expected=2', 3), {
      kind: 'create-time-mismatch'
    })
    assert.deepEqual(parseTerminateScriptOutput('ACCESS_DENIED', 5), {
      kind: 'access-denied',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('PROTECTED win32=5', 5), {
      kind: 'protected',
      win32Error: 5
    })
    assert.deepEqual(parseTerminateScriptOutput('TERMINATED', 0), { kind: 'terminated' })
    assert.deepEqual(parseTerminateScriptOutput('ALREADY_GONE', 0), { kind: 'already-gone' })
    assert.equal(parseTerminateScriptOutput('FAILED win32=87', 1).kind, 'failed')
  })
})

describe('restart manager output parser', () => {
  it('maps RM rows into force-release holders and emits safe split expression', () => {
    const holders = parseRestartManagerOutput(
      JSON.stringify([{ pid: 12, createdAt: 34, name: 'python.exe' }]),
      ['C:\\h\\venv\\Scripts\\hermes.exe']
    )

    assert.equal(holders.length, 1)
    assert.equal(holders[0]?.source, 'restart-manager')
    assert.equal(holders[0]?.pid, 12)
    assert.match(String(holders[0]?.resource), /hermes\.exe/)
    assert.equal(RESTART_MANAGER_ROW_SPLIT_EXPRESSION, "$part.Split([char]'|', 4)")
    assert.match(buildRestartManagerScript(['C:\\a']), /\$part\.Split\(\[char\]'\|', 4\)/)
  })

  it('preserves per-resource RM evidence instead of collapsing to resources[0]', () => {
    const holders = parseRestartManagerOutput(
      JSON.stringify([
        { pid: 1, createdAt: 10, name: 'a', resource: 'C:\\h\\venv\\Scripts\\hermes.exe' },
        { pid: 2, createdAt: 11, name: 'b', resource: 'C:\\h\\venv\\Scripts\\python.exe' }
      ]),
      ['C:\\h\\venv\\Scripts\\hermes.exe', 'C:\\h\\venv\\Scripts\\python.exe']
    )

    assert.equal(holders.length, 2)
    assert.match(String(holders.find(h => h.pid === 1)?.resource), /hermes\.exe/)
    assert.match(String(holders.find(h => h.pid === 2)?.resource), /python\.exe/)
  })
})

describe('elevated UAC launch argv safety', () => {
  it('keeps adversarial filesystem paths out of PowerShell source and only in env data', async () => {
    const {
      buildElevatedForceReleaseLaunchInvocation,
      ELEVATED_FORCE_RELEASE_ARGUMENT_QUOTER,
      ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND,
      launchElevatedForceReleaseHelper
    } =
      await import('./windows-elevated-force-release')

    const adversarial = {
      helperScriptPath: "C:\\Users\\gwmai\\AppData\\Local\\tmp\\evil$()`payload (1)\\u4e2d\\u6587'file.ps1".replace(
        '\\u4e2d\\u6587',
        '\u4e2d\u6587'
      ),
      requestPath: "C:\\tmp\\req $HOME $(Get-Process) path.json",
      responsePath: "C:\\tmp\\resp backtick` and 'quotes'.json"
    }

    const invocation = buildElevatedForceReleaseLaunchInvocation(adversarial)
    const commandText = invocation.command
    assert.equal(commandText, ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND)
    assert.doesNotMatch(commandText, /evil/)
    assert.doesNotMatch(commandText, /Get-Process/)
    assert.doesNotMatch(commandText, /JSON\.stringify/)

    for (const value of Object.values(adversarial)) {
      assert.ok(!commandText.includes(value), 'path must not appear in constant launcher source')
    }

    assert.equal(invocation.env.HERMES_FORCE_RELEASE_HELPER, adversarial.helperScriptPath)
    assert.equal(invocation.env.HERMES_FORCE_RELEASE_REQUEST, adversarial.requestPath)
    assert.equal(invocation.env.HERMES_FORCE_RELEASE_RESPONSE, adversarial.responsePath)

    let captured: { args: string[]; env?: NodeJS.ProcessEnv } | null = null

    const result = await launchElevatedForceReleaseHelper({
      ...adversarial,
      platform: 'win32',
      run: async (_command, args, options) => {
        captured = { args, env: options?.env }

        return { code: 0, stdout: 'HERMES_ELEVATED_PID=41111\nHERMES_ELEVATED_CREATED_AT=1700000000.5' }
      },
      confirmIdentityAbsent: async identity => identity.pid === 41111 && identity.createdAt === 1_700_000_000.5
    })

    assert.equal(result.kind, 'launched')
    assert.ok(captured)
    assert.deepEqual(captured!.args.slice(0, 4), ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command'])
    assert.equal(captured!.args[4], ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND)

    for (const value of Object.values(adversarial)) {
      assert.ok(!captured!.args.some(arg => arg.includes(value)), 'adversarial path must not appear in outer argv source text')
    }

    assert.equal(captured!.env?.HERMES_FORCE_RELEASE_HELPER, adversarial.helperScriptPath)
    assert.equal(captured!.env?.HERMES_FORCE_RELEASE_REQUEST, adversarial.requestPath)
    assert.equal(captured!.env?.HERMES_FORCE_RELEASE_RESPONSE, adversarial.responsePath)
  })

  it('preserves adversarial paths through the real non-UAC Start-Process boundary', async () => {
    if (process.platform !== 'win32') {return}

    const {
      ELEVATED_FORCE_RELEASE_ARGUMENT_QUOTER
    } = await import('./windows-elevated-force-release')

    const temp = fsNative.mkdtempSync(path.join(osNative.tmpdir(), 'hermes force-args '))
    const captureScript = path.join(temp, 'capture arguments.ps1')
    const outputPath = path.join(temp, 'captured output.json')

    const expected = [
      path.join(temp, "helper $() `tick (1) '中文'.ps1"),
      path.join(temp, "request $HOME (2) 'quoted'.json"),
      path.join(temp, "response backtick` $() 中文.json")
    ]

    fsNative.writeFileSync(
      captureScript,
      [
        'param([string]$A, [string]$B, [string]$C)',
        "[IO.File]::WriteAllText($env:PROBE_OUTPUT, (ConvertTo-Json @($A, $B, $C) -Compress), [Text.UTF8Encoding]::new($false))"
      ].join('\n'),
      'utf8'
    )

    const probe = [
      ELEVATED_FORCE_RELEASE_ARGUMENT_QUOTER,
      "$ps = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'",
      "$argList = @('-NoLogo','-NoProfile','-NonInteractive','-File',(ConvertTo-WindowsArgument $env:CAPTURE_SCRIPT),(ConvertTo-WindowsArgument $env:PROBE_A),(ConvertTo-WindowsArgument $env:PROBE_B),(ConvertTo-WindowsArgument $env:PROBE_C))",
      "$p = Start-Process -FilePath $ps -Wait -PassThru -WindowStyle Hidden -ArgumentList $argList",
      'exit $p.ExitCode'
    ].join('; ')

    try {
      await execFileAsync(
        path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
        ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', probe],
        {
          encoding: 'utf8',
          windowsHide: true,
          timeout: 10_000,
          env: {
            ...process.env,
            CAPTURE_SCRIPT: captureScript,
            PROBE_OUTPUT: outputPath,
            PROBE_A: expected[0],
            PROBE_B: expected[1],
            PROBE_C: expected[2]
          }
        }
      )

      assert.deepEqual(JSON.parse(fsNative.readFileSync(outputPath, 'utf8')), expected)
    } finally {
      fsNative.rmSync(temp, { recursive: true, force: true })
    }
  })

  it('retains the early elevated PID for exact response-temp cleanup after interruption', async () => {
    const {
      cleanupForceReleaseArtifacts,
      forceReleasePaths,
      launchElevatedForceReleaseHelper
    } = await import('./windows-elevated-force-release')

    const directory = fsNative.mkdtempSync(path.join(osNative.tmpdir(), 'hermes-force-response-temp-'))
    const responsePath = forceReleasePaths(directory, 'interrupted').responsePath
    const helperPid = 47111
    const responseTempPath = `${responsePath}.${helperPid}.tmp`
    const sentinelPath = path.join(directory, 'unrelated-sentinel.txt')
    fsNative.writeFileSync(responseTempPath, 'partial response', 'utf8')
    fsNative.writeFileSync(sentinelPath, 'keep', 'utf8')

    try {
      const launch = await launchElevatedForceReleaseHelper({
        helperScriptPath: 'C:\\helper.ps1',
        requestPath: 'C:\\request.json',
        responsePath,
        platform: 'win32',
        run: async () => ({
          code: 1,
          stdout: `HERMES_ELEVATED_PID=${helperPid}\nHERMES_ELEVATED_CREATED_AT=1700000000.5`
        }),
        confirmIdentityAbsent: async () => true
      })

      assert.equal(launch.kind, 'failed')
      assert.equal(launch.responseTempPath, responseTempPath)

      cleanupForceReleaseArtifacts({
        directory,
        responsePath,
        responseTempPath: launch.responseTempPath,
        ownedDirectory: false
      })
      assert.equal(fsNative.existsSync(responseTempPath), false)
      assert.equal(fsNative.existsSync(sentinelPath), true)
    } finally {
      fsNative.rmSync(directory, { recursive: true, force: true })
    }
  })

  it('fails closed when the authenticated elevated identity survives launcher return', async () => {
    const { launchElevatedForceReleaseHelper } = await import('./windows-elevated-force-release')

    const result = await launchElevatedForceReleaseHelper({
      helperScriptPath: 'C:\\helper.ps1',
      requestPath: 'C:\\request.json',
      responsePath: 'C:\\response.json',
      platform: 'win32',
      run: async () => ({
        code: 1,
        stdout: 'HERMES_ELEVATED_PID=41212\nHERMES_ELEVATED_CREATED_AT=1700000001.25'
      }),
      confirmIdentityAbsent: async identity => {
        assert.deepEqual(identity, { pid: 41212, createdAt: 1_700_000_001.25 })

        return false
      }
    })

    assert.equal(result.kind, 'failed')

    if (result.kind === 'failed') {assert.equal(result.detail, 'elevated helper survived terminal boundary')}
  })

  it(
    'kills the job-assigned bootstrap before it can write after launcher death',
    { timeout: 20_000 },
    async () => {
      if (process.platform !== 'win32') {return}

      const { ELEVATED_FORCE_RELEASE_JOB_JOIN_TEMPLATE, ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND } =
        await import('./windows-elevated-force-release')

      const temp = fsNative.mkdtempSync(path.join(osNative.tmpdir(), 'hermes-elevated-job-'))
      const helper = path.join(temp, 'delayed-helper.ps1')
      const bootstrap = `${ELEVATED_FORCE_RELEASE_JOB_JOIN_TEMPLATE}\n[IO.File]::WriteAllText($env:HERMES_JOB_READY, 'ready')\nStart-Sleep -Seconds 30\n[IO.File]::WriteAllText($env:HERMES_JOB_LATE, 'late')`
      const ready = path.join(temp, 'ready.txt')
      const late = path.join(temp, 'late.txt')
      fsNative.writeFileSync(
        helper,
        [
          'param([string]$RequestPath,[string]$ResponsePath)',
          '[IO.File]::WriteAllText($env:HERMES_JOB_READY, "ready")',
          'Start-Sleep -Seconds 30',
          '[IO.File]::WriteAllText($env:HERMES_JOB_LATE, "late")'
        ].join('\n'),
        'utf8'
      )

      const ps = path.join(
        process.env.SystemRoot || 'C:\\Windows',
        'System32',
        'WindowsPowerShell',
        'v1.0',
        'powershell.exe'
      )

      const nonElevatedLauncher = ELEVATED_FORCE_RELEASE_LAUNCHER_COMMAND.replace(' -Verb RunAs', '')

      const child = spawn(
        ps,
        ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', nonElevatedLauncher],
        {
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
          env: {
            ...process.env,
            HERMES_FORCE_RELEASE_HELPER: helper,
            HERMES_FORCE_RELEASE_REQUEST: path.join(temp, 'request.json'),
            HERMES_FORCE_RELEASE_RESPONSE: path.join(temp, 'response.json'),
            HERMES_FORCE_RELEASE_BOOTSTRAP: bootstrap,
            HERMES_JOB_READY: ready,
            HERMES_JOB_LATE: late
          }
        }
      )

      let stdout = ''
      let stderr = ''
      child.stdout?.on('data', chunk => {
        stdout += String(chunk)
      })
      child.stderr?.on('data', chunk => {
        stderr += String(chunk)
      })

      try {
        const readyDeadline = Date.now() + 10_000

        while (!fsNative.existsSync(ready) && Date.now() < readyDeadline && child.exitCode == null) {
          await new Promise(resolve => setTimeout(resolve, 25))
        }

        assert.equal(
          fsNative.existsSync(ready),
          true,
          `bootstrap did not start code=${child.exitCode} stdout=${stdout} stderr=${stderr}`
        )
        const helperPid = Number(stdout.match(/HERMES_ELEVATED_PID=(\d+)/)?.[1])
        assert.ok(Number.isInteger(helperPid) && helperPid > 0, `missing helper PID: ${stdout}`)

        child.kill()
        await new Promise<void>(resolve => {
          if (child.exitCode != null) {return resolve()}
          child.once('close', () => resolve())
        })
        const absenceDeadline = Date.now() + 5_000
        let alive = true

        while (Date.now() < absenceDeadline) {
          try {
            process.kill(helperPid, 0)
          } catch {
            alive = false

            break
          }

          await new Promise(resolve => setTimeout(resolve, 25))
        }

        assert.equal(alive, false, `job-assigned helper ${helperPid} survived launcher death`)
        await new Promise(resolve => setTimeout(resolve, 250))
        assert.equal(fsNative.existsSync(late), false, 'helper performed a delayed write after launcher death')
      } finally {
        if (child.exitCode == null) {child.kill()}
        fsNative.rmSync(temp, { recursive: true, force: true })
      }
    }
  )

})

describe('force-release artifact cleanup', () => {
  it('removes only owned request/secret/response files and leaves unrelated sentinels', async () => {
    const fs = await import('node:fs')
    const os = await import('node:os')
    const path = await import('node:path')

    const {
      cleanupForceReleaseArtifacts,
      forceReleasePaths,
      writeForceReleaseRequestFiles
    } = await import('./windows-elevated-force-release')

    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-force-cleanup-'))

    const written = await writeForceReleaseRequestFiles({
      installRoot: 'C:\\h',
      holders: [{ pid: 1, createdAt: 2, name: 'x', cmdline: 'x', source: 'scanner' }],
      directory
    })

    assert.equal(written.ownedDirectory, false)
    const sentinel = path.join(directory, 'unrelated-sentinel.txt')
    fs.writeFileSync(sentinel, 'keep-me', 'utf8')

    cleanupForceReleaseArtifacts({
      directory: written.directory,
      requestPath: written.requestPath,
      secretPath: written.secretPath,
      responsePath: written.responsePath,
      ownedDirectory: true
    })

    assert.equal(fs.existsSync(written.requestPath), false)
    assert.equal(fs.existsSync(written.secretPath), false)
    assert.equal(fs.existsSync(written.responsePath), false)
    assert.equal(fs.existsSync(sentinel), true)
    assert.equal(fs.existsSync(directory), true)
    fs.rmSync(directory, { recursive: true, force: true })
  })

  it('removes an empty owned directory non-recursively after artifact cleanup', async () => {
    const fs = await import('node:fs')
    const os = await import('node:os')
    const path = await import('node:path')

    const { cleanupForceReleaseArtifacts, writeForceReleaseRequestFiles } = await import(
      './windows-elevated-force-release'
    )

    const written = await writeForceReleaseRequestFiles({
      installRoot: 'C:\\h',
      holders: [{ pid: 1, createdAt: 2, name: 'x', cmdline: 'x', source: 'scanner' }]
    })

    assert.equal(written.ownedDirectory, true)
    cleanupForceReleaseArtifacts(written)
    assert.equal(fs.existsSync(written.requestPath), false)
    assert.equal(fs.existsSync(written.secretPath), false)
    assert.equal(fs.existsSync(written.directory), false)
  })

  it('cleans partial artifacts when request serialization fails', async () => {
    const { writeForceReleaseRequestFiles } = await import('./windows-elevated-force-release')
    const originalWrite = fsNative.writeFileSync.bind(fsNative)
    const originalMkdtemp = fsNative.mkdtempSync.bind(fsNative)
    const writeSpy = vi.spyOn(fsNative, 'writeFileSync')
    const mkdtempSpy = vi.spyOn(fsNative, 'mkdtempSync')
    let writes = 0
    let createdDirectory: string | undefined
    writeSpy.mockImplementation(((filePath: any, data: any, options?: any) => {
      writes += 1

      if (writes === 2) {throw new Error('injected request write failure')}

      return originalWrite(filePath, data, options)
    }) as typeof fsNative.writeFileSync)
    mkdtempSpy.mockImplementation(((prefix: string, options?: any) => {
      const created = originalMkdtemp(prefix, options)
      createdDirectory = typeof created === 'string' ? created : undefined

      return created
    }) as typeof fsNative.mkdtempSync)

    try {
      await assert.rejects(
        writeForceReleaseRequestFiles({
          installRoot: 'C:\\h',
          holders: [{ pid: 1, createdAt: 2, name: 'x', cmdline: 'x', source: 'scanner' }]
        }),
        /injected request write failure/
      )
      assert.ok(createdDirectory, 'request writer did not create an owned directory')
      assert.equal(fsNative.existsSync(createdDirectory!), false)
    } finally {
      writeSpy.mockRestore()
      mkdtempSpy.mockRestore()
    }
  })
})

describe('elevated helper script shape', () => {
  it('binds normalized helper bytes and performs no external mutation before Job assignment', () => {
    const helperPath = path.resolve(__dirname, '../../../scripts/desktop-update/windows-force-release.ps1')
    const helperText = fsNative.readFileSync(helperPath, 'utf8').replace(/\r\n?/g, '\n')
    const digest = createHash('sha256').update(Buffer.from(helperText, 'utf8')).digest('hex')
    assert.equal(digest, ELEVATED_FORCE_RELEASE_HELPER_SHA256)
    assert.ok(ELEVATED_FORCE_RELEASE_BOOTSTRAP_TEMPLATE.startsWith(ELEVATED_FORCE_RELEASE_JOB_JOIN_TEMPLATE))
    const beforeAssignment = ELEVATED_FORCE_RELEASE_JOB_JOIN_TEMPLATE.split('$native::AssignProcessToJobObject')[0] ?? ''
    assert.doesNotMatch(beforeAssignment, /Add-Type|Start-Process|WriteAllText|WriteAllBytes|CreateText|OpenWrite/)
    assert.match(ELEVATED_FORCE_RELEASE_BOOTSTRAP_TEMPLATE, /ScriptBlock\]::Create\(\$helperText\)/)
  })

  it('does not broad path-scan installRoot and consumes signed excludePids', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const helperPath = path.resolve(__dirname, '../../../scripts/desktop-update/windows-force-release.ps1')
    const text = fs.readFileSync(helperPath, 'utf8')
    assert.match(text, /excludePids/)
    assert.doesNotMatch(text, /Get-CimInstance Win32_Process \| ForEach-Object/)
    assert.match(text, /never terminate unauthenticated|Fail-closed: never terminate unauthenticated/i)
    assert.match(text, /current Restart Manager holders/i)
    assert.doesNotMatch(text, /source = 'claim-revalidate'/)
    assert.match(text, /holder resource claim missing/)
    assert.match(text, /holder resource outside install root/)
    assert.match(text, /GetFinalPathNameByHandle/)
    assert.match(text, /IsSameOrUnderRoot\(\$imageFinal, \$installRootFinal\)/)
    assert.match(text, /OpenAuthenticated\(\$holderPid, \$expected/)
    assert.match(text, /TerminateHandle\(\$processHandle\)/)
    assert.match(text, /current-lock-ownership-mismatch/)
    assert.doesNotMatch(text, /Terminate\(\$holderPid\)/)
  })
})
