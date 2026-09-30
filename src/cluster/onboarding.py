from __future__ import annotations

import os
import platform
import subprocess
import time
import uuid
import json
import urllib.request
from pathlib import Path

from .config import ConfigStore, atomic_json_write
from .models import ClusterConfig
from .security import IdentityStore


class OnboardingController:
    """Small, non-networking first-run setup boundary used by the GUI wizard."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.marker = runtime_root / "onboarding.json"
        self.store = ConfigStore(runtime_root)

    def complete(self) -> bool:
        return self.marker.exists()

    def _configured_mode(self, mode: str, *, enabled: bool) -> ClusterConfig:
        if mode not in {"standalone", "primary", "backup"}:
            raise ValueError("Unknown Runner setup choice")
        identity = IdentityStore(self.runtime_root).load_or_create()
        cluster = self.store.load_cluster()
        if mode == "standalone":
            cluster.enabled = False
        else:
            # A selected server role is not, by itself, permission to put the
            # GUI into Agent-only mode.  The caller commits ``enabled`` only
            # after the local Agent task and authenticated API are healthy.
            cluster.enabled = enabled
            cluster.cluster_id = cluster.cluster_id or str(uuid.uuid4())
            cluster.node_id = identity["node_id"]
            cluster.node_name = cluster.node_name or platform.node()
            if mode == "primary":
                cluster.preferred_primary_node_id = cluster.node_id
        return cluster

    def select_mode(self, mode: str) -> ClusterConfig:
        """Compatibility entry point for callers that already provisioned Agent."""
        cluster = self._configured_mode(mode, enabled=(mode != "standalone"))
        self.store.save_cluster(cluster)
        atomic_json_write(self.marker, {
            "completed_at": time.time(), "mode": mode,
            "node_id": IdentityStore(self.runtime_root).load_or_create()["node_id"],
        })
        return cluster

    def prepare_mode(self, mode: str) -> ClusterConfig:
        """Persist identity/basic node metadata but keep cluster mode disabled."""
        cluster = self._configured_mode(mode, enabled=False)
        self.store.save_cluster(cluster)
        return cluster

    def commit_mode(self, mode: str) -> ClusterConfig:
        """Commit Agent-only cluster mode after provisioning has succeeded."""
        return self.select_mode(mode)

    def agent_task_status(self) -> tuple[bool, str]:
        if os.name != "nt":
            return False, "Managed by systemd on Linux"
        try:
            completed = subprocess.run(
                ["schtasks", "/Query", "/TN", "Runner Agent", "/FO", "LIST"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0, "Runner Agent task installed" if completed.returncode == 0 else "Runner Agent task needs repair"
        except OSError:
            return False, "Could not check Runner Agent service"

    def start_agent_task(self) -> None:
        if os.name == "nt":
            subprocess.run(["schtasks", "/End", "/TN", "Runner Agent"], capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            subprocess.run(["schtasks", "/Run", "/TN", "Runner Agent"], capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def local_agent_health(self, timeout: float = 3.0) -> tuple[bool, str]:
        """Verify the actual authenticated API, never merely a TCP port."""
        token_path = self.runtime_root / "agent-api.json"
        if not token_path.exists():
            return False, "Runner Agent has not initialized its local control API"
        try:
            token = str(json.loads(token_path.read_text(encoding="utf-8"))["token"])
            request = urllib.request.Request(
                "http://127.0.0.1:47471/v1/status",
                headers={"Authorization": f"Bearer {token}"}, method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("node_id"):
                return False, "Runner Agent returned an invalid health response"
            return True, "Runner Agent local API is healthy"
        except Exception as exc:
            return False, f"Runner Agent local API is unavailable: {exc}"
