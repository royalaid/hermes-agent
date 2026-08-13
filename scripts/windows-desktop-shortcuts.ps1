# windows-desktop-shortcuts.ps1 -- canonical Windows Shell registration for Hermes.
#
# A Start Menu shortcut is not only a launch target. Its AppUserModelID is how
# Windows associates a running process with the human-facing Start Menu and
# taskbar identity. Keep both Start Menu and Desktop shortcuts direct-to-exe,
# with the same ID the packaged Electron process applies at runtime.

param(
    [switch]$Repair,
    [string]$RepairTargetExe = ''
)

$script:HermesWindowsAppUserModelId = 'com.nousresearch.hermes'

function Initialize-HermesShortcutPropertyStore {
    if ('HermesShortcutPropertyStore' -as [type]) { return }

    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[Flags]
public enum HermesGetPropertyStoreFlags : uint { ReadWrite = 0x00000002 }

[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct HermesPropertyKey {
    public Guid formatId;
    public uint propertyId;

    public HermesPropertyKey(Guid formatId, uint propertyId) {
        this.formatId = formatId;
        this.propertyId = propertyId;
    }
}

[StructLayout(LayoutKind.Explicit)]
public struct HermesPropVariant {
    [FieldOffset(0)] public ushort valueType;
    [FieldOffset(8)] public IntPtr pointerValue;

    public static HermesPropVariant FromString(string value) {
        return new HermesPropVariant {
            valueType = 31, // VT_LPWSTR
            pointerValue = Marshal.StringToCoTaskMemUni(value)
        };
    }

    public void Dispose() {
        if (valueType == 31 && pointerValue != IntPtr.Zero) {
            Marshal.FreeCoTaskMem(pointerValue);
            pointerValue = IntPtr.Zero;
        }
    }
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface HermesIPropertyStore {
    void GetCount(out uint count);
    void GetAt(uint index, out HermesPropertyKey key);
    void GetValue(ref HermesPropertyKey key, out HermesPropVariant value);
    void SetValue(ref HermesPropertyKey key, ref HermesPropVariant value);
    void Commit();
}

public static class HermesShortcutPropertyStore {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHGetPropertyStoreFromParsingName(
        string path,
        IntPtr bindContext,
        HermesGetPropertyStoreFlags flags,
        ref Guid interfaceId,
        [MarshalAs(UnmanagedType.Interface)] out HermesIPropertyStore store
    );

    private static void SetStringProperty(HermesIPropertyStore store, HermesPropertyKey key, string text) {
        var value = HermesPropVariant.FromString(text);
        try {
            store.SetValue(ref key, ref value);
        } finally {
            value.Dispose();
        }
    }

    public static void SetIdentity(string shortcut, string appId, string displayName) {
        Guid propertyStoreInterfaceId = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        HermesIPropertyStore store;
        SHGetPropertyStoreFromParsingName(
            shortcut,
            IntPtr.Zero,
            HermesGetPropertyStoreFlags.ReadWrite,
            ref propertyStoreInterfaceId,
            out store
        );

        var appUserModelId = new HermesPropertyKey(
            new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
            5
        );
        var title = new HermesPropertyKey(
            new Guid("F29F85E0-4FF9-1068-AB91-08002B27B3D9"),
            2
        );

        try {
            SetStringProperty(store, appUserModelId, appId);
            SetStringProperty(store, title, displayName);
            store.Commit();
        } finally {
            Marshal.ReleaseComObject(store);
        }
    }
}
'@ -ErrorAction Stop
}

function Set-HermesShortcutIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [string]$AppUserModelId = $script:HermesWindowsAppUserModelId,
        [string]$DisplayName = 'Hermes'
    )

    Initialize-HermesShortcutPropertyStore
    [HermesShortcutPropertyStore]::SetIdentity($ShortcutPath, $AppUserModelId, $DisplayName)
}

function New-HermesDesktopShortcuts {
    param(
        [Parameter(Mandatory = $true)][string]$TargetExe,
        [string[]]$ShortcutPaths = @(
            (Join-Path ([Environment]::GetFolderPath('Programs')) 'Hermes.lnk'),
            (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hermes.lnk')
        )
    )

    if (-not (Test-Path -LiteralPath $TargetExe -PathType Leaf)) {
        throw "Hermes executable not found: $TargetExe"
    }

    $shell = New-Object -ComObject WScript.Shell
    $workDir = Split-Path -Parent $TargetExe
    $iconIco = Join-Path $workDir 'resources\icon.ico'
    $iconLocation = if (Test-Path -LiteralPath $iconIco) { "$iconIco,0" } else { "$TargetExe,0" }
    $created = @()

    $failures = @()
    foreach ($lnkPath in $ShortcutPaths) {
        try {
            $parent = Split-Path -Parent $lnkPath
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Force -Path $parent | Out-Null
            }

            $shortcut = $shell.CreateShortcut($lnkPath)
            $shortcut.TargetPath = $TargetExe
            $shortcut.WorkingDirectory = $workDir
            $shortcut.IconLocation = $iconLocation
            $shortcut.Description = 'Hermes Agent'
            $shortcut.Save()
            Set-HermesShortcutIdentity -ShortcutPath $lnkPath
            $created += $lnkPath
        } catch {
            $failures += "$lnkPath ($($_.Exception.Message))"
            Write-Warning "Could not refresh Hermes shortcut: $lnkPath"
        }
    }

    # Repaint the Start Menu and Desktop after a direct-to-exe update. This is
    # best effort: the shortcut itself is already valid if the cache utility is
    # unavailable on a particular Windows SKU.
    try { & ie4uinit.exe -show 2>$null } catch {}

    if ($failures.Count -gt 0) {
        throw "Could not refresh one or more Hermes shortcuts: $($failures -join '; ')"
    }

    return $created
}

if ($Repair) {
    if (-not $RepairTargetExe) {
        throw '-RepairTargetExe is required with -Repair'
    }
    New-HermesDesktopShortcuts -TargetExe $RepairTargetExe | Out-Null
}
