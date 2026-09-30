param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [switch]$DryRun,
    [switch]$ValidateOnly,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $RuntimeRoot 'logs\installer-upgrade.log'

function Write-UpgradeLog([string]$Message) {
    # Dry-run is intentionally read-only; diagnostics are emitted as JSON to
    # stdout so production runtime state is not touched during preflight.
    if ($DryRun) { return }
    try {
        $directory = Split-Path -Parent $logPath
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
    } catch { }
}

function Get-RestartManagerOwners([string]$Path) {
    try {
        if (-not ('RunnerRestartManager' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class RunnerRestartManager {
    [StructLayout(LayoutKind.Sequential)] public struct FILETIME { public uint Low; public uint High; }
    [StructLayout(LayoutKind.Sequential)] public struct UNIQUE_PROCESS { public int Pid; public FILETIME Started; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct PROCESS_INFO {
        public UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string AppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string ServiceName;
        public int AppType; public uint Status; public uint SessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool Restartable;
    }
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] static extern int RmStartSession(out uint handle, int flags, StringBuilder key);
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] static extern int RmRegisterResources(uint handle, uint fileCount, string[] files, uint appCount, UNIQUE_PROCESS[] apps, uint serviceCount, string[] services);
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] static extern int RmGetList(uint handle, out uint needed, ref uint count, [In, Out] PROCESS_INFO[] info, ref uint reasons);
    [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint handle);
    public static string[] GetOwners(string path) {
        uint handle; int result = RmStartSession(out handle, 0, new StringBuilder(Guid.NewGuid().ToString("N")));
        if (result != 0) return new string[0];
        try {
            result = RmRegisterResources(handle, 1, new string[] { path }, 0, null, 0, null);
            if (result != 0) return new string[0];
            uint needed, count = 0, reasons = 0;
            result = RmGetList(handle, out needed, ref count, new PROCESS_INFO[0], ref reasons);
            if (result != 234 || needed == 0) return new string[0];
            PROCESS_INFO[] info = new PROCESS_INFO[needed]; count = needed;
            result = RmGetList(handle, out needed, ref count, info, ref reasons);
            if (result != 0) return new string[0];
            string[] output = new string[count];
            for (int i = 0; i < count; i++) output[i] = info[i].AppName + " (PID " + info[i].Process.Pid + ")";
            return output;
        } finally { RmEndSession(handle); }
    }
}
'@
        }
        return @([RunnerRestartManager]::GetOwners($Path))
    } catch { return @() }
}
$targetRunner = [IO.Path]::GetFullPath((Join-Path $InstallDir 'Runner.exe'))

function Test-RunnerBinary([string]$Path) {
    if (-not $Path) { return $false }
    try { return [string]::Equals([IO.Path]::GetFullPath($Path), $targetRunner, [StringComparison]::OrdinalIgnoreCase) }
    catch { return $false }
}

function Get-AuthenticatedAgentPid {
    # WMI may report a blank ExecutablePath/CommandLine for a PyInstaller
    # one-file child. The local API's PID is authoritative because it is
    # returned only after authenticating with this machine's runtime token.
    $apiFile = Join-Path $RuntimeRoot 'agent-api.json'
    if (-not (Test-Path -LiteralPath $apiFile)) { return $null }
    try {
        $record = Get-Content -LiteralPath $apiFile -Raw | ConvertFrom-Json
        $listen = [string]$record.listen
        if ($listen -notmatch '^127\.0\.0\.1:(\d+)$') { return $null }
        $port = [int]$Matches[1]
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/health" `
            -Headers @{ Authorization = "Bearer $($record.token)" } -TimeoutSec 3
        if ($response.ok -and $response.pid -gt 0) { return [int]$response.pid }
    } catch { }
    return $null
}

function Get-RunnerAncestors([int]$ProcessId, [hashtable]$ProcessMap) {
    $ids = [System.Collections.Generic.List[int]]::new()
    $cursor = $ProcessId
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    while ($cursor -gt 0 -and $seen.Add($cursor) -and $ProcessMap.ContainsKey($cursor)) {
        $process = $ProcessMap[$cursor]
        if ($process.Name -ieq 'Runner.exe') { $ids.Add($cursor) }
        $cursor = [int]$process.ParentProcessId
    }
    return @($ids)
}

