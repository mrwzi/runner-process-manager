$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$specDir = Join-Path $root "build\temp\specs"
$distDir = Join-Path $root "dist\windows"
New-Item -ItemType Directory -Force -Path $specDir, $distDir | Out-Null
$iconFile = Join-Path $root "src\assets\runner_icon.ico"

powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-runner-only.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Runner build failed with exit code $LASTEXITCODE."
}

$buildStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$workDir = Join-Path $root "build\temp\pyinstaller\UpdateRunner-$buildStamp"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --icon $iconFile `
    --specpath $specDir `
    --distpath $distDir `
    --workpath $workDir `
    --name UpdateRunner `
    (Join-Path $PSScriptRoot "update_runner.py")

if ($LASTEXITCODE -ne 0) {
    throw "UpdateRunner build failed with exit code $LASTEXITCODE."
}
