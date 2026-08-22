# Shared, pure JSON parsing helpers for the Windows Desktop updater.
#
# Production and the contract harness dot-source this file so the tests call
# the shipped parser directly. -ForceCoreFallback is an internal test seam;
# desktop-update.ps1 never forwards user input or environment state to it.

function ConvertFrom-DesktopStampJson(
    [string]$Raw,
    [switch]$ForceCoreFallback
) {
    $convertFromJson = Get-Command ConvertFrom-Json -ErrorAction Stop
    if ($PSVersionTable.PSEdition -ne 'Core') {
        return (ConvertFrom-Json -InputObject $Raw -ErrorAction Stop)
    }
    if (-not $ForceCoreFallback -and $convertFromJson.Parameters.ContainsKey('DateKind')) {
        return (ConvertFrom-Json -InputObject $Raw -DateKind String -ErrorAction Stop)
    }

    # PowerShell Core 6.0-7.4 coerces ISO strings to DateTime but does not
    # expose -DateKind. Its already-loaded Newtonsoft reader can preserve raw
    # strings without Add-Type or a compiler. This exact stamp is flat by
    # contract, so reject nested, duplicate, comment, float, and trailing data.
    $textReader = $null
    $jsonReader = $null
    try {
        $textReader = [System.IO.StringReader]::new($Raw)
        $jsonReader = [Newtonsoft.Json.JsonTextReader]::new($textReader)
        $jsonReader.DateParseHandling = [Newtonsoft.Json.DateParseHandling]::None
        if (-not $jsonReader.Read() -or
            $jsonReader.TokenType -ne [Newtonsoft.Json.JsonToken]::StartObject) {
            throw 'Desktop stamp root is not an object'
        }
        $values = [ordered]@{}
        $ended = $false
        while ($jsonReader.Read()) {
            if ($jsonReader.TokenType -eq [Newtonsoft.Json.JsonToken]::EndObject) {
                $ended = $true
                break
            }
            if ($jsonReader.TokenType -ne [Newtonsoft.Json.JsonToken]::PropertyName -or
                $jsonReader.Value -isnot [string]) {
                throw 'Desktop stamp property name is invalid'
            }
            $name = [string]$jsonReader.Value
            if ($values.Contains($name) -or -not $jsonReader.Read()) {
                throw 'Desktop stamp property is duplicated or incomplete'
            }
            $value = switch ($jsonReader.TokenType) {
                ([Newtonsoft.Json.JsonToken]::String) { [string]$jsonReader.Value; break }
                ([Newtonsoft.Json.JsonToken]::Integer) { [int64]$jsonReader.Value; break }
                ([Newtonsoft.Json.JsonToken]::Boolean) { [bool]$jsonReader.Value; break }
                ([Newtonsoft.Json.JsonToken]::Null) { $null; break }
                default { throw 'Desktop stamp value type is invalid' }
            }
            $values[$name] = $value
        }
        if (-not $ended -or $jsonReader.Read()) {
            throw 'Desktop stamp JSON is incomplete or has trailing data'
        }
        return [pscustomobject]$values
    } finally {
        if ($jsonReader) { $jsonReader.Dispose() }
        if ($textReader) { $textReader.Dispose() }
    }
}