$all = @(Get-CimInstance Win32_Process -Filter "name = 'Runner.exe'" | Where-Object {
    Test-RunnerBinary $_.ExecutablePath
})
$allRunnerProcesses = @(Get-CimInstance Win32_Process -Filter "name = 'Runner.exe'")
$updaterProcesses = @(Get-CimInstance Win32_Process -Filter "name = 'UpdateRunner.exe'" | Where-Object {
    if (-not $_.ExecutablePath) { return $false }
    try { [string]::Equals([IO.Path]::GetFullPath($_.ExecutablePath), [IO.Path]::GetFullPath((Join-Path $InstallDir 'UpdateRunner.exe')), [StringComparison]::OrdinalIgnoreCase) }
    catch { return $false }
})
$temporaryRunnerCopies = @(Get-CimInstance Win32_Process -Filter "name = 'Runner.exe'" | Where-Object {
    [string]$_.ExecutablePath -match '\\AppData\\Local\\Temp\\RunnerInstallerFresh-[^\\]+\\Runner\.exe$'
})
$authenticatedAgentPid = Get-AuthenticatedAgentPid
$agents = @($all | Where-Object { $_.CommandLine -match '(?i)(^|\s)--agent(\s|$)' })
if ($authenticatedAgentPid) {
    $agentRecord = Get-CimInstance Win32_Process -Filter "ProcessId = $authenticatedAgentPid" -ErrorAction SilentlyContinue
    if ($agentRecord -and $agentRecord.Name -ieq 'Runner.exe' -and $agents.ProcessId -notcontains $authenticatedAgentPid) {
        $agents += $agentRecord
    }
}
# Only processes with an actual window are GUI instances. The no-window
# PyInstaller bootloader parent must never receive CloseMainWindow().
$guis = @($all | Where-Object {
    $process = Get-Process -Id ([int]$_.ProcessId) -ErrorAction SilentlyContinue
    $process -and $process.MainWindowHandle -ne 0
})
foreach ($process in $all) {
    Write-UpgradeLog "Discovered Runner PID=$($process.ProcessId) parent=$($process.ParentProcessId) path='$($process.ExecutablePath)' command='$($process.CommandLine)' agent=$($process -in $agents)"
}
foreach ($process in $temporaryRunnerCopies) {
    Write-UpgradeLog "Observed temporary Runner copy (not an install target; will not terminate it) PID=$($process.ProcessId) path='$($process.ExecutablePath)' command='$($process.CommandLine)'"
}
foreach ($process in $updaterProcesses) {
    Write-UpgradeLog "Installed updater still active PID=$($process.ProcessId) path='$($process.ExecutablePath)' command='$($process.CommandLine)'"
}

