<#
.SYNOPSIS
    Sign one file for Inno Setup and count it.

.DESCRIPTION
    Inno Setup's SignTool command calls this once for each file it signs (the installer and the
    uninstaller). It records the attempt in a counter file BEFORE it calls SignTool, so a signing
    request that fails or is retried still counts, then runs SignTool with the Artifact Signing
    dlib. kestrel.iss sets SignToolRetryCount to 0, so Inno does not retry on its own.

    Environment (set by the release job, never a secret):
      KESTREL_SIGNTOOL        full path to signtool.exe
      KESTREL_SIGN_DLIB       full path to Azure.CodeSigning.Dlib.dll
      KESTREL_SIGN_META       full path to the metadata.json for the account and profile
      KESTREL_SIGN_COUNTER    a file this script appends one line to for each signing attempt
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$File
)

$ErrorActionPreference = "Stop"
foreach ($name in @("KESTREL_SIGNTOOL", "KESTREL_SIGN_DLIB", "KESTREL_SIGN_META", "KESTREL_SIGN_COUNTER")) {
    if (-not (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue)) { throw "$name is not set" }
}
Add-Content -Path $env:KESTREL_SIGN_COUNTER -Value $File
& $env:KESTREL_SIGNTOOL sign /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 `
    /dlib $env:KESTREL_SIGN_DLIB /dmdf $env:KESTREL_SIGN_META $File
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "signing call: $File"
