# Behavioral contract for the Windows Shell registration helper.
# Run under both PowerShell 7 and inbox Windows PowerShell in installer CI.

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$helper = Join-Path $repoRoot 'scripts\windows-desktop-shortcuts.ps1'
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-shortcut-test-{0}" -f [Guid]::NewGuid().ToString('N'))

try {
    $targetExe = Join-Path $tempRoot 'Hermes.exe'
    $shortcutPath = Join-Path $tempRoot 'Programs\Hermes.lnk'
    $legacyShortcutPath = Join-Path $tempRoot 'Programs\Electron.lnk'
    $otherShortcutPath = Join-Path $tempRoot 'OtherPrograms\Hermes.lnk'
    $unrelatedElectronPath = Join-Path $tempRoot 'OtherPrograms\Electron.lnk'
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $shortcutPath) -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $otherShortcutPath) -Force | Out-Null
    New-Item -ItemType File -Path $targetExe -Force | Out-Null

    . $helper
    $wsh = New-Object -ComObject WScript.Shell
    $legacyShortcut = $wsh.CreateShortcut($legacyShortcutPath)
    $legacyShortcut.TargetPath = $targetExe
    $legacyShortcut.Description = 'Electron'
    $legacyShortcut.Save()
    Set-HermesShortcutIdentity -ShortcutPath $legacyShortcutPath -DisplayName 'Electron'

    $unrelatedElectron = $wsh.CreateShortcut($unrelatedElectronPath)
    $unrelatedElectron.TargetPath = $targetExe
    $unrelatedElectron.Description = 'Unrelated Electron app'
    $unrelatedElectron.Save()

    New-HermesDesktopShortcuts -TargetExe $targetExe -ShortcutPaths @($shortcutPath, $otherShortcutPath) | Out-Null

    if (Test-Path -LiteralPath $legacyShortcutPath) {
        throw 'Hermes-owned legacy Electron.lnk was not removed'
    }
    if (-not (Test-Path -LiteralPath $unrelatedElectronPath)) {
        throw 'Unrelated Electron.lnk was removed'
    }

    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.NameSpace((Split-Path -Parent $shortcutPath))
    $item = $folder.ParseName((Split-Path -Leaf $shortcutPath))
    if ([string]$item.ExtendedProperty('System.Title') -ne 'Hermes') {
        throw "System.Title was not Hermes"
    }
    if ([string]$item.ExtendedProperty('System.AppUserModel.ID') -ne 'com.nousresearch.hermes') {
        throw "System.AppUserModel.ID was not com.nousresearch.hermes"
    }
    Write-Host 'OK: Hermes shortcut title and AppUserModelID are registered'
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
