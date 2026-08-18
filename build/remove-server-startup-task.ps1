$ErrorActionPreference = "Stop"

$taskName = "Runner V4 Server Startup"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task: $taskName"
} else {
    Write-Host "Scheduled task was not installed: $taskName"
}
