from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

from manager import ProcessManager

from .config import ConfigStore
from .audit import AuditLog
from .dependency import shutdown_order, startup_order
from .health import HealthChecker
from .models import AgentMode, HealthState, NodeRole, Readiness, SyncStatus
from .models import ConnectivityState, Heartbeat, Lease, PROTOCOL_VERSION
from .heartbeat import HeartbeatMonitor
from .ownership import OwnershipController
from .security import IdentityStore, redact_secrets
from .secrets_store import SecretStore
from .pairing import PairingManager
from .sync import persistence_ready
from .readiness import ReadinessEngine
from .ingress import CloudflareTunnelProvider
from .version import AGENT_VERSION


LOGGER = logging.getLogger("runner.agent")
class RunnerAgent:
    """Long-running owner of processes, leases and cluster safety decisions."""

    def __init__(self, runtime_root: Path, coordinator=None) -> None:
        self.runtime_root = runtime_root
        self.store = ConfigStore(runtime_root)
        migrated = self.store.migrate_apps()
        self.cluster = self.store.load_cluster()
        self.identity = IdentityStore(runtime_root).load_or_create()
        self.secret_store = SecretStore(runtime_root)
        self.pairing = PairingManager(self.identity["node_id"], self.identity["public_fingerprint"], self.secret_store)
        if not self.cluster.node_id:
            self.cluster.node_id = self.identity["node_id"]
            self.store.save_cluster(self.cluster)
        self.manager = ProcessManager(migrated["apps"], logs_dir=runtime_root / "logs")
        self.config_lock = threading.RLock()
        self._prepare_existing_local_deployments()
        self.manager.set_start_authorizer(self._authorize_start)
        self.health_checker = HealthChecker()
        self.ingress = CloudflareTunnelProvider({
            app["id"]: dict(app.get("ingress", {})) for app in self.manager.export_apps()
            if app.get("ingress", {}).get("provider") == "cloudflare_tunnel"
        })
        self.role = NodeRole.STANDBY if self.cluster.enabled else NodeRole.ACTIVE
        self.sync_status: dict[str, SyncStatus] = {}
        self.heartbeat_monitor = HeartbeatMonitor(self.cluster.suspect_after_seconds)
        self.peer_status: dict[str, dict[str, Any]] = {}
        self.transport = None
        self._heartbeat_sequence = 0
        self.last_transition_reason = "Agent initialized"
        self.suppress_reacquire_until = 0.0
        self._readiness_cache: dict[str, Readiness] = {}
        self._readiness_lock = threading.RLock()
        self._readiness_stop = threading.Event()
        self._readiness_thread: threading.Thread | None = None
        self.audit = AuditLog(runtime_root / "logs" / "cluster-audit.jsonl", self.cluster.node_id)
        self.audit.write("agent_started", mode=str(self.cluster.mode), agent_version=AGENT_VERSION)
        if self.cluster.enabled and coordinator is None and self.cluster.witness_url:
            from .coordinator import HttpCoordinator

            encoded_secret = SecretStore(runtime_root).get("witness_shared_secret")
            if encoded_secret:
                import base64
                coordinator = HttpCoordinator(
                    self.cluster.witness_url,
                    self.cluster.node_id,
                    base64.urlsafe_b64decode(encoded_secret),
                )
        self.ownership: OwnershipController | None = None
        if self.cluster.enabled and coordinator is not None:
            self.ownership = OwnershipController(
                coordinator,
                self.cluster.node_id,
                ttl=self.cluster.lease_ttl_seconds,
                renew_interval=self.cluster.lease_renew_seconds,
                fence_margin=self.cluster.fence_margin_seconds,
                on_fence=self._self_fence,
                on_authority=self._publish_guard_deadline,
            )
            self.ownership.start()
        self.readiness_engine = ReadinessEngine(self)
        if self.cluster.enabled and any(app.get("protected") for app in self.manager.export_apps()):
            self._readiness_thread = threading.Thread(
                target=self._readiness_loop, name="runner-readiness-refresh", daemon=True
            )
            self._readiness_thread.start()

    def start_configured(self) -> None:
        if self.cluster.mode == AgentMode.STANDALONE:
            self.manager.start_auto_start_apps()
            return
        # A binary-only Agent restart may retain a valid witness lease. Adopt
        # that exact lease before touching the process; stale PIDs never count
        # as authority and remain fenced.
        self.manager.refresh_all()
        time.sleep(0.5)
        self._reconcile_legacy_handoff()
        for app in self.manager.export_apps():
            if app.get("protected"):
                snapshot = self.manager.snapshot(app["id"])
                if snapshot["status"] in {"Running", "Already Running", "Waiting Input", "Starting"}:
                    group = str(app.get("service_group") or app["id"])
                    adopted = False
                    if self.ownership:
                        try:
                            lease = self.ownership.coordinator.current(group)
                            if lease and lease.owner_node_id == self.cluster.node_id:
                                self.ownership.adopt(lease)
                                adopted = True
                                self.audit.write("upgrade_reconciled", application_id=app["id"], lease_epoch=lease.epoch)
                        except Exception as exc:
                            self.audit.write("upgrade_reconcile_failed", application_id=app["id"], reason=str(exc))
                    if not adopted:
                        self.manager.force_stop_app(app["id"])
                        self.audit.write("startup_self_fence", application_id=app["id"], reason="Agent restarted without proven lease")
        # Cluster-protected applications never inherit legacy auto-start authority.
        for app in self.manager.export_apps():
            if app.get("auto_start") and not app.get("protected"):
                self.manager.start_app(app["id"])

    def _reconcile_legacy_handoff(self) -> None:
        """Audit, but never trust, the PID snapshot made before legacy GUI exit."""
        path = self.runtime_root / "legacy-handoff-processes.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            expected = {int(item["pid"]) for item in payload.get("managed_processes", []) if item.get("pid")}
            observed = {
                int(self.manager.snapshot(app["id"]).get("pid") or 0)
                for app in self.manager.export_apps()
                if self.manager.snapshot(app["id"]).get("status") in {"Running", "Already Running", "Waiting Input", "Starting"}
            }
            self.audit.write("legacy_upgrade_reconciled", expected_pids=sorted(expected),
                             retained_pids=sorted(expected & observed), missing_pids=sorted(expected - observed))
            (self.runtime_root / "upgrade-in-progress.json").unlink(missing_ok=True)
        except Exception as exc:
            self.audit.write("legacy_upgrade_reconcile_failed", reason=str(exc))

    def _prepare_existing_local_deployments(self) -> None:
        """Record an existing local project as a node-specific deployment.

        This preserves upgraded v1.4 installations: enabling protection does not
        require users to re-import a project that is already runnable locally.
        """
        changed = False
        for app in self.manager.export_apps():
            if not app.get("protected"):
                continue
            deployments = dict(app.get("deployments", {}))
            if self.cluster.node_id in deployments:
                continue
            cwd = Path(str(app.get("cwd") or ""))
            runner = Path(str(app.get("runner_path") or ""))
            is_compose = app.get("app_type") == "docker_compose"
            supported = is_compose or not (platform.system() != "Windows" and runner.suffix.lower() in {".bat", ".cmd", ".exe"})
            if is_compose:
                from manager.deployments import DockerComposeDeploymentProvider
                try:
                    provider = DockerComposeDeploymentProvider({"id": app["id"], "cwd": str(cwd), **dict(app.get("compose") or {})})
                    provider.validate()
                    ready = provider.availability().engine and provider.availability().compose
                except Exception:
                    ready = False
            else:
                ready = bool(supported and cwd.is_dir() and runner.is_file())
            deployments[self.cluster.node_id] = {
                "supported": supported, "ready": ready,
                "cwd": str(cwd), "runner_path": str(runner), "prepared_at": time.time(),
            }
            app["deployments"] = deployments
            self.manager.update_app(app["id"], app)
            changed = True
        if changed:
            from .config import atomic_json_write
            atomic_json_write(self.store.apps_path, {"schema_version": 2, "apps": self.manager.export_apps()})

    def acquire_and_start(self, app_ids: list[str]) -> tuple[bool, str]:
        apps = self.manager.export_apps()
        by_id = {app["id"]: app for app in apps}
        try:
            order = startup_order(apps, set(app_ids))
        except ValueError as exc:
            return False, str(exc)
        protected_groups = {by_id[app_id]["service_group"] for app_id in order if by_id[app_id].get("protected")}
        if protected_groups and self.ownership is None:
            return False, "Cluster coordinator is unavailable; protected applications remain fenced"
        readiness = self.readiness(order)
        if not readiness.ready:
            return False, "; ".join(readiness.reasons)
        acquired: list[str] = []
        try:
            for group in sorted(protected_groups):
                if not self.ownership.authorized(group):
                    self.ownership.acquire(group)
                    acquired.append(group)
            self.role = NodeRole.TAKING_OVER
            for app_id in order:
                # This code runs in the Agent orchestration worker, never the
                # Qt thread.  A synchronous launch prevents a lease handoff
                # from racing an asynchronous GUI action queue.
                self.manager.start_app_for_agent(app_id)
                if not self._wait_for_running(app_id, float(by_id[app_id].get("startup_timeout_seconds", 60))):
                    raise RuntimeError(f"{by_id[app_id]['name']} did not become healthy before its startup timeout")
            for app_id in order:
                if by_id[app_id].get("ingress", {}).get("enabled"):
                    group = str(by_id[app_id].get("service_group") or app_id)
                    lease = self.ownership.lease(group) if self.ownership else None
                    if not lease:
                        raise RuntimeError(f"Cannot activate ingress for {by_id[app_id]['name']} without a current lease")
                    self.ingress.activate(app_id, self.cluster.node_id, lease.epoch)
            self.role = NodeRole.ACTIVE
            self.last_transition_reason = "Ownership acquired and application health verified"
            self.audit.write("takeover_completed", applications=order, groups=sorted(protected_groups))
            return True, self.last_transition_reason
        except Exception as exc:
            for app_id in reversed(order):
                app = by_id[app_id]
                if app.get("ingress", {}).get("enabled") and self.ownership:
                    lease = self.ownership.lease(str(app.get("service_group") or app_id))
                    if lease:
                        try:
                            self.ingress.deactivate(app_id, self.cluster.node_id, lease.epoch)
                        except Exception:
                            pass
            for app_id in reversed(order):
                self.manager.stop_app(app_id)
            for group in acquired:
                try:
                    self.ownership.release(group)
                except Exception:
                    pass
            self.role = NodeRole.STANDBY
            self.last_transition_reason = str(exc)
            self.audit.write("takeover_failed", applications=order, reason=str(exc))
            return False, str(exc)

    def transfer_out(self, app_ids: list[str]) -> tuple[bool, str]:
        apps = self.manager.export_apps()
        by_id = {app["id"]: app for app in apps}
        self.role = NodeRole.HANDING_BACK
        # Give the target a bounded ownership handoff window. Without this,
        # an auto-start preferred node could reacquire immediately after it
        # released the witness lease, racing the intended target. Arm it now
        # to cover shutdown, then re-arm after shutdown because graceful stop
        # can consume the initial window before the lease is actually released.
        handoff_window = max(2.0, self.cluster.heartbeat_interval_seconds * 10)
        self.suppress_reacquire_until = time.monotonic() + handoff_window
        for app_id in app_ids:
            app = by_id.get(app_id, {})
            if app.get("ingress", {}).get("enabled") and self.ownership:
                group = str(app.get("service_group") or app_id)
                lease = self.ownership.lease(group)
                if lease:
                    self.ingress.deactivate(app_id, self.cluster.node_id, lease.epoch)
        for app_id in shutdown_order(apps, set(app_ids)):
            self.manager.stop_app(app_id)
            self._wait_for_stopped(app_id, 15)
        # The target has already completed readiness checks before requesting
        # release. Start its acquisition window at the actual release edge,
        # not at the beginning of a potentially slow graceful shutdown.
        self.suppress_reacquire_until = max(
            self.suppress_reacquire_until,
            time.monotonic() + handoff_window,
        )
        groups = {by_id[app_id]["service_group"] for app_id in app_ids if by_id[app_id].get("protected")}
        for group in groups:
            if self.ownership and not self.ownership.release(group):
                self._self_fence(group, "Lease release could not be confirmed")
                return False, f"Could not safely release ownership for {group}"
            self._guard_path(group).unlink(missing_ok=True)
        self.role = NodeRole.STANDBY
        self.last_transition_reason = "Applications stopped and leases released"
        self.audit.write("handoff_completed", applications=app_ids, groups=sorted(groups))
        return True, self.last_transition_reason

    def readiness(self, app_ids: list[str]) -> Readiness:
        result = self.readiness_engine.evaluate(app_ids)
        with self._readiness_lock:
            for app_id in app_ids:
                self._readiness_cache[str(app_id)] = result
        return result

    def _readiness_loop(self) -> None:
        """Refresh UI/readiness snapshots away from API and heartbeat threads."""
        while not self._readiness_stop.is_set():
            for app in self.manager.export_apps():
                if self._readiness_stop.is_set():
                    break
                if not app.get("protected"):
                    continue
                app_id = str(app["id"])
                try:
                    value = self.readiness_engine.evaluate([app_id])
                    with self._readiness_lock:
                        self._readiness_cache[app_id] = value
                except Exception as exc:
                    LOGGER.warning("readiness refresh failed app=%s error=%s", app_id, exc)
                    with self._readiness_lock:
                        self._readiness_cache[app_id] = Readiness(False, [f"Readiness check failed: {exc}"])
            self._readiness_stop.wait(20.0)

    def _cached_readiness(self, app_id: str) -> Readiness:
        with self._readiness_lock:
            value = self._readiness_cache.get(app_id)
        if value is not None:
            return value
        return Readiness(False, ["Readiness is being checked by Runner Agent"])

    def attach_transport(self, transport) -> None:
        self.transport = transport

    def refresh_transport_peers(self) -> None:
        """Apply pairing changes without requiring an Agent restart."""
        if self.transport is None:
            return
        import base64
        records = self.secret_store.get("trusted_nodes", {})
        self.transport.trust.clear()
        self.transport.trust.update({
            str(node_id): base64.urlsafe_b64decode(value["shared_secret"])
            for node_id, value in records.items()
        })
        self.transport.endpoints.clear()
        self.transport.endpoints.update({str(node["node_id"]): str(node["endpoint"]) for node in self.cluster.nodes})

    def remote_logs(self, app_id: str) -> dict[str, Any]:
        app = self.manager.snapshot(app_id)
        owner = ""
        if app.get("protected") and self.ownership:
            group = str(app.get("service_group") or app_id)
            lease = self.ownership.lease(group)
            if lease is None:
                try:
                    lease = self.ownership.coordinator.current(group)
                except Exception:
                    lease = None
            owner = str(lease.owner_node_id) if lease else ""
        if owner and owner != self.cluster.node_id and self.transport:
            return self.transport.request(owner, "GET", f"/v1/logs/{app_id}")
        return {"node_id": self.cluster.node_id, "app_id": app_id, "text": self.manager.get_log_cache_text(app_id)}

    def transfer_here(self, app_ids: list[str], source_node_id: str | None = None) -> tuple[bool, str]:
        readiness = self.readiness(app_ids)
        if not readiness.ready:
            return False, "; ".join(readiness.reasons)
        if not source_node_id:
            for app in self.manager.export_apps():
                if app["id"] in app_ids and app.get("protected") and self.ownership:
                    lease = self.ownership.coordinator.current(str(app.get("service_group") or app["id"]))
                    if lease and lease.owner_node_id != self.cluster.node_id:
                        source_node_id = lease.owner_node_id
                        break
        if source_node_id and source_node_id != self.cluster.node_id:
            if not self.transport:
                return False, "Trusted peer transport is unavailable"
            prepared = self.transport.request(source_node_id, "POST", "/v1/handoff/prepare", {"app_ids": app_ids, "target_node_id": self.cluster.node_id})
            if not prepared.get("ready", False):
                return False, "Current owner refused handoff: " + "; ".join(prepared.get("reasons", []))
            released = self.transport.request(source_node_id, "POST", "/v1/handoff/release", {"app_ids": app_ids, "target_node_id": self.cluster.node_id})
            if not released.get("success", False):
                return False, str(released.get("reason", "Current owner could not safely release ownership"))
        return self.acquire_and_start(app_ids)

    def status(self, *, refresh_readiness: bool = True) -> dict[str, Any]:
        leases: dict[str, dict] = {}
        applications = self.manager.app_definitions()
        if self.ownership:
            for app in applications:
                group = app.get("service_group", "")
                lease = self.ownership.lease(group)
                if lease is None and group and not refresh_readiness:
                    # Heartbeats/local UI reads must not synchronously call a
                    # remote witness. Reuse the most recent authenticated
                    # peer heartbeat lease when this node is a standby.
                    for peer in self.peer_status.values():
                        cached = dict(peer.get("leases", {})).get(group)
                        if cached:
                            try:
                                lease = Lease(**cached)
                                break
                            except (TypeError, ValueError):
                                continue
                if lease is None and group and refresh_readiness:
                    try:
                        lease = self.ownership.coordinator.current(group)
                    except Exception:
                        lease = None
                if lease:
                    leases[group] = {
                        "owner_node_id": lease.owner_node_id,
                        "lease_id": lease.lease_id,
                        "epoch": lease.epoch,
                        "expires_at": lease.expires_at,
                    }
                if app.get("protected"):
                    readiness = self.readiness([app["id"]]) if refresh_readiness else self._cached_readiness(str(app["id"]))
                    app["owner_node_id"] = lease.owner_node_id if lease else ""
                    app["lease_epoch"] = lease.epoch if lease else None
                    app["start_allowed"] = readiness.ready
                    app["local_owner"] = bool(self.ownership.authorized(group))
                    app["backup_ready"] = readiness.ready
                    app["readiness_reasons"] = readiness.reasons
                    if lease and lease.owner_node_id == self.cluster.node_id and self.cluster.nodes:
                        # On the active node, readiness means the paired
                        # deployment—not the active copy—is ready for takeover.
                        peer_records = [
                            self.peer_status.get(str(node.get("node_id")), {})
                            for node in self.cluster.nodes
                            if str(node.get("node_id")) != self.cluster.node_id
                        ]
                        peer_app = next(
                            (record.get("applications", {}).get(app["id"]) for record in peer_records
                             if record.get("applications", {}).get(app["id"])),
                            None,
                        )
                        app["backup_ready"] = bool(peer_app and peer_app.get("backup_ready"))
                        app["backup_readiness_reasons"] = list((peer_app or {}).get("readiness_reasons", []))
                    if lease and lease.owner_node_id != self.cluster.node_id:
                        remote = self.peer_status.get(lease.owner_node_id, {}).get("applications", {}).get(app["id"], {})
                        if remote:
                            local_readiness = {
                                "backup_ready": app["backup_ready"],
                                "readiness_reasons": app["readiness_reasons"],
                                "start_allowed": False,
                            }
                            for key in ("status", "health", "pid", "uptime", "cpu_percent", "memory_mb", "last_error"):
                                if key in remote:
                                    app[key] = remote[key]
                            app.update(local_readiness)
                            app["remote_metrics"] = True
                            app["origin_node_id"] = lease.owner_node_id
                else:
                    app["start_allowed"] = True
        protected_ids = [app["id"] for app in self.manager.export_apps() if app.get("protected") and app.get("auto_start")]
        eligibility: list[str] = []
        if self.cluster.enabled:
            if not self.cluster.nodes: eligibility.append("No trusted backup is paired")
            if not self.ownership: eligibility.append("Authoritative witness is not configured or reachable")
            if not self.cluster.preferred_primary_node_id: eligibility.append("Preferred primary is not configured")
            if protected_ids:
                if refresh_readiness:
                    eligibility.extend(self.readiness(protected_ids).reasons)
                else:
                    for app_id in protected_ids:
                        eligibility.extend(self._cached_readiness(app_id).reasons)
            else:
                eligibility.append("No protected auto-start applications are configured")
        return redact_secrets({
            "agent_version": AGENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "node_id": self.cluster.node_id,
            "node_name": self.cluster.node_name or socket.gethostname(),
            "cluster_name": self.cluster.cluster_name,
            "mode": str(self.cluster.mode),
            "role": str(self.role),
            "os": platform.platform(),
            "leases": leases,
            "last_transition_reason": self.last_transition_reason,
            "applications": applications,
            "peers": self.peer_status,
            "automatic_failover": self.cluster.automatic_failover,
            "automatic_failback": self.cluster.automatic_failback,
            "automatic_failover_eligible": not eligibility,
            "automatic_failover_reasons": list(dict.fromkeys(eligibility)),
        })

    def heartbeat(self) -> Heartbeat:
        self._heartbeat_sequence += 1
        # Liveness traffic must not synchronously run Docker/Compose checks.
        # Fresh readiness remains enforced by the ownership/start path.
        status = self.status(refresh_readiness=False)
        return Heartbeat(
            node_id=self.cluster.node_id,
            sequence=self._heartbeat_sequence,
            sent_at=time.time(),
            agent_version=AGENT_VERSION,
            protocol_version=PROTOCOL_VERSION,
            os_name=platform.system(),
            role=self.role,
            connectivity=ConnectivityState.ONLINE,
            health=HealthState.HEALTHY,
            sync_status=SyncStatus.SYNCED if all(value == SyncStatus.SYNCED for value in self.sync_status.values()) else SyncStatus.OUT_OF_DATE,
            leases=status["leases"],
            applications={app["id"]: app for app in status["applications"]},
        )

    def receive_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        heartbeat = Heartbeat(
            node_id=str(payload["node_id"]), sequence=int(payload["sequence"]), sent_at=float(payload["sent_at"]),
            agent_version=str(payload["agent_version"]), protocol_version=int(payload["protocol_version"]),
            os_name=str(payload["os_name"]), role=NodeRole(payload["role"]),
            connectivity=ConnectivityState(payload["connectivity"]), health=HealthState(payload["health"]),
            sync_status=SyncStatus(payload["sync_status"]), leases=dict(payload.get("leases", {})),
            applications=dict(payload.get("applications", {})),
        )
        accepted = self.heartbeat_monitor.observe(heartbeat)
        if accepted:
            self.peer_status[heartbeat.node_id] = {
                **heartbeat.to_dict(), "connection": "connected", "latency_ms": None,
                "last_heartbeat_age": 0.0, "last_heartbeat_at": time.time(),
            }
        return {"accepted": accepted, "node_id": self.cluster.node_id, "protocol_version": PROTOCOL_VERSION}

    def prepare_handoff(self, app_ids: list[str], target_node_id: str) -> dict[str, Any]:
        reasons: list[str] = []
        by_id = {app["id"]: app for app in self.manager.export_apps()}
        for app_id in app_ids:
            app = by_id.get(app_id)
            if not app:
                reasons.append(f"Unknown application {app_id}")
                continue
            group = str(app.get("service_group") or app_id)
            if app.get("protected") and (not self.ownership or not self.ownership.authorized(group)):
                reasons.append(f"This node is not the authoritative owner of {app['name']}")
        return {"ready": not reasons, "reasons": reasons, "target_node_id": target_node_id}

    def cluster_stop(self, app_ids: list[str]) -> tuple[bool, str]:
        """Stop on the current owner while deliberately retaining its lease."""
        owner = self._owner_for_apps(app_ids)
        if owner and owner != self.cluster.node_id:
            if not self.transport:
                return False, "Current owner is remote and unavailable"
            result = self.transport.request(owner, "POST", "/v1/apps/stop", {"app_ids": app_ids})
            return bool(result.get("success")), str(result.get("reason", "Remote stop completed"))
        for app_id in app_ids:
            self.manager.stop_app(app_id)
        return True, "Stopped on current owner; ownership lease retained"

    def cluster_restart(self, app_ids: list[str]) -> tuple[bool, str]:
        owner = self._owner_for_apps(app_ids)
        if owner and owner != self.cluster.node_id:
            if not self.transport:
                return False, "Current owner is remote and unavailable"
            result = self.transport.request(owner, "POST", "/v1/apps/restart", {"app_ids": app_ids})
            return bool(result.get("success")), str(result.get("reason", "Remote restart completed"))
        for app_id in app_ids:
            app = next(item for item in self.manager.export_apps() if item["id"] == app_id)
            if app.get("protected") and not self._authorize_start(app)[0]:
                return False, "Restart denied because this node does not own the lease"
            self.manager.restart_app(app_id)
        return True, "Restarted on current owner"

    def cluster_force_stop(self, app_ids: list[str]) -> tuple[bool, str]:
        owner = self._owner_for_apps(app_ids)
        if owner and owner != self.cluster.node_id:
            if not self.transport:
                return False, "Current owner is remote and unavailable"
            result = self.transport.request(owner, "POST", "/v1/apps/force-stop", {"app_ids": app_ids})
            return bool(result.get("success")), str(result.get("reason", "Remote force stop completed"))
        for app_id in app_ids:
            self.manager.force_stop_app(app_id)
        return True, "Force stopped on current owner"

    def _owner_for_apps(self, app_ids: list[str]) -> str:
        if not self.ownership:
            return self.cluster.node_id
        for app in self.manager.export_apps():
            if app["id"] in app_ids and app.get("protected"):
                lease = self.ownership.coordinator.current(str(app.get("service_group") or app["id"]))
                if lease:
                    return lease.owner_node_id
        return ""

    def pairing_offer(self) -> dict[str, Any]:
        from dataclasses import asdict
        result = asdict(self.pairing.create_offer())
        try:
            from .tailscale import local_identity
            identity = local_identity()
            result.update({
                "endpoint": f"https://{identity.endpoint_host}:{self.cluster.remote_port}",
                "tailscale_hostname": identity.hostname,
                "tailscale_ipv4": identity.ipv4,
                "tailscale_ipv6": identity.ipv6,
            })
        except Exception as exc:
            result["connection_warning"] = str(exc)
        return result

    def accept_pairing(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.cluster.preferred_primary_node_id:
            self.cluster.preferred_primary_node_id = self.cluster.node_id
        shared = self.pairing.accept(str(payload["code"]), str(payload["node_id"]), str(payload["fingerprint"]))
        node = {
            "node_id": str(payload["node_id"]), "name": str(payload.get("name") or payload["node_id"]),
            "endpoint": str(payload["endpoint"]), "os": str(payload.get("os", "unknown")),
            "agent_version": str(payload.get("agent_version", "unknown")), "fingerprint": str(payload["fingerprint"]),
        }
        self.cluster.nodes = [value for value in self.cluster.nodes if value.get("node_id") != node["node_id"]] + [node]
        self.store.save_cluster(self.cluster)
        self.refresh_transport_peers()
        self.audit.write("node_paired", remote_node_id=node["node_id"])
        return {
            "node_id": self.cluster.node_id, "fingerprint": self.identity["public_fingerprint"],
            "shared_secret": shared, "preferred_primary_node_id": self.cluster.preferred_primary_node_id,
        }

    def pair_remote(self, endpoint: str, code: str) -> dict[str, Any]:
        import urllib.request
        advertised = socket.gethostname()
        tailscale = {}
        try:
            from .tailscale import local_identity
            identity = local_identity()
            advertised = identity.endpoint_host
            tailscale = {"tailscale_hostname": identity.hostname, "tailscale_ipv4": identity.ipv4, "tailscale_ipv6": identity.ipv6}
        except Exception:
            pass
        payload = {
            "code": code, "node_id": self.cluster.node_id, "fingerprint": self.identity["public_fingerprint"],
            "name": self.cluster.node_name or socket.gethostname(), "endpoint": f"https://{advertised}:{self.cluster.remote_port}",
            "os": platform.system(), "agent_version": AGENT_VERSION,
            **tailscale,
        }
        body = json.dumps(payload).encode()
        request = urllib.request.Request(endpoint.rstrip("/") + "/v1/pairing/accept", data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode())
        trust = dict(self.secret_store.get("trusted_nodes", {}))
        trust[str(result["node_id"])] = {"fingerprint": str(result["fingerprint"]), "shared_secret": str(result["shared_secret"])}
        self.secret_store.set("trusted_nodes", trust)
        if not self.cluster.preferred_primary_node_id:
            self.cluster.preferred_primary_node_id = str(result.get("preferred_primary_node_id") or self.cluster.node_id)
        node = {"node_id": str(result["node_id"]), "name": str(result["node_id"]), "endpoint": endpoint}
        self.cluster.nodes = [value for value in self.cluster.nodes if value.get("node_id") != node["node_id"]] + [node]
        self.store.save_cluster(self.cluster)
        self.refresh_transport_peers()
        self.audit.write("node_paired", remote_node_id=node["node_id"])
        return node

    def remove_peer(self, node_id: str) -> tuple[bool, str]:
        # Removing trust must never be interpreted as permission to take over.
        # Refuse while that peer owns any configured service group.
        if self.ownership:
            for app in self.manager.export_apps():
                if not app.get("protected"):
                    continue
                lease = self.ownership.coordinator.current(str(app.get("service_group") or app["id"]))
                if lease and lease.owner_node_id == node_id:
                    return False, "Cannot unpair the node while it owns protected applications; transfer ownership first"
        trust = dict(self.secret_store.get("trusted_nodes", {}))
        trust.pop(node_id, None)
        self.secret_store.set("trusted_nodes", trust)
        self.cluster.nodes = [node for node in self.cluster.nodes if str(node.get("node_id")) != node_id]
        self.store.save_cluster(self.cluster)
        self.peer_status.pop(node_id, None)
        self.refresh_transport_peers()
        self.audit.write("node_unpaired", remote_node_id=node_id)
        return True, "Peer trust removed"

    def configure_witness(self, url: str, shared_secret: str) -> tuple[bool, str]:
        import base64
        from .coordinator import HttpCoordinator
        normalized = url.rstrip("/")
        if not normalized.startswith("https://"):
            return False, "Witness address must use HTTPS"
        try:
            secret = base64.urlsafe_b64decode(shared_secret.encode("ascii"))
            if len(secret) < 32:
                raise ValueError("Witness secret is too short")
            coordinator = HttpCoordinator(normalized, self.cluster.node_id, secret)
            # A current-lease lookup is harmless and proves TLS, authentication,
            # protocol reachability and witness database availability.
            coordinator.current("runner-setup-check")
        except Exception as exc:
            return False, f"Witness test failed: {exc}"
        self.secret_store.set("witness_shared_secret", shared_secret)
        self.cluster.witness_url = normalized
        self.store.save_cluster(self.cluster)
        self.audit.write("witness_configured", witness_url=normalized)
        return True, "Witness connected. Restart Runner Agent to apply the new lease connection."

    def sync_manifest(self, app_id: str) -> dict[str, Any]:
        app = next(value for value in self.manager.export_apps() if value["id"] == app_id)
        from .sync import build_manifest
        root = Path(app["cwd"])
        excludes = list(app.get("sync", {}).get("exclude", []))
        # Compose deployments are copied to a standby as a deployable source
        # tree, never as a snapshot of a live host.  In particular, .env is
        # supplied through Runner's encrypted secret store and the known AI
        # Bridge state folders are not safe to replicate as ordinary files.
        # A future explicitly configured persistence replicator may opt into a
        # safe strategy; it must not happen implicitly during file sync.
        if app.get("app_type") == "docker_compose":
            excludes.extend([
                ".env", "**/.env", "*.env", "**/*.env",
                ".runner_runtime/**", "**/.runner_runtime/**",
                "ai-logs/**", "**/ai-logs/**",
                "training_data/**", "**/training_data/**",
                "node_modules/**", "**/node_modules/**",
                ".venv/**", "**/.venv/**", "venv/**", "**/venv/**",
            ])
        persistence = dict(app.get("persistence", {}))
        if persistence.get("strategy") in {"sqlite", "unsupported"}:
            excludes.extend(str(value).replace("\\", "/") for value in persistence.get("paths", []))
        manifest = build_manifest(root, excludes)
        return manifest.to_dict()

    def receive_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        import base64
        from .sync import AtomicDeployment, ManifestEntry, SyncManifest, safe_destination
        app_id = str(payload["app_id"])
        entries = [ManifestEntry(**item) for item in payload["manifest"]["entries"]]
        manifest = SyncManifest(str(payload["manifest"]["version"]), entries)
        incoming = self.runtime_root / "incoming" / app_id / manifest.version
        if incoming.exists(): shutil.rmtree(incoming)
        incoming.mkdir(parents=True)
        for relative, encoded in payload["files"].items():
            destination = safe_destination(incoming, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(encoded))
        deployment = AtomicDeployment(self.runtime_root / "deployments")
        staged = deployment.stage_from_directory(app_id, incoming, manifest)
        active = deployment.activate(app_id, staged, manifest.version)
        incoming_app = dict(payload.get("application", {}))
        if incoming_app:
            source_cwd = Path(str(incoming_app.get("cwd") or ""))
            source_runner = Path(str(incoming_app.get("runner_path") or ""))
            local = dict(incoming_app)
            local["cwd"] = str(active)
            if local.get("app_type") == "docker_compose":
                compose = dict(local.get("compose") or {})
                compose_file = Path(str(compose.get("compose_file") or "docker-compose.yml"))
                if compose_file.is_absolute():
                    raise ValueError("Compose file must be relative to its synchronized project root")
                compose["project_dir"] = str(active)
                compose["compose_file"] = str(active / compose_file)
                local["compose"] = compose
                local["runner_path"] = ""
                local_persistence = dict(local.get("persistence") or {})
                local_persistence.setdefault("strategy", "stateless")
                local["persistence"] = local_persistence
            else:
                try:
                    relative_runner = source_runner.resolve().relative_to(source_cwd.resolve())
                except (OSError, ValueError):
                    raise ValueError("Application entry file must be inside its synchronized project root")
                local["runner_path"] = str(active / relative_runner)
            deployments = dict(local.get("deployments", {}))
            deployments[self.cluster.node_id] = {
                "supported": True, "ready": True, "cwd": str(active),
                "runner_path": local["runner_path"], "version": manifest.version,
                "prepared_at": time.time(),
            }
            local["deployments"] = deployments
            with self.config_lock:
                existing = {item["id"] for item in self.manager.export_apps()}
                if app_id in existing:
                    self.manager.update_app(app_id, local)
                else:
                    self.manager.add_app(local)
                from .config import atomic_json_write
                atomic_json_write(self.store.apps_path, {"schema_version": 3, "apps": self.manager.export_apps()})
        shutil.rmtree(incoming, ignore_errors=True)
        self.sync_status[app_id] = SyncStatus.SYNCED
        self.audit.write("sync_completed", application_id=app_id, version=manifest.version)
        return {"success": True, "version": manifest.version, "path": str(active)}

    def shutdown(self, stop_applications: bool = True) -> None:
        self._readiness_stop.set()
        if self._readiness_thread:
            self._readiness_thread.join(timeout=2.0)
        if self.ownership:
            self.ownership.stop()
        if stop_applications:
            self.manager.shutdown()

    def _authorize_start(self, app: dict[str, Any]) -> tuple[bool, str]:
        if not app.get("protected") or not self.cluster.enabled:
            return True, "Standalone or unprotected application"
        group = str(app.get("service_group") or app["id"])
        if self.ownership and self.ownership.authorized(group):
            return True, "Current fencing lease is valid"
        return False, f"Start denied: this node does not own a valid lease for {group}"

    def _self_fence(self, group_id: str, reason: str) -> None:
        LOGGER.critical("Self-fencing service group %s: %s", group_id, reason)
        self.audit.write("self_fence_initiated", service_group=group_id, reason=reason)
        self._guard_path(group_id).unlink(missing_ok=True)
        for app in self.manager.export_apps():
            if app.get("protected") and app.get("service_group") == group_id:
                self.manager.force_stop_app(app["id"])
        self.role = NodeRole.STANDBY
        self.last_transition_reason = f"Self-fenced: {reason}"

    def _guard_path(self, group_id: str) -> Path:
        safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in group_id)
        return self.runtime_root / "guards" / f"{safe}.deadline"

    def _publish_guard_deadline(self, group_id: str, deadline: float) -> None:
        path = self._guard_path(group_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        import uuid
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(f"{deadline:.9f}", encoding="ascii")
        try:
            for attempt in range(20):
                try:
                    os.replace(temporary, path)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.02)
        finally:
            temporary.unlink(missing_ok=True)

    def _wait_for_running(self, app_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.manager.snapshot(app_id)
            if snapshot["status"] in {"Running", "Waiting Input", "Already Running"}:
                if snapshot.get("app_type") == "docker_compose":
                    # The provider marks Running only when Docker reports every
                    # required service running and healthy/healthless.
                    return True
                check = self.health_checker.check(snapshot.get("health_check", {"type": "process"}), snapshot.get("pid"))
                return check.state == HealthState.HEALTHY
            if snapshot["status"] == "Crashed":
                return False
            time.sleep(0.1)
        return False

    def _wait_for_stopped(self, app_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.manager.snapshot(app_id)["status"] in {"Stopped", "Stopped by Runner", "Crashed"}:
                return True
            time.sleep(0.1)
        return False
