param(
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot),
    [string]$RuntimeRoot = "$env:ProgramData\Runner_V4"
)

$script = Join-Path $PSScriptRoot 'install-runner-agent.ps1'
& $script -InstallDir $InstallDir -RuntimeRoot $RuntimeRoot
exit $LASTEXITCODE