function Get-LegacyAgentWrappers([object[]]$Applications) {
    # Some legacy one-file Runner launches have a blank executable path after
    # their source EXE has been moved/deleted.  Do not guess from process name
    # alone: identify only Runner.exe ancestors of known managed application
    # PIDs.  This lets us fence the obsolete supervisor without terminating
    # its Python/CMD children.
    $runnerProcesses = @(Get-CimInstance Win32_Process -Filter "name = 'Runner.exe'")
    $allProcesses = @(Get-CimInstance Win32_Process)
    $byPid = @{}
    foreach ($process in $allProcesses) { $byPid[[int]$process.ProcessId] = $process }
    $wrapperIds = [System.Collections.Generic.HashSet[int]]::new()
    $agentPid = Get-AuthenticatedAgentPid
    if ($agentPid) {
        foreach ($id in (Get-RunnerAncestors $agentPid $byPid)) { [void]$wrapperIds.Add($id) }
    }
    # Prefer the authenticated local Agent API. SYSTEM-owned Python/CMD
    # children often hide their command line from WMI, while the Agent's own
    # status endpoint provides the exact managed PIDs without secrets.
    $managedPids = [System.Collections.Generic.HashSet[int]]::new()
    $apiFile = Join-Path $RuntimeRoot 'agent-api.json'
    if (Test-Path -LiteralPath $apiFile) {
        try {
            $token = (Get-Content -LiteralPath $apiFile -Raw | ConvertFrom-Json).token
            $response = Invoke-RestMethod -Uri 'http://127.0.0.1:47471/v1/apps' -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
            foreach ($entry in @($response.applications)) {
                if ($entry.pid) { [void]$managedPids.Add([int]$entry.pid) }
            }
        } catch {
            # The API can already be down during recovery. The command-line
            # fallback below remains useful for ordinary user-owned processes.
        }
    }
    foreach ($managedPid in $managedPids) {
        $parentId = $managedPid
        $seen = [System.Collections.Generic.HashSet[int]]::new()
        while ($parentId -gt 0 -and $seen.Add($parentId)) {
            if (-not $byPid.ContainsKey($parentId)) { break }
            $parent = $byPid[$parentId]
            if ($parent.Name -ieq 'Runner.exe') { [void]$wrapperIds.Add($parentId) }
            $parentId = [int]$parent.ParentProcessId
        }
    }
    foreach ($application in $Applications) {
        $needle = [string]$application.runner_path
        if (-not $needle) { continue }
        foreach ($candidate in $allProcesses) {
            if ([string]$candidate.CommandLine -notlike "*$needle*") { continue }
            $parentId = [int]$candidate.ParentProcessId
            $seen = [System.Collections.Generic.HashSet[int]]::new()
            while ($parentId -gt 0 -and $seen.Add($parentId)) {
                if (-not $byPid.ContainsKey($parentId)) { break }
                $parent = $byPid[$parentId]
                if ($parent.Name -ieq 'Runner.exe') { [void]$wrapperIds.Add($parentId) }
                $parentId = [int]$parent.ParentProcessId
            }
        }
    }
    return @($runnerProcesses | Where-Object { $wrapperIds.Contains([int]$_.ProcessId) })
}

function Get-Apps([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($raw -is [array]) { return @($raw) }
        return @($raw.apps)
    } catch { return @() }
}

