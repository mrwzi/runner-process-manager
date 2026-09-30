param(
    [Parameter(Mandatory = $true)][string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'

function Get-Apps([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($raw -is [array]) { return @($raw) }
        return @($raw.apps)
    } catch { return @() }
}

$markerPath = Join-Path $RuntimeRoot 'upgrade-preinstall.json'
$failurePath = Join-Path $RuntimeRoot 'upgrade-verification-failed.json'
if (Test-Path -LiteralPath $failurePath) {
    throw 'A previous Runner upgrade registry verification failed. Agent and GUI launch are blocked until a new upgrade preflight succeeds.'
}
if (-not (Test-Path -LiteralPath $markerPath)) {
    throw 'Runner upgrade verification marker is missing. The GUI will not be launched.'
}
$marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
$expected = [int]$marker.expected_app_count
$appsPath = Join-Path $RuntimeRoot 'apps.json'
$actual = @(Get-Apps $appsPath).Count

# The count must match exactly. A mismatch is unsafe because it can hide a
# partial migration just as easily as an empty registry. Restore only the
# registry backup; identity, secrets, witness/cluster state, logs and project
# folders are intentionally untouched.
if ($actual -ne $expected) {
    $backup = [string]$marker.registry_backup
    if (-not $backup -or -not (Test-Path -LiteralPath $backup)) {
        throw "Runner registry count changed from $expected to $actual, but the registry backup is unavailable. GUI launch aborted."
    }
    Copy-Item -LiteralPath $backup -Destination $appsPath -Force
    $restored = @(Get-Apps $appsPath).Count
    @{ expected_app_count = $expected; observed_app_count = $actual; restored_app_count = $restored;
       failed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } |
        ConvertTo-Json | Set-Content -LiteralPath $failurePath -Encoding UTF8
    throw "Runner registry count changed from $expected to $actual during upgrade. The previous registry was restored ($restored apps). GUI launch aborted."
}

@{ expected_app_count = $expected; verified_app_count = $actual; verified_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'last-upgrade-verification.json') -Encoding UTF8
exit 0
