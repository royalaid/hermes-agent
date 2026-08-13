import { describe, expect, it, vi } from 'vitest'

import {
  buildWindowsRelaunchCommand,
  configureWindowsTaskbarDetails,
  resolveWindowsAppUserModelId,
  WINDOWS_APP_USER_MODEL_ID,
  WINDOWS_DEV_APP_USER_MODEL_ID
} from './windows-taskbar-details'

describe('configureWindowsTaskbarDetails', () => {
  it('gives a Windows window the Hermes taskbar identity and relaunch details', () => {
    const setAppDetails = vi.fn()

    configureWindowsTaskbarDetails(
      { setAppDetails },
      {
        appId: WINDOWS_APP_USER_MODEL_ID,
        iconPath: 'C:\\Program Files\\Hermes\\resources\\icon.ico',
        isWindows: true,
        relaunchCommand: '"C:\\Program Files\\Hermes\\Hermes.exe"',
        relaunchDisplayName: 'Hermes'
      }
    )

    expect(setAppDetails).toHaveBeenCalledExactlyOnceWith({
      appId: WINDOWS_APP_USER_MODEL_ID,
      appIconIndex: 0,
      appIconPath: 'C:\\Program Files\\Hermes\\resources\\icon.ico',
      relaunchCommand: '"C:\\Program Files\\Hermes\\Hermes.exe"',
      relaunchDisplayName: 'Hermes'
    })
  })

  it('does nothing outside Windows', () => {
    const setAppDetails = vi.fn()

    configureWindowsTaskbarDetails(
      { setAppDetails },
      {
        appId: WINDOWS_APP_USER_MODEL_ID,
        iconPath: 'C:\\Hermes\\icon.ico',
        isWindows: false,
        relaunchCommand: '"C:\\Hermes\\Hermes.exe"',
        relaunchDisplayName: 'Hermes'
      }
    )

    expect(setAppDetails).not.toHaveBeenCalled()
  })

  it('keeps Windows identity metadata when the icon cannot be resolved', () => {
    const setAppDetails = vi.fn()

    configureWindowsTaskbarDetails(
      { setAppDetails },
      {
        appId: WINDOWS_APP_USER_MODEL_ID,
        iconPath: undefined,
        isWindows: true,
        relaunchCommand: '"C:\\Hermes\\Hermes.exe"',
        relaunchDisplayName: 'Hermes'
      }
    )

    expect(setAppDetails).toHaveBeenCalledExactlyOnceWith({
      appId: WINDOWS_APP_USER_MODEL_ID,
      relaunchCommand: '"C:\\Hermes\\Hermes.exe"',
      relaunchDisplayName: 'Hermes'
    })
  })

  it('includes the app entry when Electron runs in default-app mode', () => {
    expect(
      buildWindowsRelaunchCommand({
        executablePath: 'C:\\Program Files\\Hermes\\electron.exe',
        appEntryPath: 'C:\\Users\\gwmai\\git\\hermes-agent\\apps\\desktop',
        isDefaultApp: true
      })
    ).toBe('"C:\\Program Files\\Hermes\\electron.exe" "C:\\Users\\gwmai\\git\\hermes-agent\\apps\\desktop"')
  })

  it('uses the packaged executable alone outside default-app mode', () => {
    expect(
      buildWindowsRelaunchCommand({
        executablePath: 'C:\\Program Files\\Hermes\\Hermes.exe',
        appEntryPath: undefined,
        isDefaultApp: false
      })
    ).toBe('"C:\\Program Files\\Hermes\\Hermes.exe"')
  })

  it('keeps unpackaged Electron runs out of the production Hermes taskbar group', () => {
    expect(resolveWindowsAppUserModelId(false)).toBe(WINDOWS_APP_USER_MODEL_ID)
    expect(resolveWindowsAppUserModelId(true)).toBe(WINDOWS_DEV_APP_USER_MODEL_ID)
  })
})
