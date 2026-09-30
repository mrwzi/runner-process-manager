from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path

from .models import ConnectivityState
from .transport import ClusterTransport
from .failover import FailoverController
from .models import NodeRole


# A manifest hashes every deployment file.  Heartbeats must remain cheap; a
# heartbeat interval is a liveness setting, not a request to rescan a whole
# project tree.  The first sync remains immediate and changes are picked up by
# this bounded background check.
SYNC_MANIFEST_CHECK_INTERVAL_SECONDS = 30.0
LOGGER = logging.getLogger("runner.peer_service")


class PeerService:
    """Continuous heartbeats and bounded-retry synchronization over ClusterTransport."""

    def __init__(self, agent, transport: ClusterTransport) -> None:
        self.agent = agent
        self.transport = transport
        self._stop = threading.Event()
        self._thread = None
        self._sync_versions: dict[tuple[str, str], str] = {}
        self._manifest_cache: dict[str, dict] = {}
        self._last_manifest_check: dict[str, float] = {}
        self._started = time.monotonic()
        self._failover = FailoverController(agent, agent.heartbeat_monitor)
        self._action_lock = threading.Lock()
        self.agent.attach_transport(transport)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="runner-peer-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # A handoff is allowed to finish its bounded graceful-stop request so
        # shutdown cannot leave a background HTTP handler holding persistence
        # resources (or leave a half-completed ownership transition).
        if self._thread: self._thread.join(timeout=35)

    def tick(self) -> None:
        self.agent.refresh_transport_peers()
        heartbeat = self.agent.heartbeat().to_dict()
        for peer in self.agent.cluster.nodes:
            node_id = str(peer["node_id"])
            started = time.monotonic()
            try:
                self.transport.request(node_id, "POST", "/v1/heartbeat", heartbeat)
                record = self.agent.peer_status.setdefault(node_id, {})
                record.update({"connection": "connected", "latency_ms": (time.monotonic() - started) * 1000, "last_error": ""})
                if self.agent.role == NodeRole.ACTIVE:
                    self._sync_peer(node_id)
            except Exception as exc:
                record = self.agent.peer_status.setdefault(node_id, {})
                age = self.agent.heartbeat_monitor.age(node_id)
                if age is None:
                    age = time.monotonic() - self._started
                if age <= self.agent.cluster.heartbeat_interval_seconds * 2:
                    state = "degraded"
                elif age <= self.agent.cluster.suspect_after_seconds:
                    state = "suspect"
                else:
                    state = "offline"
                record.update({"connection": state, "last_error": str(exc), "last_heartbeat_age": age})
        self._orchestrate()

    def _sync_peer(self, node_id: str) -> None:
        for app in self.agent.manager.export_apps():
            sync = app.get("sync", {})
            if not sync.get("enabled"):
                continue
            app_id = app["id"]
            now = time.monotonic()
            manifest = self._manifest_cache.get(app_id)
            # A cache miss intentionally performs an immediate manifest build
            # so a newly paired standby begins preparation without waiting.
            if manifest is None or now - self._last_manifest_check.get(app_id, 0.0) >= SYNC_MANIFEST_CHECK_INTERVAL_SECONDS:
                manifest = self.agent.sync_manifest(app_id)
                self._manifest_cache[app_id] = manifest
                self._last_manifest_check[app_id] = now
            key = (node_id, app_id)
            if self._sync_versions.get(key) == manifest["version"]:
                continue
            root = Path(app["cwd"])
            files = {
                entry["path"]: base64.b64encode((root / Path(*entry["path"].split("/"))).read_bytes()).decode("ascii")
                for entry in manifest["entries"]
            }
            from .models import SyncStatus
            self.agent.sync_status[app["id"]] = SyncStatus.SYNCING
            response = self.transport.request(node_id, "POST", "/v1/sync/push", {
                "app_id": app["id"], "manifest": manifest, "files": files, "application": app,
            })
            if response.get("success"):
                self._sync_versions[key] = manifest["version"]
                self.agent.sync_status[app["id"]] = SyncStatus.SYNCED

    def _orchestrate(self) -> None:
        if not self._action_lock.acquire(blocking=False):
            return
        try:
            protected = [
                app["id"] for app in self.agent.manager.export_apps()
                if app.get("protected") and app.get("auto_start")
            ]
            if not protected or not self.agent.ownership:
                return
            preferred = self.agent.cluster.preferred_primary_node_id
            local = self.agent.cluster.node_id
            # The preferred node may acquire an unowned cluster on initial boot.
            if local == preferred:
                remote_owners = set()
                for app in self.agent.manager.export_apps():
                    if app["id"] not in protected:
                        continue
                    lease = self.agent.ownership.coordinator.current(str(app.get("service_group") or app["id"]))
                    if lease and lease.owner_node_id != local:
                        remote_owners.add(lease.owner_node_id)
                if not remote_owners and self.agent.role != NodeRole.ACTIVE and time.monotonic() >= self.agent.suppress_reacquire_until:
                    success, reason = self.agent.acquire_and_start(protected)
                    if not success:
                        self.agent.last_transition_reason = f"Ownership acquisition deferred: {reason}"
                elif remote_owners and self.agent.cluster.automatic_failback:
                    owner = sorted(remote_owners)[0]
                    if self.agent.heartbeat_monitor.reachable(owner) and self.agent.readiness(protected).ready:
                        self.agent.transfer_here(protected, owner)
                return
            if not self.agent.cluster.automatic_failover or not preferred:
                return
            # Never infer failure immediately after startup; require a complete
            # local monotonic suspect window with no acceptable heartbeat.
            if time.monotonic() - self._started < self.agent.cluster.suspect_after_seconds:
                return
            decision = self._failover.evaluate(preferred, protected)
            self.agent.last_transition_reason = decision.reason
            if decision.acted:
                self.agent.audit.write("automatic_failover_completed", applications=protected, reason=decision.reason)
        except Exception as exc:
            self.agent.last_transition_reason = f"Cluster orchestration deferred: {exc}"
            self.agent.audit.write("cluster_orchestration_deferred", reason=str(exc))
        finally:
            self._action_lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # A transient local API/witness/heartbeat exception must not
                # silently kill the only reconnect and failover scheduler.
                self.agent.last_transition_reason = f"Agent monitoring retrying: {exc}"
                LOGGER.warning("peer service tick failed node=%s error=%r", self.agent.cluster.node_id, exc)
                try:
                    self.agent.audit.write("peer_service_tick_failed", reason=str(exc))
                except Exception:
                    pass
            self._stop.wait(self.agent.cluster.heartbeat_interval_seconds)
