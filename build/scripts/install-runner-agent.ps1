param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'
$taskName = 'Runner Agent'
$resultPath = Join-Path $RuntimeRoot 'agent-provisioning.json'

function Write-ProvisionResult([bool]$Success, [string]$Message) {
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    @{ success = $Success; message = $Message; installed_at = [DateTime]::UtcNow.ToString('o'); install_dir = $InstallDir; runtime_root = $RuntimeRoot } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Runner Agent installation requires administrator approval. Runner did not enable cluster mode.'
    }

    $runner = Join-Path $InstallDir 'Runner.exe'
    if (-not (Test-Path -LiteralPath $runner)) { throw "Runner.exe was not found in $InstallDir" }
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null

    # SYSTEM owns the scheduled Agent; the interactive local controller needs
    # access to local authenticated API metadata and settings.
    & icacls $RuntimeRoot /grant '*S-1-5-32-545:(OI)(CI)M' /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not repair permissions on $RuntimeRoot" }

    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $runner -Argument "--agent --runtime-root `"$RuntimeRoot`"" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 30) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $servicePrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $servicePrincipal -Description 'Runner background Agent. It safely owns Runner applications even when the GUI is closed.' -Force | Out-Null

    $ruleName = 'Runner Agent (Tailscale)'
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47473 -RemoteAddress '100.64.0.0/10' -Profile Private,Domain -Program $runner | Out-Null
    }

    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(20)
    $healthy = $false
    $failure = 'Runner Agent did not initialize its local API in time'
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath (Join-Path $RuntimeRoot 'agent-api.json')) {
            try {
                $token = (Get-Content -LiteralPath (Join-Path $RuntimeRoot 'agent-api.json') -Raw | ConvertFrom-Json).token
                $response = Invoke-RestMethod -Uri 'http://127.0.0.1:47471/v1/status' -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
                if ($response.node_id) { $healthy = $true; break }
                $failure = 'Runner Agent returned an invalid local health response'
            } catch { $failure = $_.Exception.Message }
        }
        Start-Sleep -Milliseconds 350
    }
    if (-not $healthy) { throw $failure }
    Write-ProvisionResult $true 'Runner Agent task and authenticated local API are healthy'
    exit 0
} catch {
    try { Write-ProvisionResult $false $_.Exception.Message } catch { }
    Write-Error "Runner Agent provisioning failed: $($_.Exception.Message)"
    exit 1
}
