$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$specDir = Join-Path $root "build\temp\specs"
$distDir = Join-Path $root "dist\windows"
New-Item -ItemType Directory -Force -Path $specDir, $distDir | Out-Null
$appsJson = Join-Path $root "src\config\apps.json"
$runPy = Join-Path $root "src\launcher\run.py"
$sourceDir = Join-Path $root "src"
$assetsDir = Join-Path $sourceDir "assets"
$iconFile = Join-Path $assetsDir "runner_icon.ico"
$buildStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$workDir = Join-Path $root "build\temp\pyinstaller\Runner-$buildStamp"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon $iconFile `
    --specpath $specDir `
    --distpath $distDir `
    --workpath $workDir `
    --paths $sourceDir `
    --name Runner `
    --add-data "${appsJson};." `
    --add-data "${assetsDir};assets" `
    $runPy

if ($LASTEXITCODE -ne 0) {
    throw "Runner build failed with exit code $LASTEXITCODE."
}
