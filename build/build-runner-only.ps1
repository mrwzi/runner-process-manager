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

$appsJson = Join-Path $root "config\\apps.json"
$logsDir = Join-Path $root "logs"
$runPy = Join-Path $root "launcher\\run.py"
$assetsDir = Join-Path $root "assets"
$iconFile = Join-Path $assetsDir "runner_icon.ico"
$buildStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$workDir = Join-Path $root ".runner_runtime\pyinstaller-build\Runner-$buildStamp"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon $iconFile `
    --specpath $specDir `
    --distpath $root `
    --workpath $workDir `
    --name Runner `
    --add-data "${appsJson};." `
    --add-data "${logsDir};logs" `
    --add-data "${assetsDir};assets" `
    $runPy

if ($LASTEXITCODE -ne 0) {
    throw "Runner build failed with exit code $LASTEXITCODE."
}