function Save-LegacyHandoff {
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    Remove-Item -LiteralPath (Join-Path $RuntimeRoot 'upgrade-verification-failed.json') -Force -ErrorAction SilentlyContinue
    $backup = Join-Path $RuntimeRoot ('config-backups\legacy-handoff-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    $target = Join-Path $RuntimeRoot 'apps.json'
    # A historical install may have used either the per-user V1/V3/V4 root
    # or the old portable Desktop layout.  Do not treat a new ProgramData
    # runtime as a valid empty migration source.
    $legacySources = @(
        (Join-Path $env:LOCALAPPDATA 'Runner_V4\apps.json'),
        (Join-Path $env:LOCALAPPDATA 'Runner_V3\apps.json'),
        (Join-Path $env:LOCALAPPDATA 'Runner_V2\apps.json'),
        (Join-Path $env:LOCALAPPDATA 'Runner_V1\apps.json'),
        (Join-Path $env:USERPROFILE 'Desktop\Runner\config\apps.json'),
        (Join-Path $env:USERPROFILE 'Desktop\Runner v1.4\src\config\apps.json'),
        # Available only in a source checkout; installed packages do not
        # contain source configuration and simply skip this candidate.
        (Join-Path $PSScriptRoot '..\..\src\config\apps.json')
    ) | Select-Object -Unique
    if (Test-Path -LiteralPath $target) { Copy-Item -LiteralPath $target -Destination (Join-Path $backup 'apps.before.json') -Force }
    # Preserve the machine-specific state that must survive a binary upgrade.
    # Logs and deployment/project folders are deliberately never moved or
    # rewritten by this upgrade path.
    foreach ($stateFile in @('identity.json', 'secrets.enc', 'secrets.key', 'cluster.json', 'onboarding.json', 'agent-api.json', 'agent-provisioning.json')) {
        $statePath = Join-Path $RuntimeRoot $stateFile
        if (Test-Path -LiteralPath $statePath) {
            Copy-Item -LiteralPath $statePath -Destination (Join-Path $backup $stateFile) -Force
        }
    }
    $tlsPath = Join-Path $RuntimeRoot 'tls'
    if (Test-Path -LiteralPath $tlsPath) {
        Copy-Item -LiteralPath $tlsPath -Destination (Join-Path $backup 'tls') -Recurse -Force
    }
    $sourceAppCount = 0
    foreach ($legacy in $legacySources) {
        if (Test-Path -LiteralPath $legacy) {
            Copy-Item -LiteralPath $legacy -Destination (Join-Path $backup ("apps.legacy." + [IO.Path]::GetFileName((Split-Path $legacy -Parent)) + ".json")) -Force
            $sourceAppCount += @(Get-Apps $legacy).Count
        }
    }

    $existingApps = @(Get-Apps $target)
    $expectedExistingCount = $existingApps.Count
    $merged = [ordered]@{}
    foreach ($app in $existingApps) { if ($app.id) { $merged[[string]$app.id] = $app } }
    # A valid ProgramData registry is authoritative.  Legacy data is a
    # recovery source only when ProgramData is empty; it must never append
    # stale projects to an already working production registry.
    if ($expectedExistingCount -eq 0) {
        foreach ($legacy in $legacySources) {
            foreach ($app in (Get-Apps $legacy)) {
                if ($app.id -and -not $merged.Contains([string]$app.id)) { $merged[[string]$app.id] = $app }
            }
        }
    }
    if (($sourceAppCount -gt 0 -or $expectedExistingCount -gt 0) -and $merged.Count -eq 0) {
        throw "Runner found $sourceAppCount legacy application records but recovered none. Upgrade stopped before an empty registry could be committed. Open the recovery backup at $backup."
    }
    if ($merged.Count -gt 0) {
        $payload = [ordered]@{ schema_version = 2; apps = @($merged.Values) }
        $temporary = Join-Path $RuntimeRoot ('.apps.handoff-' + [guid]::NewGuid().ToString('N') + '.tmp')
        $payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $target -Force
    }

    # This is diagnostic/reconciliation state only. It never grants start
    # authority, and contains no secrets.
    $apps = @(Get-Apps $target)
    if ($expectedExistingCount -gt 0 -and $apps.Count -ne $expectedExistingCount) {
        Copy-Item -LiteralPath (Join-Path $backup 'apps.before.json') -Destination $target -Force
        throw "Runner expected $expectedExistingCount existing applications but migration produced $($apps.Count). The prior registry was restored; upgrade stopped."
    }
    $agentTask = Get-ScheduledTask -TaskName 'Runner Agent' -ErrorAction SilentlyContinue
    $legacyStartupTask = Get-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction SilentlyContinue
    $agentTaskBackup = Join-Path $backup 'runner-agent-task.xml'
    $legacyTaskBackup = Join-Path $backup 'runner-autostart-task.xml'
    if ($agentTask) { Export-ScheduledTask -TaskName 'Runner Agent' | Set-Content -LiteralPath $agentTaskBackup -Encoding UTF8 }
    if ($legacyStartupTask) { Export-ScheduledTask -TaskName 'Runner Auto Start' | Set-Content -LiteralPath $legacyTaskBackup -Encoding UTF8 }
    # Compose retirement is intentional in this Runner build. The original
    # nine-entry registry is backed up above; post-upgrade verification must
    # expect only the process applications that this product still supports.
    $expectedPostUpgradeApps = @($apps | Where-Object { [string]$_.app_type -ne 'docker_compose' })
    @{ expected_app_count = $expectedPostUpgradeApps.Count; source_app_count = $apps.Count;
       registry_backup = (Join-Path $backup 'apps.before.json'); state_backup = $backup;
       agent_task_backup = $(if (Test-Path -LiteralPath $agentTaskBackup) { $agentTaskBackup } else { $null });
       legacy_task_backup = $(if (Test-Path -LiteralPath $legacyTaskBackup) { $legacyTaskBackup } else { $null });
       agent_task_exists = [bool]$agentTask; agent_task_was_enabled = [bool]($agentTask -and $agentTask.State -ne 'Disabled');
       agent_task_was_running = [bool]($agentTask -and $agentTask.State -eq 'Running');
       legacy_task_exists = [bool]$legacyStartupTask; legacy_task_was_enabled = [bool]($legacyStartupTask -and $legacyStartupTask.State -ne 'Disabled');
       captured_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'upgrade-preinstall.json') -Encoding UTF8
    $managed = @(Get-CimInstance Win32_Process | Where-Object {
        $line = [string]$_.CommandLine
        foreach ($app in $apps) {
            $runner = [string]$app.runner_path
            if ($runner -and $line.IndexOf($runner, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
        }
        return $false
    } | ForEach-Object { @{ pid=$_.ProcessId; parent_pid=$_.ParentProcessId; name=$_.Name; executable_path=$_.ExecutablePath; command_line=$_.CommandLine } })
    @{ captured_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); applications=@($apps | ForEach-Object { @{ id=$_.id; name=$_.name; runner_path=$_.runner_path; cwd=$_.cwd } }); managed_processes=$managed } |
        ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'legacy-handoff-processes.json') -Encoding UTF8
}

if ($Rollback) {
    $markerPath = Join-Path $RuntimeRoot 'upgrade-preinstall.json'
    if (Test-Path -LiteralPath $markerPath) {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ($marker.agent_task_exists) {
            $task = Get-ScheduledTask -TaskName 'Runner Agent' -ErrorAction SilentlyContinue
            if (-not $task -and $marker.agent_task_backup -and (Test-Path -LiteralPath $marker.agent_task_backup)) {
                $xml = Get-Content -LiteralPath $marker.agent_task_backup -Raw
                Register-ScheduledTask -TaskName 'Runner Agent' -Xml $xml -Force -ErrorAction Stop | Out-Null
                $task = Get-ScheduledTask -TaskName 'Runner Agent' -ErrorAction Stop
                Write-UpgradeLog 'Restored the pre-upgrade Runner Agent task definition.'
            }
            if ($marker.agent_task_was_enabled) {
                Enable-ScheduledTask -TaskName 'Runner Agent' -ErrorAction Stop | Out-Null
                if ($marker.agent_task_was_running) { Start-ScheduledTask -TaskName 'Runner Agent' -ErrorAction SilentlyContinue }
            } elseif ($task) {
                Disable-ScheduledTask -TaskName 'Runner Agent' -ErrorAction Stop | Out-Null
            }
        }
        if ($marker.legacy_task_exists) {
            $task = Get-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction SilentlyContinue
            if (-not $task -and $marker.legacy_task_backup -and (Test-Path -LiteralPath $marker.legacy_task_backup)) {
                $xml = Get-Content -LiteralPath $marker.legacy_task_backup -Raw
                Register-ScheduledTask -TaskName 'Runner Auto Start' -Xml $xml -Force -ErrorAction Stop | Out-Null
                $task = Get-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction Stop
                Write-UpgradeLog 'Restored the pre-upgrade Runner Auto Start task definition.'
            }
            if ($marker.legacy_task_was_enabled) { Enable-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction Stop | Out-Null }
            elseif ($task) { Disable-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction Stop | Out-Null }
        }
    }
    Remove-Item -LiteralPath (Join-Path $RuntimeRoot 'upgrade-in-progress.json') -Force -ErrorAction SilentlyContinue
    Write-UpgradeLog 'Installer rollback restored the prior Agent/startup task state.'
    exit 0
}

if ($DryRun) {
    $runtimeApps = @(Get-Apps (Join-Path $RuntimeRoot 'apps.json'))
    $legacyAgentWrappers = @(Get-LegacyAgentWrappers $runtimeApps)
    @{ runner_processes = @($all | ForEach-Object {
           $processId = [int]$_.ProcessId
           $gui = Get-Process -Id $processId -ErrorAction SilentlyContinue
           @{ pid = $processId; parent_pid=$_.ParentProcessId; path = $_.ExecutablePath; command_line = $_.CommandLine;
              role = if ($processId -eq $authenticatedAgentPid) { 'authenticated-agent' } elseif ($agents.ProcessId -contains $processId) { 'agent-wrapper' } elseif ($gui -and $gui.MainWindowHandle -ne 0) { 'gui-window' } elseif ($legacyAgentWrappers.ProcessId -contains $processId) { 'verified-runner-management-wrapper' } elseif ($all.ProcessId -contains $processId) { 'installed-gui-bootstrap-parent' } else { 'runner-helper-or-unclassified' } }
       });
       authenticated_agent_pid = $authenticatedAgentPid;
       agent_processes = $agents.Count; gui_windows = $guis.Count;
       legacy_agent_wrappers = @($legacyAgentWrappers | ForEach-Object { @{ pid=$_.ProcessId; parent_pid=$_.ParentProcessId; path=$_.ExecutablePath } }) } | ConvertTo-Json -Depth 4
    exit 0
}

# Persist configuration and capture managed PIDs while the legacy GUI is still
# alive. We never ask that GUI to close: its old close handler may terminate
# every application it owns.
Save-LegacyHandoff
$runtimeApps = @(Get-Apps (Join-Path $RuntimeRoot 'apps.json'))
$legacyAgentWrappers = @(Get-LegacyAgentWrappers $runtimeApps)

if ($ValidateOnly) {
    $apps = @(Get-Apps (Join-Path $RuntimeRoot 'apps.json'))
    @{ validated_app_count = $apps.Count; runtime_root = $RuntimeRoot } | ConvertTo-Json -Depth 4
    exit 0
}

# Mark maintenance so a replacement Agent can audit/reconcile, never auto-start.
@{ started_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); operation = 'binary_upgrade' } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'upgrade-in-progress.json') -Encoding UTF8

