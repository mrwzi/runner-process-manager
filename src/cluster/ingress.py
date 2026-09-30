from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import shutil
import subprocess
from pathlib import Path


@dataclass(slots=True)
class IngressStatus:
    healthy: bool
    active_node_id: str
    reason: str


class IngressProvider(ABC):
    @abstractmethod
    def status(self, application_id: str) -> IngressStatus: ...

    @abstractmethod
    def activate(self, application_id: str, node_id: str, fencing_epoch: int) -> None: ...

    @abstractmethod
    def deactivate(self, application_id: str, node_id: str, fencing_epoch: int) -> None: ...


class CloudflareTunnelProvider(IngressProvider):
    """Provider boundary for named Cloudflare Tunnels.

    Tunnel credentials belong in SecretStore. The connector should be modeled as
    a lease-protected dependency so only the authorized application owner routes
    side-effecting website traffic.
    """

    def __init__(self, configurations: dict[str, dict] | None = None) -> None:
        self._states: dict[str, IngressStatus] = {}
        self._epochs: dict[str, int] = {}
        self._configurations = configurations or {}
        self._processes: dict[str, subprocess.Popen] = {}

    def status(self, application_id: str) -> IngressStatus:
        return self._states.get(application_id, IngressStatus(False, "", "Ingress is not configured"))

    def activate(self, application_id: str, node_id: str, fencing_epoch: int) -> None:
        if fencing_epoch < self._epochs.get(application_id, 0):
            raise PermissionError("Stale fencing epoch cannot activate ingress")
        config = self._configurations.get(application_id, {})
        if config.get("enabled"):
            executable = shutil.which(str(config.get("executable") or "cloudflared"))
            if not executable:
                raise FileNotFoundError("cloudflared is not installed or not in PATH")
            command = [executable, "tunnel"]
            if config.get("config_file"):
                path = Path(str(config["config_file"]))
                if not path.is_file():
                    raise FileNotFoundError(f"Cloudflare Tunnel config does not exist: {path}")
                command.extend(["--config", str(path)])
            command.extend(["run", str(config.get("tunnel") or "")])
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._processes[application_id] = process
        self._epochs[application_id] = fencing_epoch
        self._states[application_id] = IngressStatus(True, node_id, "Cloudflare Tunnel ownership activated")

    def deactivate(self, application_id: str, node_id: str, fencing_epoch: int) -> None:
        if fencing_epoch != self._epochs.get(application_id) or self.status(application_id).active_node_id != node_id:
            raise PermissionError("Stale owner cannot deactivate current ingress")
        process = self._processes.pop(application_id, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self._states[application_id] = IngressStatus(False, "", "Ingress ownership released")
