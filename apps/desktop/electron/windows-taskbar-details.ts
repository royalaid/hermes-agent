export const WINDOWS_APP_USER_MODEL_ID = 'com.nousresearch.hermes'

export interface WindowsTaskbarDetailsTarget {
  setAppDetails(details: {
    appId: string
    appIconIndex: number
    appIconPath: string
    relaunchCommand: string
    relaunchDisplayName: string
  }): void
}

export interface WindowsTaskbarDetailsOptions {
  appId: string
  iconPath?: string
  isWindows: boolean
  relaunchCommand: string
  relaunchDisplayName: string
}

/**
 * A managed Hermes update relaunches the packaged executable directly, without
 * an NSIS Start Menu shortcut for Windows to supply app metadata. Give each
 * visible window the same identity and relaunch branding explicitly so the
 * taskbar does not fall back to Electron's defaults.
 */
export function configureWindowsTaskbarDetails(
  target: WindowsTaskbarDetailsTarget,
  { appId, iconPath, isWindows, relaunchCommand, relaunchDisplayName }: WindowsTaskbarDetailsOptions
): void {
  if (!isWindows || !iconPath) {
    return
  }

  target.setAppDetails({
    appId,
    appIconIndex: 0,
    appIconPath: iconPath,
    relaunchCommand,
    relaunchDisplayName
  })
}
