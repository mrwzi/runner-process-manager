$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$out = Join-Path $root 'build\temp\portable\Runner-v4.1.0-portable'
$zip = Join-Path $root 'dist\portable\Runner-v4.1.0-portable.zip'
if (Test-Path $out) { Remove-Item -LiteralPath $out -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $out 'assets') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $out 'tools') -Force | Out-Null
Copy-Item (Join-Path $root 'dist\windows\Runner.exe') $out
Copy-Item (Join-Path $root 'dist\windows\UpdateRunner.exe') $out
Copy-Item (Join-Path $root 'src\assets\runner_icon.ico') (Join-Path $out 'assets')
Copy-Item (Join-Path $PSScriptRoot 'install-runner-agent.ps1') (Join-Path $out 'tools')
Copy-Item (Join-Path $PSScriptRoot 'remove-runner-agent.ps1') (Join-Path $out 'tools')
Copy-Item (Join-Path $PSScriptRoot 'repair-runner.ps1') (Join-Path $out 'tools')
Set-Content -LiteralPath (Join-Path $out 'README.txt') -Value "Runner portable build. No node identity, cluster configuration, secrets, certificates, logs, or runtime data are included. Runner creates a new local identity on first launch."
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip -CompressionLevel Optimal
Copy-Item -LiteralPath $zip -Destination (Join-Path $root 'release\Runner-v4.1.0-portable.zip') -Force
Write-Host "Portable ZIP: $zip"
