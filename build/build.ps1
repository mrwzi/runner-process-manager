$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

if (-not (Test-Path -LiteralPath "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}

$specDir = Join-Path $root "build\\specs"
if (-not (Test-Path -LiteralPath $specDir)) {
    New-Item -ItemType Directory -Path $specDir | Out-Null
}
$iconFile = Join-Path $root "assets\\runner_icon.ico"

powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-runner-only.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Runner build failed with exit code $LASTEXITCODE."
}

$buildStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$workDir = Join-Path $root ".runner_runtime\pyinstaller-build\UpdateRunner-$buildStamp"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --icon $iconFile `
    --specpath $specDir `
    --distpath $root `
    --workpath $workDir `
    --name UpdateRunner `
    (Join-Path $root "build\\update_runner.py")

if ($LASTEXITCODE -ne 0) {
    throw "UpdateRunner build failed with exit code $LASTEXITCODE."
}
