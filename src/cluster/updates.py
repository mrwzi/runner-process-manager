"""Signed release discovery and safe, machine-local update preparation.

The release URL deliberately defaults to empty.  A distributor must configure
an HTTPS URL and an Ed25519 release key before online updates are offered.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import atomic_json_write
from .version import RUNNER_VERSION


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: str = ""

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?", value.strip())
        if not match:
            raise UpdateError(f"Invalid semantic version: {value!r}")
        return cls(*(int(match.group(i)) for i in range(1, 4)), match.group(4) or "")

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        numeric = (self.major, self.minor, self.patch)
        other_numeric = (other.major, other.minor, other.patch)
        if numeric != other_numeric:
            return numeric < other_numeric
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        return self.prerelease < other.prerelease


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    artifacts: dict[str, dict[str, str]]
    min_protocol_version: int | None = None


class UpdateSettings:
    def __init__(self, runtime_root: Path) -> None:
        self.path = runtime_root / "update-settings.json"

    def load(self) -> dict[str, Any]:
        default = {"manifest_url": "", "automatically_check": True, "download_automatically": False}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {**default, **raw}
        except (OSError, ValueError, json.JSONDecodeError):
            return default

    def save(self, values: dict[str, Any]) -> None:
        current = self.load()
        current.update({key: values[key] for key in current.keys() if key in values})
        atomic_json_write(self.path, current)


class ReleaseClient:
    """Fetches a signed manifest, then verifies artifact bytes before use."""
    def __init__(self, public_key_b64: str = "", opener: Callable[..., Any] | None = None) -> None:
        self.public_key_b64 = public_key_b64
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def fetch(self, url: str, timeout: float = 12.0) -> Release:
        if not url.startswith("https://"):
            raise UpdateError("Release manifest must use HTTPS.")
        try:
            with self.opener(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise UpdateError(f"Could not retrieve release manifest: {exc}") from exc
        if not self.public_key_b64:
            raise UpdateError("No Runner release signing key is configured.")
        try:
            signature = base64.b64decode(str(payload.pop("signature")))
            Ed25519PublicKey.from_public_bytes(base64.b64decode(self.public_key_b64)).verify(signature, self._canonical(payload))
        except Exception as exc:
            raise UpdateError("Release manifest signature verification failed.") from exc
        if not isinstance(payload.get("artifacts"), dict):
            raise UpdateError("Release manifest has no artifacts.")
        SemanticVersion.parse(str(payload.get("version", "")))
        return Release(str(payload["version"]), str(payload.get("release_notes", "")), payload["artifacts"], payload.get("min_protocol_version"))

    def download(self, artifact: dict[str, str], destination: Path, timeout: float = 45.0) -> Path:
        url, expected = artifact.get("url", ""), artifact.get("sha256", "").lower()
        if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise UpdateError("Release artifact URL or SHA-256 is invalid.")
        digest = hashlib.sha256()
        temporary = destination.with_suffix(destination.suffix + ".partial")
        try:
            with self.opener(url, timeout=timeout) as response, temporary.open("wb") as output:
                while data := response.read(1024 * 1024):
                    digest.update(data)
                    output.write(data)
            if digest.hexdigest() != expected:
                raise UpdateError("Downloaded update failed SHA-256 verification.")
            os.replace(temporary, destination)
            return destination
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def backup_runtime(runtime_root: Path) -> Path:
    """Snapshot config/state before installer migration; never changes identity."""
    target = runtime_root / "config-backups" / f"update-{time.strftime('%Y%m%d-%H%M%S')}"
    target.mkdir(parents=True, exist_ok=False)
    for name in ("apps.json", "cluster.json", "identity.json", "secrets.enc", "secrets.key", "ui_state.json", "onboarding.json", "update-settings.json"):
        source = runtime_root / name
        if source.exists():
            shutil.copy2(source, target / name)
    return target


def update_plan(local_status: dict[str, Any], installed_version: str = RUNNER_VERSION) -> tuple[bool, str]:
    """Return whether it is safe to update this node now, never transfer itself."""
    if not local_status.get("mode") == "cluster":
        return True, "Standalone node: update may restart the local Agent."
    role = str(local_status.get("role", ""))
    peers = local_status.get("peers", {})
    healthy_backup = any(bool(peer.get("ready") or peer.get("backup_ready")) and peer.get("connection") == "connected" for peer in peers.values())
    if role == "active" and healthy_backup:
        return False, "This node is ACTIVE. Update the healthy standby first, verify it, then use a controlled ownership transfer before updating this node."
    if role == "active":
        return False, "This node is ACTIVE and no healthy standby is verified. Runner will not restart production services for an update."
    return True, "Standby node: safe to update after its current work completes."
