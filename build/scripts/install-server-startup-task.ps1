$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\dist\windows')).Path
$installer = Join-Path $PSScriptRoot 'install-runner-agent.ps1'
if (-not (Test-Path $installer)) { throw 'Runner Agent installer script is missing.' }
& $installer -InstallDir $root -RuntimeRoot (Join-Path $env:ProgramData 'Runner_V4')
