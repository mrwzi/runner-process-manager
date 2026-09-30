from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import atomic_json_write
from .models import PersistenceStrategy, SyncStatus

DEFAULT_EXCLUDES = (
    ".git/**", "**/.git/**", "__pycache__/**", "**/__pycache__/**", "*.pyc", "**/*.pyc",
    "node_modules/**", "**/node_modules/**", ".runner_runtime/**", "**/.runner_runtime/**",
    ".venv/**", "**/.venv/**", "venv/**", "**/venv/**", "env/**", "**/env/**",
    "logs/**", "**/logs/**", "tmp/**", "**/tmp/**",
)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    mode: int


@dataclass(slots=True)
class SyncManifest:
    version: str
    entries: list[ManifestEntry]

    def to_dict(self) -> dict:
        return {"version": self.version, "entries": [asdict(entry) for entry in self.entries]}


def build_manifest(root: Path, excludes: Iterable[str] = ()) -> SyncManifest:
    excluded = set(DEFAULT_EXCLUDES) | set(excludes)
    entries: list[ManifestEntry] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded or any(PurePosixPath(relative).match(pattern) for pattern in excluded):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat_result = path.stat()
        entries.append(ManifestEntry(relative, stat_result.st_size, digest.hexdigest(), stat_result.st_mode & 0o777))
    canonical = json.dumps([asdict(entry) for entry in entries], separators=(",", ":"), sort_keys=True).encode()
    return SyncManifest(hashlib.sha256(canonical).hexdigest(), entries)


def safe_destination(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Unsafe synchronized path: {relative}")
    destination = (root / Path(*pure.parts)).resolve()
    resolved_root = root.resolve()
    if destination != resolved_root and resolved_root not in destination.parents:
        raise ValueError(f"Synchronized path escapes deployment root: {relative}")
    return destination


class AtomicDeployment:
    def __init__(self, deployments_root: Path) -> None:
        self.deployments_root = deployments_root
        self.deployments_root.mkdir(parents=True, exist_ok=True)

    def stage_from_directory(self, app_id: str, source: Path, manifest: SyncManifest) -> Path:
        staging = self.deployments_root / ".staging" / f"{app_id}-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            for entry in manifest.entries:
                source_path = safe_destination(source, entry.path)
                destination = safe_destination(staging, entry.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
            verified = build_manifest(staging)
            if verified.version != manifest.version:
                raise ValueError("Staged deployment checksum does not match its manifest")
            atomic_json_write(staging / ".runner-manifest.json", manifest.to_dict())
            return staging
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def activate(self, app_id: str, staging: Path, version: str) -> Path:
        target = self.deployments_root / app_id / version
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(staging, ignore_errors=True)
            return target
        os.replace(staging, target)
        pointer = target.parent / "active.json"
        atomic_json_write(pointer, {"version": version, "path": str(target)})
        return target


def persistence_ready(config: dict) -> tuple[bool, str]:
    raw_strategy = str(config.get("strategy", "stateless"))
    try:
        strategy = PersistenceStrategy(raw_strategy)
    except ValueError:
        return False, f"Unknown persistence strategy: {raw_strategy}"
    if strategy in {PersistenceStrategy.STATELESS, PersistenceStrategy.EXTERNAL, PersistenceStrategy.REPLICATED}:
        return True, "Persistence strategy is failover-capable"
    if strategy == PersistenceStrategy.SQLITE:
        return False, "Live SQLite files require an application-aware replication strategy"
    if strategy == PersistenceStrategy.CUSTOM:
        return bool(config.get("ready", False)), str(config.get("readiness_reason", "Custom persistence is not verified"))
    return False, "Persistence strategy is unsupported for automatic failover"
