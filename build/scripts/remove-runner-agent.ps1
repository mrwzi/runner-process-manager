$ErrorActionPreference = 'SilentlyContinue'
Stop-ScheduledTask -TaskName 'Runner Agent'
Unregister-ScheduledTask -TaskName 'Runner Agent' -Confirm:$false
Remove-NetFirewallRule -DisplayName 'Runner Agent (Tailscale)'
