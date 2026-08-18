$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runnerExe = Join-Path $root "Runner.exe"
$taskName = "Runner V4 Server Startup"

if (-not (Test-Path -LiteralPath $runnerExe)) {
    throw "Runner.exe was not found. Build Runner first."
}

$isSourceTree = (
    (Test-Path -LiteralPath (Join-Path $root "launcher\run.py")) -and
    (Test-Path -LiteralPath (Join-Path $root "build")) -and
    (Test-Path -LiteralPath (Join-Path $root "manager"))
)
$runtimeRoot = if ($isSourceTree) {
    Join-Path $root ".runner_runtime"
} else {
    Join-Path $env:LOCALAPPDATA "Runner_V4"
}

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

$quotedRunner = '"' + $runnerExe + '"'
$quotedRuntime = '"' + $runtimeRoot + '"'
$argument = "/c start `"Runner V4 Headless`" /min $quotedRunner --headless --runtime-root $quotedRuntime"

$action = New-ScheduledTaskAction -Execute "$env:ComSpec" -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts Runner V4 headless at system boot and launches apps marked Start when Runner opens." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $taskName"
Write-Host "Runner: $runnerExe"
Write-Host "Runtime root: $runtimeRoot"
