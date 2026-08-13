export const WINDOWS_APP_USER_MODEL_ID = 'com.nousresearch.hermes'
export const WINDOWS_DEV_APP_USER_MODEL_ID = `${WINDOWS_APP_USER_MODEL_ID}.Dev`

export interface WindowsTaskbarDetailsTarget {
  setAppDetails(details: {
    appId: string
    appIconIndex?: number
    appIconPath?: string
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

export interface WindowsRelaunchCommandOptions {
  executablePath: string
  appEntryPath?: string
  isDefaultApp: boolean
}

export function resolveWindowsAppUserModelId(isDefaultApp: boolean): string {
  return isDefaultApp ? WINDOWS_DEV_APP_USER_MODEL_ID : WINDOWS_APP_USER_MODEL_ID
}

function quoteWindowsCommandArgument(value: string): string {
  return `"${value.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\*)$/, '$1$1')}"`
}

export function buildWindowsRelaunchCommand({
  executablePath,
  appEntryPath,
  isDefaultApp
}: WindowsRelaunchCommandOptions): string {
  const command = quoteWindowsCommandArgument(executablePath)

  if (!isDefaultApp || !appEntryPath) {
    return command
  }

  return `${command} ${quoteWindowsCommandArgument(appEntryPath)}`
}

/**
 * A managed Hermes update relaunches the packaged executable directly. Keep
 * each visible window aligned with the installed shortcut's identity and
 * relaunch behavior.
 */
export function configureWindowsTaskbarDetails(
  target: WindowsTaskbarDetailsTarget,
  { appId, iconPath, isWindows, relaunchCommand, relaunchDisplayName }: WindowsTaskbarDetailsOptions
): void {
  if (!isWindows) {
    return
  }

  target.setAppDetails({
    appId,
    ...(iconPath ? { appIconIndex: 0, appIconPath: iconPath } : {}),
    relaunchCommand,
    relaunchDisplayName
  })
}
