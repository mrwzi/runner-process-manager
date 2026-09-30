# Operations

## Debian 13 headless Agent

Install the amd64 package with `sudo apt install ./runner-agent_4.1.0_amd64.deb`.
It installs `runner-agent.service`, creates the unprivileged `runner` user,
enables and starts the Agent, and keeps its state in `/var/lib/runner`.
No desktop environment, PySide6, pip, or source checkout is required.

Run `sudo runner-agent setup --role backup`, sign in to Tailscale if needed,
then use **Servers: Add Backup Server** on the Windows controller. If a code
is shown there, the Debian confirmation is:

`sudo runner-agent pair --endpoint https://PRIMARY.tailnet.ts.net:47473 --code CODE`

Use `runner-agent status`, `runner-agent test-connection`, and
`runner-agent repair` for normal diagnostics. Package removal intentionally
retains `/var/lib/runner`, including node identity, pairing credentials,
encrypted secrets, logs, and synchronized deployments. Transfer ownership
before removing an active node.

## Updates

Use **Updates** to configure a signed HTTPS release manifest and review the
release notes. Runner defaults to checking only when a source is configured;
it does not ship with a public source in this build. Update a healthy standby
first. The GUI blocks a self-update of an ACTIVE node until ownership has been
safely transferred through the normal HA workflow. See [UPDATES.md](UPDATES.md).

## Windows Agent

`RunnerSetup-4.1.0.exe` installs and starts the `Runner Agent` scheduled task automatically. It runs at boot as SYSTEM, survives GUI closure, and stores its state under `%ProgramData%\Runner_V4`. The installer creates a scoped inbound rule for TCP 47473 from Tailscale addresses only; it does not open a public-internet rule. Use **Servers: Repair Agent & Firewall** if the task or rule is damaged.

## Linux Agent

`build/scripts/linux/package.sh` creates the Debian package that installs Runner under `/opt/runner`, creates an unprivileged `runner` account, creates `/var/lib/runner` with mode 0700, and enables `runner-agent.service`. The systemd unit restarts on failure and restricts filesystem writes to the runtime directory.

## Tailscale

Tailscale provides routability and device-level encrypted transport. Runner still authenticates paired node identities at its own protocol layer. Node UUIDs are authoritative; Tailscale addresses are metadata and may change.

Agent and witness endpoints exposed beyond localhost must use HTTPS. Router port forwarding is not required when both machines and the witness are reachable through the tailnet.

The first-run wizard detects the installed client, connection state, hostname and peers. If it is missing, **Install Tailscale** opens the official download flow; **Connect Tailscale** starts the normal sign-in flow. The normal pairing picker uses detected computer names, not `100.x` addresses. IPs remain available only in technical details.

## Pairing and protection

Start the Agent on both machines and open `Runner` on the preferred primary. In `Cluster / Servers`, choose `Create code for this server`; on the backup choose `Pair`, enter the displayed Tailscale HTTPS address and one-time code, and wait for the peer to show `Connected`. The code is single-use and expires after five minutes. The peer relationship is stored in the encrypted secret store; removing a peer is refused while it owns a protected service group.

Mark an application `Cluster Protected`, configure its dependency and health-check settings, and provide a deployment entry for the backup (the normal sync path creates this after the first verified activation). A standby's `Ready` state requires synchronization, runtime/platform support, persistence safety, secrets, dependencies, and witness readiness; it is not based on files alone. Enable `Automatic failover` only after the Servers dialog reports no prerequisite reasons. Failover and failback run without the GUI.

## Container support

Docker/Compose management is not part of Runner 4.1.0. Legacy Compose records are inert and ignored; Runner does not invoke the Docker CLI or Docker Engine API.

## Witness service

A witness can run from the packaged Runner binary on a third failure domain. Initialize one credential for each node, then start the HTTPS witness service:

Runner.exe --witness --runtime-root C:\RunnerWitness --initialize-node <node-uuid>
Runner.exe --witness --runtime-root C:\RunnerWitness --certificate C:\tls\witness.crt --private-key C:\tls\witness.key

Configure the resulting witness URL and each node's encrypted witness_shared_secret on the corresponding Agent. The witness stores lease state only, not project files. Keep it independent of both application nodes; do not place it on either node if that would share their failure domain.

## Logs

- Application logs: `<runtime>/logs/<application>.log`
- Cluster audit: `<runtime>/logs/cluster-audit.jsonl`
- systemd: `journalctl -u runner-agent`
- Windows scheduled task: Task Scheduler history for `Runner V4 Server Startup`

Audit events include timestamp, node ID, event, application/service group, fencing context, and reason. Secrets are redacted.

## Recovery

Agent restart never restores authority from memory or a stale GUI. In cluster mode it stops discovered protected processes until authority is proven again. A stale node reconnecting with an old epoch cannot renew, release, start, or change ingress for a newer owner.

Configuration backups are under `<runtime>/config-backups`. Restore only while the Agent is stopped, and do not restore lease database files onto multiple witnesses.

For diagnostics, inspect `cluster-audit.jsonl` for `automatic_failover_completed`, `self_fence_initiated`, `handoff_completed`, `sync_completed`, and `cluster_orchestration_deferred`. A protected process with a stopped Agent is fenced by its independent guard when the last monotonic authorization deadline expires.
