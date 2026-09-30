$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$installerScript = Join-Path $PSScriptRoot "installer.iss"
$installerOut = Join-Path $root "release"
$buildTempRoot = Join-Path $root "build\temp"
$buildOut = Join-Path $buildTempRoot ("installer-" + [guid]::NewGuid().ToString("N"))

Set-Location $root

if (-not (Test-Path -LiteralPath "dist\windows\Runner.exe")) {
    throw "dist\windows\Runner.exe is missing. Run build\scripts\build.ps1 first."
}

if (-not (Test-Path -LiteralPath "dist\windows\UpdateRunner.exe")) {
    throw "dist\windows\UpdateRunner.exe is missing. Run build\scripts\build.ps1 first."
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
New-Item -ItemType Directory -Path $buildOut -Force | Out-Null

$isccPath = if ($iscc.Source) { $iscc.Source } else { $iscc.FullName }
& $isccPath "/O$buildOut" "/FRunnerSetup-4.1.0" $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compilation failed with exit code $LASTEXITCODE."
}

$builtSetup = Join-Path $buildOut "RunnerSetup-4.1.0.exe"
if (-not (Test-Path -LiteralPath $builtSetup)) {
    throw "Installer build finished but RunnerSetup-4.1.0.exe was not found."
}

$setup = Join-Path $installerOut "RunnerSetup-4.1.0.exe"
$stage = Join-Path $installerOut (".RunnerSetup-4.1.0-stage-" + [guid]::NewGuid().ToString("N") + ".exe")
Copy-Item -LiteralPath $builtSetup -Destination $stage
if (Test-Path -LiteralPath $setup) {
    $previousRelease = Join-Path $buildOut "RunnerSetup-4.1.0.previous.exe"
    try {
        [IO.File]::Replace($stage, $setup, $previousRelease)
    } catch {
        Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
        throw "Could not atomically publish the installer to $setup. The existing release was preserved. $($_.Exception.Message)"
    }
} else {
    try { [IO.File]::Move($stage, $setup) }
    catch {
        Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
        throw "Could not publish the installer to $setup. $($_.Exception.Message)"
    }
}

Get-FileHash -Algorithm SHA256 $setup | Format-List
Write-Host "Installer created: $setup"
