from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class TailscaleIdentity:
    hostname: str
    ipv4: str = ""
    ipv6: str = ""
    online: bool = False

    @property
    def endpoint_host(self) -> str:
        return self.hostname.rstrip(".")


def available_peers(timeout: float = 5.0) -> list[TailscaleIdentity]:
    executable = shutil.which("tailscale")
    if not executable:
        return []
    try:
        result = subprocess.run([executable, "status", "--json"], capture_output=True, text=True, timeout=timeout, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        peers = json.loads(result.stdout).get("Peer", {}).values()
        return [
            TailscaleIdentity(
                hostname=str(item.get("DNSName") or item.get("HostName") or "").rstrip("."),
                ipv4=next((value for value in item.get("TailscaleIPs", []) if ":" not in value), ""),
                ipv6=next((value for value in item.get("TailscaleIPs", []) if ":" in value), ""),
                online=bool(item.get("Online", False)),
            ) for item in peers if item.get("DNSName") or item.get("HostName")
        ]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def local_identity(timeout: float = 5.0) -> TailscaleIdentity:
    executable = shutil.which("tailscale")
    if not executable:
        raise RuntimeError("Tailscale CLI is not installed or not in PATH")
    result = subprocess.run(
        [executable, "status", "--json"], capture_output=True, text=True,
        timeout=timeout, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    payload = json.loads(result.stdout)
    own = payload.get("Self", {})
    addresses = list(own.get("TailscaleIPs", []))
    return TailscaleIdentity(
        hostname=str(own.get("DNSName") or own.get("HostName") or "").rstrip("."),
        ipv4=next((value for value in addresses if ":" not in value), ""),
        ipv6=next((value for value in addresses if ":" in value), ""),
        online=bool(own.get("Online", True)),
    )


def ensure_certificate(runtime_root: Path, hostname: str, timeout: float = 20.0) -> tuple[Path, Path]:
    """Obtain a CA-trusted tailnet certificate through the installed CLI."""
    executable = shutil.which("tailscale")
    if not executable:
        raise RuntimeError("Tailscale CLI is not installed")
    tls_root = runtime_root / "tls"
    tls_root.mkdir(parents=True, exist_ok=True)
    certificate, key = tls_root / "agent.crt", tls_root / "agent.key"
    subprocess.run(
        [executable, "cert", f"--cert-file={certificate}", f"--key-file={key}", hostname],
        capture_output=True, text=True, timeout=timeout, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        key.chmod(0o600)
    except OSError:
        pass
    return certificate, key
