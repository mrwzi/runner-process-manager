from __future__ import annotations

import os
import platform
import shutil
import socket
from pathlib import Path
from typing import Any

from .models import PROTOCOL_VERSION, Readiness, SyncStatus
from .platform import current_platform
from .sync import persistence_ready
from manager.deployments import DockerComposeDeploymentProvider


class ReadinessEngine:
    """Single authoritative deployment readiness calculation.

    This intentionally returns every known reason instead of stopping at the
    first failure; operators need an actionable list before enabling failover.
    """

    def __init__(self, agent) -> None:
        self.agent = agent

    def evaluate(self, app_ids: list[str]) -> Readiness:
        apps = {item["id"]: item for item in self.agent.manager.export_apps()}
        reasons: list[str] = []
        visiting: set[str] = set()
        checked: set[str] = set()

        def check(app_id: str) -> None:
            if app_id in checked:
                return
            if app_id in visiting:
                reasons.append(f"{app_id}: dependency cycle detected")
                return
            app = apps.get(app_id)
            if app is None:
                reasons.append(f"{app_id}: application configuration is missing")
                return
            visiting.add(app_id)
            name = str(app.get("name") or app_id)
            for dependency in app.get("dependencies", []):
                check(str(dependency))

            if app.get("app_type") == "docker_compose":
                compose = dict(app.get("compose") or {})
                provider = DockerComposeDeploymentProvider({
                    "id": app_id, "cwd": app.get("cwd", ""),
                    "require_healthchecks": bool(app.get("protected") and self.agent.cluster.enabled),
                    **compose,
                })
                deployment = dict(app.get("deployments", {})).get(self.agent.cluster.node_id, {})
                if app.get("protected") and self.agent.cluster.enabled:
                    if not deployment:
                        reasons.append(f"{name}: no Compose deployment is configured for this node")
                    elif not deployment.get("supported", True):
                        reasons.append(f"{name}: Compose deployment is unsupported on {platform.system()}")
                    elif not deployment.get("ready", False):
                        reasons.append(f"{name}: Compose deployment preparation is incomplete")
                sync = dict(app.get("sync", {}))
                state = self.agent.sync_status.get(app_id, SyncStatus(str(sync.get("status", "not_configured"))))
                local_preferred_bootstrap = self.agent.cluster.node_id == self.agent.cluster.preferred_primary_node_id
                local_owner = bool(self.agent.ownership and self.agent.ownership.authorized(str(app.get("service_group") or app_id)))
                if sync.get("enabled") and state != SyncStatus.SYNCED and not (local_preferred_bootstrap or local_owner):
                    reasons.append(f"{name}: project synchronization is {state}")
                persistence = dict(app.get("persistence", {}))
                persistence_ok, persistence_reason = persistence_ready(persistence)
                if not persistence_ok:
                    reasons.append(f"{name}: {persistence_reason}")
                available = set(self.agent.secret_store.get("application_secrets", {}).keys())
                missing_declared = sorted(set(str(item) for item in app.get("required_secrets", [])) - available)
                if missing_declared:
                    reasons.append(f"{name}: required secret(s) missing: " + ", ".join(missing_declared))
                ready, docker_reasons, _state = provider.readiness(available, require_stopped=not local_owner)
                if not ready:
                    reasons.extend(f"{name}: {reason}" for reason in docker_reasons)
                visiting.remove(app_id)
                checked.add(app_id)
                return

            deployment = dict(app.get("deployments", {})).get(self.agent.cluster.node_id, {})
            if app.get("protected") and self.agent.cluster.enabled:
                if not deployment:
                    reasons.append(f"{name}: no deployment is configured for this node")
                elif not deployment.get("supported", True):
                    reasons.append(f"{name}: deployment is unsupported on {platform.system()}")
                elif not deployment.get("ready", False):
                    reasons.append(f"{name}: deployment preparation is incomplete")

            cwd = Path(str(deployment.get("cwd") or app.get("cwd") or ""))
            runner = Path(str(deployment.get("runner_path") or app.get("runner_path") or ""))
            if not cwd.is_dir():
                reasons.append(f"{name}: working directory does not exist: {cwd}")
            if not runner.is_file():
                reasons.append(f"{name}: entry file does not exist: {runner}")
            else:
                suffix = runner.suffix.lower()
                if platform.system() != "Windows" and suffix in {".bat", ".cmd", ".exe"}:
                    reasons.append(f"{name}: {suffix} applications are unsupported on {platform.system()}")
                try:
                    current_platform().build_command(str(runner), str(cwd), list(app.get("args", [])))
                except (OSError, FileNotFoundError) as exc:
                    reasons.append(f"{name}: {exc}")

            ready, reason = persistence_ready(dict(app.get("persistence", {})))
            if not ready:
                reasons.append(f"{name}: {reason}")
            persistence = dict(app.get("persistence", {}))
            if persistence.get("strategy") == "external":
                host = persistence.get("host")
                port = persistence.get("port")
                if host and port:
                    try:
                        with socket.create_connection((str(host), int(port)), timeout=2):
                            pass
                    except OSError as exc:
                        reasons.append(f"{name}: external database is unreachable: {exc}")

            sync = dict(app.get("sync", {}))
            state = self.agent.sync_status.get(app_id, SyncStatus(str(sync.get("status", "not_configured"))))
            local_preferred_bootstrap = self.agent.cluster.node_id == self.agent.cluster.preferred_primary_node_id
            local_owner = bool(self.agent.ownership and self.agent.ownership.authorized(str(app.get("service_group") or app_id)))
            if sync.get("enabled") and state != SyncStatus.SYNCED and not (local_preferred_bootstrap or local_owner):
                reasons.append(f"{name}: project synchronization is {state}")

            required_secrets = list(app.get("required_secrets", [])) + list(deployment.get("required_secrets", []))
            available = self.agent.secret_store.get("application_secrets", {})
            for secret_name in required_secrets:
                if str(secret_name) not in available:
                    reasons.append(f"{name}: required secret {secret_name} is missing")

            check_config = dict(app.get("health_check", {}))
            kind = str(check_config.get("type", "process"))
            if kind not in {"process", "tcp", "http", "https"}:
                reasons.append(f"{name}: unsupported health check type {kind}")
            if kind == "tcp" and (not check_config.get("host") or not check_config.get("port")):
                reasons.append(f"{name}: TCP health check requires host and port")
            if kind in {"http", "https"} and not check_config.get("url"):
                reasons.append(f"{name}: HTTP health check requires a URL")

            visiting.remove(app_id)
            checked.add(app_id)

        for app_id in app_ids:
            check(app_id)

        if self.agent.cluster.enabled:
            incompatible = [
                node_id for node_id, value in self.agent.peer_status.items()
                if value.get("protocol_version") not in {None, PROTOCOL_VERSION}
            ]
            if incompatible:
                reasons.append("Runner protocol is incompatible with: " + ", ".join(incompatible))
            if not self.agent.ownership:
                reasons.append("Authoritative witness is unavailable")
            free = shutil.disk_usage(self.agent.runtime_root).free
            required = sum(
                int(dict(apps.get(app_id, {}).get("deployment", {})).get("minimum_free_bytes", 0))
                for app_id in app_ids
            )
            if free < required:
                reasons.append(f"Insufficient disk space: {free} bytes free, {required} required")

        sync_state = SyncStatus.SYNCED if not reasons else SyncStatus.OUT_OF_DATE
        import time
        return Readiness(not reasons, reasons, sync_state, time.time())
