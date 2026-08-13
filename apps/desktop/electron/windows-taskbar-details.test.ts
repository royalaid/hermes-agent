import { describe, expect, it, vi } from 'vitest'

import { configureWindowsTaskbarDetails, WINDOWS_APP_USER_MODEL_ID } from './windows-taskbar-details'

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

  it.each([
    { iconPath: 'C:\\Hermes\\icon.ico', isWindows: false },
    { iconPath: undefined, isWindows: true }
  ])('does nothing when Windows taskbar details cannot apply', ({ iconPath, isWindows }) => {
    const setAppDetails = vi.fn()

    configureWindowsTaskbarDetails(
      { setAppDetails },
      {
        appId: WINDOWS_APP_USER_MODEL_ID,
        iconPath,
        isWindows,
        relaunchCommand: '"C:\\Hermes\\Hermes.exe"',
        relaunchDisplayName: 'Hermes'
      }
    )

    expect(setAppDetails).not.toHaveBeenCalled()
  })
})
