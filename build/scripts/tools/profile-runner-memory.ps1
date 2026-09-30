param(
    [Parameter(Mandatory = $true)][int[]]$ProcessId,
    [int]$DurationHours = 4,
    [int]$IntervalSeconds = 60,
    [string]$OutputPath = "$env:TEMP\runner-memory-profile.csv"
)

# Read-only sampler for a live installation.  It never starts, stops, or
# signals a process.  PerfProc works for SYSTEM-owned Agent processes where
# Get-Process cannot expose CPU counters to a non-elevated GUI user.
$end = (Get-Date).AddHours($DurationHours)
"timestamp,pid,name,cpu_percent,private_bytes,working_set_private,handle_count" | Set-Content -LiteralPath $OutputPath -Encoding utf8
while ((Get-Date) -lt $end) {
    $rows = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $ProcessId -contains [int]$_.IDProcess }
    foreach ($row in $rows) {
        "{0:o},{1},{2},{3},{4},{5},{6}" -f (Get-Date),$row.IDProcess,$row.Name,$row.PercentProcessorTime,$row.PrivateBytes,$row.WorkingSetPrivate,$row.HandleCount |
            Add-Content -LiteralPath $OutputPath -Encoding utf8
    }
    Start-Sleep -Seconds $IntervalSeconds
}
Write-Output $OutputPath
