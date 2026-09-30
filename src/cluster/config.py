from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .models import CONFIG_SCHEMA_VERSION, ClusterConfig


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class ConfigStore:
    """Versioned configuration with backup-before-migration semantics."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.cluster_path = runtime_root / "cluster.json"
        self.apps_path = runtime_root / "apps.json"

    def load_cluster(self) -> ClusterConfig:
        if not self.cluster_path.exists():
            return ClusterConfig()
        raw = json.loads(self.cluster_path.read_text(encoding="utf-8-sig"))
        schema = int(raw.get("schema_version", 1))
        if schema > CONFIG_SCHEMA_VERSION:
            raise ValueError(f"Cluster config schema {schema} is newer than supported {CONFIG_SCHEMA_VERSION}")
        if schema < CONFIG_SCHEMA_VERSION:
            raw = self._migrate_cluster(raw, schema)
        fields = ClusterConfig.__dataclass_fields__
        return ClusterConfig(**{key: raw[key] for key in fields if key in raw})

    def save_cluster(self, config: ClusterConfig) -> None:
        payload = config.to_dict()
        payload["schema_version"] = CONFIG_SCHEMA_VERSION
        atomic_json_write(self.cluster_path, payload)

    def migrate_apps(self) -> dict[str, Any]:
        raw = json.loads(self.apps_path.read_text(encoding="utf-8-sig")) if self.apps_path.exists() else {"apps": []}
        apps = raw if isinstance(raw, list) else raw.get("apps", [])
        changed = isinstance(raw, list) or int(raw.get("schema_version", 1)) < CONFIG_SCHEMA_VERSION
        migrated: list[dict[str, Any]] = []
        for app in apps:
            item = dict(app)
            # Compose deployments are no longer supported by Runner. Forget
            # their registry records without issuing any Docker commands or
            # touching their project folders/volumes. The original registry
            # is backed up below before this migration is committed.
            if str(item.get("app_type") or "process") == "docker_compose":
                changed = True
                continue
            item.setdefault("protected", False)
            item.setdefault("service_group", item.get("id", ""))
            item.setdefault("dependencies", [])
            item.setdefault("health_check", {"type": "process", "timeout_seconds": 5})
            item.setdefault("sync", {"enabled": False, "include": [], "exclude": [], "status": "not_configured"})
            item.setdefault("persistence", {"strategy": "stateless", "paths": []})
            item.setdefault("deployments", {})
            item.setdefault("startup_timeout_seconds", 60)
            item.setdefault("app_type", "process")
            migrated.append(item)
        result = {"schema_version": CONFIG_SCHEMA_VERSION, "apps": migrated}
        if changed:
            self._backup(self.apps_path)
            atomic_json_write(self.apps_path, result)
        return result

    def _migrate_cluster(self, raw: dict[str, Any], schema: int) -> dict[str, Any]:
        self._backup(self.cluster_path)
        migrated = dict(raw)
        if schema < 2:
            migrated.setdefault("automatic_failover", False)
            migrated.setdefault("automatic_failback", False)
            migrated.setdefault("transport", "tailscale")
        migrated["schema_version"] = CONFIG_SCHEMA_VERSION
        atomic_json_write(self.cluster_path, migrated)
        return migrated

    def _backup(self, path: Path) -> None:
        if path.exists():
            backup_dir = self.runtime_root / "config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = backup_dir / f"{path.name}.{stamp}.bak"
            suffix = 1
            while target.exists():
                target = backup_dir / f"{path.name}.{stamp}-{suffix}.bak"
                suffix += 1
            shutil.copy2(path, target)
