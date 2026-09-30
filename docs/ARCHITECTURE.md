# Architecture

## Components

```text
Runner GUI -> localhost authenticated Agent API -> Runner Agent -> local processes
                                                    |
                                                    +-> HTTPS witness lease service
                                                    +-> ClusterTransport (Tailscale initially)
                                                    +-> synchronization/deployment store
                                                    +-> optional IngressProvider
                                                    +-> paired peer heartbeat/sync service
```

## Docker Compose deployments

Docker Compose is a deployment provider, not a shell-command application type.
`DockerComposeDeploymentProvider` is the only component that talks to the
local Docker CLI/Engine; the authenticated Agent owns the decision to invoke
it.  The remote Runner protocol never exposes the Docker socket or arbitrary
Compose paths.

The provider validates a Compose project below an approved deployment root,
loads structured `docker compose config --format json`, discovers services,
dependencies, mounts, image references, ports, container state and health.
It supports `pull`, optional `build`, `up -d`, `stop`, restart and redacted
service logs.  It deliberately never calls `docker compose down`, deletes a
volume, or controls a Compose project outside the configured root.

For a protected Compose deployment, the normal Agent ownership gate applies
before `up -d`.  The standby preparation path can synchronize/validate files
and pull/build images, but it refuses to run while any project container is
running.  Loss of lease calls the same self-fencing stop path.  ACTIVE is set
only once each required service is running and any declared Compose health
check is healthy; healthless services are reported as running/health unknown.

Compose mounts are classified as stateless, syncable files, external/shared,
database/stateful, or unknown.  Unknown and database/stateful mounts block
standby readiness.  Secrets are declared by name and supplied from encrypted
Runner secrets; `.env` is excluded from synchronization.

## Release updates

The GUI checks an optional signed HTTPS release manifest outside the Agent's
process-control channel. A verified package is handed to the small local
`UpdateRunner` helper only after signature and SHA-256 verification. Machine
runtime data is stored separately from installed binaries and is backed up,
never bundled or overwritten. Cluster status is consulted before update: an
ACTIVE node is not restarted by an update workflow.

Qt is a presentation layer. Cluster state, authentication, synchronization, leases, health, and state machines are independent Python services and can be tested without widgets.

The Agent starts `RemoteAgentServer` only with a configured TLS certificate (Tailscale certificates can be obtained automatically through the installed CLI). `PeerService` signs every heartbeat, status, handoff, and synchronization request and refreshes endpoint/trust maps after pairing; the GUI is not involved in liveness or failover.

`PlatformAdapter` contains process-command, service, interpreter, console, and tree-termination differences. Windows-specific extensions are rejected as unsupported Linux deployments rather than failing during takeover.

## Ownership invariant

For every protected service group, a process start passes through the Agent's ownership authorizer. The authorizer requires a locally current lease for the exact service group. GUI commands, legacy auto-start, restarts, and Agent recovery cannot bypass this gate.

The witness performs atomic acquire/renew/release operations. Epochs never decrease or get reused. Agents do not use wall clocks to decide how long they may continue: the conservative authorization deadline is derived from local monotonic time at the beginning of the successful witness request.

If renewal cannot be proven before that deadline, the Agent force-stops the protected process tree and records `self_fence_initiated`.

## Failover

The deterministic states are `PRIMARY_HEALTHY`, `PRIMARY_SUSPECT`, `WAIT_FOR_LEASE`, `VERIFY_BACKUP_READY`, `START_DEPENDENCIES`, `START_APPLICATIONS`, `VERIFY_HEALTH`, and `BACKUP_ACTIVE`. Every transition is validated. Failure enters `ERROR` rather than skipping a prerequisite.

Direct A-to-B loss does not permit takeover while A still renews the witness lease. If the witness is unreachable, B cannot acquire authority. This intentionally trades availability for duplicate-execution safety.

## Failback

A returning preferred primary remains standby. The state moves through resynchronization and readiness before handoff. The active node stops applications in reverse dependency order and releases leases. Only then may the preferred primary acquire newer epochs and start in dependency order. A failed readiness check leaves the healthy current active node in place.

## Synchronization and persistence

File manifests contain relative paths, sizes, modes, and SHA-256 checksums. Transfers stage into a temporary deployment, reject path traversal, verify the complete manifest, and atomically activate an immutable version.

Persistence is explicitly classified as stateless, external, SQLite, replicated, custom, or unsupported. SQLite is not considered failover-ready through file synchronization. Custom persistence remains unready until an application-specific checker proves otherwise.

Continuous sync is owner-directed: the active Agent sends changed manifest versions to paired standbys. The standby verifies every entry in a staging directory, atomically activates the immutable version, and updates its node-specific runnable deployment path. Runtime logs, caches, virtual environments, and unsafe live database paths are excluded by default.

Readiness is calculated by one `ReadinessEngine` for both UI and takeover. A protected process is launched through an independent process guard; if the Agent cannot refresh its lease deadline, the guard terminates the process tree even if the Agent itself has crashed.

## Protocol compatibility

Heartbeats carry a protocol version and monotonically increasing sequence. Incompatible versions are rejected. Heartbeat age is measured from local monotonic receipt time, so remote clock drift cannot manufacture failure evidence.