# Disable automatic relaunch while the binaries are being replaced. The Agent
# task is restarted by the installer only after the new files are in place.
$task = Get-ScheduledTask -TaskName 'Runner Agent' -ErrorAction SilentlyContinue
if ($task) {
    Disable-ScheduledTask -TaskName 'Runner Agent' -ErrorAction SilentlyContinue | Out-Null
}
# A stale old GUI auto-start task can resurrect an obsolete supervisor after
# the upgrade.  Disable it now; the new Agent task is installed only after
# binary replacement and verified health.
$legacyTask = Get-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction SilentlyContinue
if ($legacyTask) { Disable-ScheduledTask -TaskName 'Runner Auto Start' -ErrorAction SilentlyContinue | Out-Null }

# A normal GUI close can run the historical ProcessManager shutdown path and
# stop child applications. A frozen GUI can also ignore WM_CLOSE indefinitely.
# The registry and process handoff have been persisted above; now stop only
# verified Runner management image PIDs. Windows does not terminate their
# independent Python/CMD children when the parent exits.
$managedPids = @()
try {
    $handoffPath = Join-Path $RuntimeRoot 'legacy-handoff-processes.json'
    if (Test-Path -LiteralPath $handoffPath) {
        $handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
        $managedPids = @($handoff.managed_processes | ForEach-Object { [int]$_.pid })
    }
} catch { Write-UpgradeLog "Could not read managed process handoff: $($_.Exception.Message)" }
$runningManagedPids = @($managedPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
$managedChildrenPresent = $runningManagedPids.Count -gt 0
$unsafeParentIds = @($legacyAgentWrappers | ForEach-Object { [int]$_.ProcessId })
$guiBootstrapIds = [System.Collections.Generic.HashSet[int]]::new()
$processMap = @{}
foreach ($process in @(Get-CimInstance Win32_Process)) { $processMap[[int]$process.ProcessId] = $process }
foreach ($guiWindow in $guis) {
    foreach ($id in (Get-RunnerAncestors ([int]$guiWindow.ProcessId) $processMap)) { [void]$guiBootstrapIds.Add($id) }
}
$safeGuiPids = @($guis | Where-Object { $unsafeParentIds -notcontains [int]$_.ProcessId } | ForEach-Object { [int]$_.ProcessId })
foreach ($pidToClose in $safeGuiPids) {
    $liveGui = Get-CimInstance Win32_Process -Filter "ProcessId = $pidToClose" -ErrorAction SilentlyContinue
    if (-not $liveGui) { continue }
    if ($liveGui.Name -ine 'Runner.exe' -or -not (Test-RunnerBinary $liveGui.ExecutablePath)) {
        throw "Runner GUI PID $pidToClose changed identity during upgrade preparation. No process was stopped and no binaries were replaced. See $logPath."
    }
    Write-UpgradeLog "Stopping only verified Runner GUI PID=$pidToClose after persisted handoff; managed child PIDs preserved=$($runningManagedPids -join ',')"
    Stop-Process -Id $pidToClose -Force -ErrorAction Stop
}

# Legacy parent-only handoff is deliberately limited to Runner.exe processes
# already identified by executable path. Never kill by image name and never
# enumerate/terminate managed application processes.
$verifiedAgentPids = @($agents | Where-Object {
    # These records came from the installed Runner.exe path and either have
    # the explicit --agent role or are the PID returned by the authenticated
    # local Agent health endpoint.  They are Runner infrastructure, not app
    # processes.  Stopping them after Save-LegacyHandoff leaves their child
    # applications alive for the new Agent to reconcile.
    (Test-RunnerBinary $_.ExecutablePath) -and
    (($_.CommandLine -match '(?i)(^|\s)--agent(\s|$)') -or ([int]$_.ProcessId -eq [int]$authenticatedAgentPid))
} | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
$runnerOnlyPids = @($allRunnerProcesses | Where-Object {
    ($verifiedAgentPids -contains [int]$_.ProcessId) -or
    ($legacyAgentWrappers.ProcessId -contains $_.ProcessId) -or
    ($guiBootstrapIds.Contains([int]$_.ProcessId) -and $unsafeParentIds -notcontains [int]$_.ProcessId)
} | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
foreach ($runnerPid in $runnerOnlyPids) {
    if (Get-Process -Id $runnerPid -ErrorAction SilentlyContinue) {
        $role = if ($verifiedAgentPids -contains $runnerPid) { 'verified Agent' } elseif ($legacyAgentWrappers.ProcessId -contains $runnerPid) { 'legacy management wrapper' } else { 'GUI bootstrap parent' }
        Write-UpgradeLog "Stopping only $role PID=$runnerPid after persisted handoff; managed child PIDs preserved=$($runningManagedPids -join ',')"
        Stop-Process -Id $runnerPid -Force -ErrorAction Stop
    }
}

# Bounded post-close verification. Refuse to proceed if Runner is still alive;
# never offer a skip path or attempt to replace a mapped executable.
$deadline = [Diagnostics.Stopwatch]::StartNew()
do {
    Start-Sleep -Milliseconds 200
    $locked = @(Get-CimInstance Win32_Process -Filter "name = 'Runner.exe'" | Where-Object {
        (Test-RunnerBinary $_.ExecutablePath) -or ($runnerOnlyPids -contains [int]$_.ProcessId)
    })
} while ($locked.Count -gt 0 -and $deadline.Elapsed.TotalSeconds -lt 12)

if ($locked.Count -gt 0) {
    $details = (@($locked | ForEach-Object { "PID=$($_.ProcessId) path='$($_.ExecutablePath)' command='$($_.CommandLine)'" }) -join '; ')
    Write-UpgradeLog "Preflight refused replacement; remaining process(es): $details"
    throw "Runner is still using its installed executable. $details Close Runner and retry. No application or container was stopped and no Runner files were replaced. See $logPath."
}
if ($updaterProcesses.Count -gt 0) {
    $details = (@($updaterProcesses | ForEach-Object { "PID=$($_.ProcessId) path='$($_.ExecutablePath)'" }) -join '; ')
    throw "The installed UpdateRunner is still running and owns its executable. $details The update bootstrap must release it before Setup starts. No Runner files were replaced. See $logPath."
}

# Check the actual replacement targets for non-process locks (for example a
# scanner or another updater). If exclusive access cannot be obtained, stop
# before Inno reaches its file-copy phase; never offer to skip a binary.
foreach ($fileName in @('Runner.exe', 'UpdateRunner.exe')) {
    $filePath = Join-Path $InstallDir $fileName
    if (-not (Test-Path -LiteralPath $filePath)) { continue }
    try {
        $stream = [IO.File]::Open($filePath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $stream.Dispose()
        Write-UpgradeLog "Exclusive replacement check passed path='$filePath'"
    } catch {
        $owners = @(Get-RestartManagerOwners $filePath)
        $ownerText = if ($owners.Count) { $owners -join ', ' } else { 'Windows did not identify a registered lock owner' }
        Write-UpgradeLog "Exclusive replacement check failed path='$filePath' owners='$ownerText' reason='$($_.Exception.Message)'"
        throw "Runner cannot safely replace $filePath because another process still has it open. Lock owner: $ownerText. Close that process and retry. No Runner files were replaced. See $logPath."
    }
}

@{ runner_processes_closed = $all.Count; verified_agent_processes_closed = $verifiedAgentPids.Count; legacy_agent_wrappers_closed = $legacyAgentWrappers.Count;
   temporary_processes_closed = @($all | Where-Object { $_.ExecutablePath -match 'RunnerInstallerFresh-' }).Count } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'last-upgrade-handoff.json') -Encoding UTF8
Write-UpgradeLog "Runner process preflight complete; managed applications were preserved."
