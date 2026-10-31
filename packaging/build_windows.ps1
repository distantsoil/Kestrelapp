<#
.SYNOPSIS
    Build Kestrel-Setup-<version>.exe: PyInstaller one-folder, optional Authenticode signing,
    Inno Setup.

.DESCRIPTION
    packaging\build_windows.ps1                # full build
    packaging\build_windows.ps1 -SkipBuild     # dist\Kestrel already exists
    packaging\build_windows.ps1 -CleanData     # then wipe %LOCALAPPDATA%\Kestrel (backup first)
    packaging\build_windows.ps1 -CleanData -KeepKeys   # same, but keep .env (API keys)

    -CleanData runs the freshly built `kestrel-cli.exe reset --yes` so the next launch is a true
    first run (D-UI-27). It writes a backup zip to %LOCALAPPDATA%\Kestrel-backups first and
    refuses while Kestrel is running. Without -KeepKeys the API keys go too.

    Environment:
      PYTHON                     interpreter with kestrel + pyinstaller (default: python)
      WINDOWS_CERT_PFX_BASE64    base64 of the code-signing .pfx; absent = unsigned build
      WINDOWS_CERT_PASSWORD      the .pfx password
      WINDOWS_CERT_TIMESTAMP_URL RFC 3161 timestamp server (default: DigiCert)

    Unsigned is the default and is a working build: SmartScreen shows "Windows protected your PC"
    on the first run of the installer, and the operator clicks More info, then Run anyway. Signing
    with the operator's own certificate (an OV or EV Authenticode certificate) removes that; it is
    gated on the WINDOWS_CERT_* variables so the same script serves both. See docs/RELEASING.md.
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$CleanData,
    [switch]$KeepKeys
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Dist = Join-Path $Root "dist\Kestrel"

$initPy = Get-Content (Join-Path $Root "kestrel\__init__.py") -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') {
    throw "could not read __version__ from kestrel\__init__.py"
}
$Version = $Matches[1]
$Setup = Join-Path $Root "dist\Kestrel-Setup-$Version.exe"

# ------------------------------------------------------------------ 1. the one-folder build
if (-not $SkipBuild) {
    Write-Host "==> version.iss from __version__ $Version"
    & $Python (Join-Path $Root "packaging\gen_version_iss.py")
    if ($LASTEXITCODE -ne 0) { throw "gen_version_iss failed" }
    Write-Host "==> bundled embedding model (kestrel.spec refuses to build without it)"
    Push-Location $Root
    try {
        & $Python scripts\fetch_embed_model.py --check
        if ($LASTEXITCODE -ne 0) {
            & $Python scripts\fetch_embed_model.py
            if ($LASTEXITCODE -ne 0) { throw "fetch_embed_model failed" }
        }
    } finally { Pop-Location }
    Write-Host "==> PyInstaller one-folder build"
    Push-Location $Root
    try {
        & $Python -m PyInstaller packaging\kestrel.spec --noconfirm
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    } finally { Pop-Location }
}
foreach ($exe in @("Kestrel.exe", "kestrel-cli.exe")) {
    if (-not (Test-Path (Join-Path $Dist $exe))) {
        throw "missing $Dist\$exe; run PyInstaller first (or drop -SkipBuild)"
    }
}

# ------------------------------------------------------------------ 1b. clean data (optional)
if ($CleanData) {
    Write-Host "==> Reset the local Kestrel data so the next launch is a first run"
    $resetArgs = @("reset", "--yes")
    if ($KeepKeys) { $resetArgs += "--keep-keys" }
    & (Join-Path $Dist "kestrel-cli.exe") @resetArgs
    if ($LASTEXITCODE -ne 0) { throw "kestrel-cli reset failed (is Kestrel still running?)" }
}

# ------------------------------------------------------------------ 2. signtool (optional)
function Find-Signtool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem -Path $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*\x64\*" } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$Signed = $false
$PfxPath = $null
if ($env:WINDOWS_CERT_PFX_BASE64 -and $env:WINDOWS_CERT_PASSWORD) {
    $Signtool = Find-Signtool
    if (-not $Signtool) { throw "WINDOWS_CERT_* set but signtool.exe not found (install the Windows SDK)" }
    $PfxPath = Join-Path ([System.IO.Path]::GetTempPath()) ("kestrel-sign-" + [guid]::NewGuid() + ".pfx")
    [IO.File]::WriteAllBytes($PfxPath, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX_BASE64))
    $Timestamp = if ($env:WINDOWS_CERT_TIMESTAMP_URL) { $env:WINDOWS_CERT_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $Signed = $true
    Write-Host "==> signing dist\Kestrel\*.exe with the provided certificate"
    foreach ($exe in @("Kestrel.exe", "kestrel-cli.exe")) {
        & $Signtool sign /f $PfxPath /p $env:WINDOWS_CERT_PASSWORD /fd SHA256 /tr $Timestamp /td SHA256 `
            /d "Kestrel" (Join-Path $Dist $exe)
        if ($LASTEXITCODE -ne 0) { throw "signtool failed on $exe" }
    }
} else {
    Write-Host @"
==> WINDOWS_CERT_PFX_BASE64 / WINDOWS_CERT_PASSWORD are not set: the installer will be UNSIGNED.
    It installs and runs, but SmartScreen shows "Windows protected your PC" the first time; the
    operator clicks More info, then Run anyway. To remove that step, add your own Authenticode
    certificate as those two variables (base64 of the .pfx, and its password). See docs/RELEASING.md.
"@
}

# ------------------------------------------------------------------ 3. Inno Setup
function Find-Iscc {
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # winget installs per user by default (LOCALAPPDATA\Programs); an admin install uses Program Files.
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
$Iscc = Find-Iscc
if (-not $Iscc) { throw "ISCC.exe not found: install Inno Setup 6 (winget install JRSoftware.InnoSetup)" }

Write-Host "==> Inno Setup -> $Setup"
if (Test-Path $Setup) { Remove-Item $Setup -Force }
& $Iscc (Join-Path $Root "packaging\kestrel.iss")
if ($LASTEXITCODE -ne 0) { throw "iscc failed" }
if (-not (Test-Path $Setup)) { throw "iscc did not produce $Setup (OutputBaseFilename mismatch?)" }

if ($Signed) {
    Write-Host "==> signing the installer"
    & $Signtool sign /f $PfxPath /p $env:WINDOWS_CERT_PASSWORD /fd SHA256 /tr $Timestamp /td SHA256 `
        /d "Kestrel Setup" $Setup
    if ($LASTEXITCODE -ne 0) { throw "signtool failed on the installer" }
}
if ($PfxPath -and (Test-Path $PfxPath)) { Remove-Item $PfxPath -Force }

Get-Item $Setup | Format-List Name, Length
Write-Host "done: $Setup"
