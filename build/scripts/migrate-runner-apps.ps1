param([Parameter(Mandatory = $true)][string]$RuntimeRoot)

$ErrorActionPreference = 'Stop'
$appsPath = Join-Path $RuntimeRoot 'apps.json'
if (-not (Test-Path -LiteralPath $appsPath)) {
    throw "Runner application registry is missing: $appsPath"
}

$originalBytes = [IO.File]::ReadAllBytes($appsPath)
$rawText = [Text.Encoding]::UTF8.GetString($originalBytes).TrimStart([char]0xFEFF)
$raw = $rawText | ConvertFrom-Json
$isArray = $raw -is [array]
$apps = if ($isArray) { @($raw) } else { @($raw.apps) }
$kept = @($apps | Where-Object { [string]$_.app_type -ne 'docker_compose' })
if ($apps.Count -gt 0 -and $kept.Count -eq 0) {
    throw 'Upgrade blocked: every configured application would be removed. The registry was not changed.'
}

$retiredCount = $apps.Count - $kept.Count
if ($retiredCount -eq 0) {
    @{ source_app_count = $apps.Count; expected_app_count = $kept.Count; retired_compose_count = 0 } |
        ConvertTo-Json -Compress
    exit 0
}

$backupDir = Join-Path $RuntimeRoot 'config-backups'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backup = Join-Path $backupDir "apps-before-compose-removal-$stamp.json"
$suffix = 1
while (Test-Path -LiteralPath $backup) {
    $backup = Join-Path $backupDir "apps-before-compose-removal-$stamp-$suffix.json"
    $suffix++
}
[IO.File]::WriteAllBytes($backup, $originalBytes)

if ($isArray) {
    $updated = @($kept)
} else {
    $updated = [ordered]@{}
    foreach ($property in $raw.PSObject.Properties) {
        if ($property.Name -ne 'apps') { $updated[$property.Name] = $property.Value }
    }
    $updated['apps'] = @($kept)
}

$temporary = Join-Path $RuntimeRoot ('.apps-compose-migration-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    $json = ConvertTo-Json -InputObject $updated -Depth 100
    [IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $appsPath -Force

    $written = Get-Content -LiteralPath $appsPath -Raw | ConvertFrom-Json
    $writtenApps = if ($written -is [array]) { @($written) } else { @($written.apps) }
    if ($writtenApps.Count -ne $kept.Count -or @($writtenApps | Where-Object { [string]$_.app_type -eq 'docker_compose' }).Count -ne 0) {
        throw "Registry verification failed after migration. Expected $($kept.Count) supported apps."
    }
} catch {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    [IO.File]::WriteAllBytes($appsPath, $originalBytes)
    throw "Compose-entry migration failed; the original registry was restored from its in-memory backup. Recovery copy: $backup. $($_.Exception.Message)"
}

@{ source_app_count = $apps.Count; expected_app_count = $kept.Count;
   retired_compose_count = $retiredCount; registry_backup = $backup } | ConvertTo-Json -Compress
