# Runner

Runner is a cross-platform process Agent and desktop control center. It preserves the v1.4 standalone workflow while adding an opt-in, lease-fenced active/standby architecture for Windows and Linux.

## Normal installation

Download the [Runner v4.1.0 installer](https://github.com/mrwzi/runner-process-manager/releases/download/v4.1.0/RunnerSetup-4.1.0.exe) from GitHub Releases. It includes Python and all required dependencies, installs the background Runner Agent, creates its Tailscale-only firewall rule, and opens a first-run choice: **Standalone computer**, **Primary server**, or **Backup server**. Existing configuration, identities, secrets, logs, and cluster data are preserved during an upgrade.

For a portable copy, download [Runner v4.1.0 Portable](https://github.com/mrwzi/runner-process-manager/releases/download/v4.1.0/Runner-v4.1.0-portable.zip). It intentionally contains no node identity, secrets, certificates, logs, or runtime configuration; every computer creates its own identity at first run.

For **Debian 13 headless Agent/backup nodes**, download the [amd64 package](https://github.com/mrwzi/runner-process-manager/releases/download/v4.1.0/runner-agent_4.1.0_amd64.deb) or [ARM64 package](https://github.com/mrwzi/runner-process-manager/releases/download/v4.1.0/runner-agent_4.1.0_arm64.deb). These packages install the headless Agent, not the Windows desktop controller.

## Backup server and high availability

Choose **Primary server** for the preferred node and **Backup server** for its standby during setup. Pair them through **Servers** using the one-time code and Tailscale HTTPS endpoint. High availability also needs a separately configured witness on an independent failure domain, a ready deployment on the backup, and passing health/readiness checks. Automatic failover is off by default; see [Operations](docs/OPERATIONS.md) and [Implementation Status](docs/IMPLEMENTATION_STATUS.md) before enabling it. A passing local test is not a substitute for testing the actual network and storage setup.

## Modes

- **Standalone** is the backward-compatible default. The desktop controller owns local processes and existing `auto_start` behavior is unchanged.
- **Cluster** requires the background Agent. The GUI talks to the local Agent and closing the GUI does not stop Agent-owned services. Protected applications may run only while the Agent holds a current witness lease.

Existing `apps.json` files are backed up and migrated to schema 2. Migration never enables cluster mode, protection, synchronization, or automatic failover.

## Safety model

The authoritative witness stores one lease per service group in SQLite using `BEGIN IMMEDIATE` transactions. Grants have a random lease ID, witness-clock expiry, and monotonically increasing fencing epoch. An expired lease cannot be renewed; a stale lease cannot release a newer grant. Agents use a conservative local monotonic deadline and self-fence before their authority can overlap a later grant.

A missing peer heartbeat is only failure evidence. It never grants authority. Takeover still requires witness acquisition, compatible protocol versions, a ready deployment, synchronized files when configured, an acceptable persistence strategy, dependency readiness, and passing health checks.

See [Architecture](docs/ARCHITECTURE.md), [Operations](docs/OPERATIONS.md), and [Security](docs/SECURITY.md).

The implemented/remaining production scope is recorded explicitly in [Implementation Status](docs/IMPLEMENTATION_STATUS.md). Cluster mode remains opt-in and must not be represented as ready until its external witness, peer endpoints, deployment mappings, synchronization, and persistence checks are configured and exercised on the real nodes.

## Supported applications

Windows supports Python, Node.js, Batch/CMD, and native executables. Linux supports Python, Node.js, shell scripts, and native executables. Deployment readiness is node-specific; a Windows-only application is not advertised as ready on Linux.

Health checks support process existence, TCP, HTTP, and HTTPS. Plain file copying is deliberately rejected as a safe replication strategy for live SQLite databases.

## Development

```powershell
python -m pip install -r build\scripts\requirements.txt
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python src\launcher\run.py
```

Start an Agent from source:

```powershell
python src\launcher\run.py --agent --runtime-root .runner_runtime
```

Build Windows Runner:

```powershell
powershell -ExecutionPolicy Bypass -File build\scripts\build.ps1
```

Linux packaging uses `build/scripts/linux/runner-agent.service` and `build/scripts/linux/package.sh`.

## Runtime data

Runtime configuration, encrypted secrets, identities, deployment manifests, audit records, and application logs live beneath the selected runtime root. `logs/cluster-audit.jsonl` contains structured failover and fencing events. Secret values are redacted from normal logs.
