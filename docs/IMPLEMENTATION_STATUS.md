# Implementation Status

## Implemented and tested

- Backward-compatible schema migration with backup and safe defaults.
- Independent Agent mode and authenticated localhost controller API.
- GUI/Agent ownership separation in cluster mode.
- Stable UUID identity, encrypted secret store, one-use pairing primitive, request signing, replay protection, and log redaction.
- Transactional witness lease store with expiry, renewal, release, and permanent fencing epochs.
- Monotonic self-fencing controller and protected-process start gate.
- Startup recovery that fences protected orphan processes rather than assuming prior authority.
- Explicit failover states, dependency ordering/cycle detection, process/TCP/HTTP health checks, and readiness blockers.
- Checksum manifests, safe paths, staged verification, and atomic deployment activation.
- Explicit persistence strategies; live SQLite is blocked from ordinary-file-sync readiness.
- Windows/Linux platform adapters, Linux build script, hardened systemd unit, and Windows at-boot Agent task.
- Tailscale transport and Cloudflare ingress provider boundaries.
- Structured cluster audit records.
- Authenticated paired-node HTTPS API with protocol checks, replay protection, remote status/logs, handoff, and sync endpoints.
- Continuous heartbeat/synchronization PeerService independent of the GUI, with dynamic peer activation immediately after pairing.
- Pairing GUI (one-time code, peer details, automatic failover/failback controls, explicit unpair protection).
- Authoritative readiness engine covering deployment paths, runtime/platform compatibility, dependencies, persistence, secrets, health-check configuration, protocol, witness, and disk prerequisites.
- Automatic failover/retry and controlled failback/manual Transfer Here orchestration; standby Start is routed through handoff and cannot duplicate-start a protected app.
- Independent process guard that fences a protected child if Agent authorization deadlines stop advancing.
- Tailscale CLI identity/certificate discovery and Cloudflare Tunnel connector execution boundary.
- Two-independent-Agent lifecycle integration harness covering takeover, rejoin/failback, manual transfer, and single-owner fencing.
- Windows Inno Setup installer, automatic boot Agent task, Tailscale-scoped firewall rule, repair action, uninstall data-retention choice, first-run role wizard, and clean portable ZIP build.

## Verification boundaries

The local two-Agent network lifecycle is operational and tested. Physical deployment boundaries remain:

- No second Ubuntu node or independent witness was available for an actual cross-ISP failover drill.
- Network synchronization is operational for changed manifest versions with staged checksum verification; chunk-level resume/bandwidth control is not included.
- A deployed witness still requires the operator to provision its TLS certificate and per-node HMAC credentials.
- Cloudflare Tunnel execution is implemented and mockable locally; real Cloudflare account/API validation was not possible here.
- Database-specific replication drivers for PostgreSQL/MySQL/SQLite are not included. Such applications remain not ready unless an external/replicated/custom strategy proves readiness.
- Linux packaging scripts are present but were not executed on Ubuntu in this Windows environment.
- `Runner.exe`, `UpdateRunner.exe`, the Windows installer, and the clean portable ZIP were rebuilt from the integrated source.
- Fresh administrator-level installation, upgrade installation, firewall rule creation, and scheduled-task execution require an elevated Windows test host; this session compiled the installer and tested its contents but could not approve UAC.
- A real cross-ISP Tailscale/Linux failover drill remains unverified.

Because these items are material, automatic failover defaults to disabled and existing applications default to unprotected standalone operation. The UI and models must not label a backup Ready merely because its process files exist.
# Release updates

Signed-manifest update discovery, SHA-256 package verification, runtime backup,
Windows in-place installer execution/rollback, Agent restart, Update/About UI,
and HA active-node update blocking are implemented and covered by local tests.
No public release manifest/source is configured in this workspace, so online
automatic updates remain intentionally unclaimed and disabled by default.
