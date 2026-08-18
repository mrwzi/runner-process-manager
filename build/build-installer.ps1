$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$installerScript = Join-Path $PSScriptRoot "installer.iss"
$installerOut = Join-Path $root "installer"

Set-Location $root

if (-not (Test-Path -LiteralPath "Runner.exe")) {
    throw "Runner.exe is missing. Run build\build.ps1 first."
}

if (-not (Test-Path -LiteralPath "UpdateRunner.exe")) {
    throw "UpdateRunner.exe is missing. Run build\build.ps1 first."
}

$iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if (-not $iscc) {
    $defaultPaths = @(
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $defaultPaths) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            $iscc = Get-Item -LiteralPath $path
            break
        }
    }
}

if (-not $iscc) {
    throw "Inno Setup 6 compiler (ISCC.exe) was not found. Install Inno Setup 6, then rerun this script."
}

if (-not (Test-Path -LiteralPath $installerOut)) {
    New-Item -ItemType Directory -Path $installerOut | Out-Null
}

$isccPath = if ($iscc.Source) { $iscc.Source } else { $iscc.FullName }
& $isccPath $installerScript

$setup = Join-Path $installerOut "RunnerSetup.exe"
if (-not (Test-Path -LiteralPath $setup)) {
    throw "Installer build finished but RunnerSetup.exe was not found."
}

Get-FileHash -Algorithm SHA256 $setup | Format-List
Write-Host "Installer created: $setup"
