"""Deployment adapters kept outside Runner's host-process supervisor.

Docker is controlled only by the local Agent/manager.  No Docker socket or
daemon API is exposed by Runner's remote protocol.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerAvailability:
    engine: bool
    compose: bool
    reason: str = ""


def _run_docker_hidden(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run Docker without flashing a console from the windowed Windows Agent.

    Keep this at the provider boundary so every status, inventory, logs, and
    lifecycle Docker invocation has the same bounded, non-interactive policy.
    """
    options: dict[str, Any] = {
        "text": True, "encoding": "utf-8", "errors": "replace",
        "capture_output": True, **kwargs,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startupinfo
    return subprocess.run(command, **options)


@dataclass(frozen=True)
class DockerWorkload:
    """Read-only inventory record for a Docker container.

    Inventory never receives environment values and is safe to expose as
    status metadata.  It is deliberately separate from a Runner-managed
    deployment: discovery must not imply authorization to control it.
    """

    container_id: str
    name: str
    image: str
    state: str
    health: str
    category: str
    compose_project: str | None
    compose_service: str | None
    compose_file: str | None
    working_dir: str | None
    mounts: tuple[dict[str, str], ...]
    ports: tuple[str, ...]
    persistence: tuple[dict[str, str], ...]


def _persistence_classification(source: str, target: str, image: str) -> str:
    """Classify writable state without copying or probing its contents."""
    target_l = target.lower()
    image_l = image.lower()
    if any(term in image_l for term in ("mongo", "redis", "postgres", "mysql", "mariadb")):
        return "database/stateful"
    if any(term in target_l for term in ("/data/db", "/data/configdb", "/var/lib/postgresql", "/var/lib/mysql")):
        return "database/stateful"
    if any(term in target_l for term in ("/app/uploads", "/app/room_state", "/app/training_data", "/app/ai-logs", "/state")):
        return "persistent application state"
    if source and (source.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", source)):
        return "external/shared storage"
    if source:
        return "unknown"
    return "stateless"


def _inventory_record(raw: dict[str, Any]) -> DockerWorkload:
    config = dict(raw.get("Config") or {})
    state_record = raw.get("State") if isinstance(raw.get("State"), dict) else {}
    labels_raw = raw.get("Labels") or raw.get("labels") or config.get("Labels") or {}
    if isinstance(labels_raw, str):
        labels = {}
        for item in labels_raw.split(","):
            key, separator, value = item.partition("=")
            if separator:
                labels[key] = value
    else:
        labels = dict(labels_raw)
    name = str(raw.get("Names") or raw.get("Name") or raw.get("name") or "").lstrip("/")
    image = str(raw.get("Image") or raw.get("image") or config.get("Image") or "")
    state = str(state_record.get("Status") or raw.get("status") or raw.get("State") or raw.get("state") or raw.get("Status") or "unknown").lower()
    health_record = state_record.get("Health") if isinstance(state_record.get("Health"), dict) else {}
    health = str(raw.get("Health") or raw.get("health") or health_record.get("Status") or "unknown").lower()
    if not health and state == "running":
        health = "unknown"
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    compose_file = labels.get("com.docker.compose.project.config_files")
    working_dir = labels.get("com.docker.compose.project.working_dir")
    mounts_raw = raw.get("Mounts") or raw.get("mounts") or []
    mounts: list[dict[str, str]] = []
    persistence: list[dict[str, str]] = []
    for mount in mounts_raw:
        if not isinstance(mount, dict):
            continue
        source = str(mount.get("Source") or mount.get("source") or mount.get("Name") or "")
        target = str(mount.get("Destination") or mount.get("destination") or mount.get("Target") or "")
        kind = _persistence_classification(source, target, image)
        mounts.append({"source": source, "target": target, "type": str(mount.get("Type") or mount.get("type") or "unknown"), "rw": str(bool(mount.get("RW", mount.get("rw", False))))})
        persistence.append({"target": target, "classification": kind})
    ports_raw = raw.get("Ports") or raw.get("ports") or raw.get("NetworkSettings", {}).get("Ports") or {}
    if isinstance(ports_raw, dict):
        ports = tuple(sorted(str(key) for key in ports_raw))
    elif isinstance(ports_raw, list):
        ports = tuple(sorted(str(item) for item in ports_raw))
    else:
        ports = (str(ports_raw),) if ports_raw else ()
    if project:
        category = "managed-compose"
    elif persistence and any(item["classification"] in {"database/stateful", "persistent application state"} for item in persistence):
        category = "stateful-unmanaged"
    else:
        category = "standalone-unmanaged"
    return DockerWorkload(
        container_id=str(raw.get("ID") or raw.get("Id") or raw.get("id") or ""), name=name,
        image=image, state=state, health=health, category=category,
        compose_project=project, compose_service=service,
        compose_file=compose_file, working_dir=working_dir,
        mounts=tuple(mounts), ports=ports, persistence=tuple(persistence),
    )


def inventory_docker_workloads(runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> list[DockerWorkload]:
    """Inventory all containers without changing Docker state.

    This performs only ``docker ps``.  It intentionally does not inspect
    environment variables, logs, volumes, or the Docker socket directly.
    ``runner`` is injectable for isolated tests.
    """
    execute = runner or (lambda command, **kwargs: _run_docker_hidden(command, timeout=kwargs.pop("timeout", 30), **kwargs))
    try:
        result = execute(["docker", "ps", "--all", "--format", "{{json .}}"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"Docker inventory failed: {exc}") from exc
    if result.returncode:
        raise DeploymentError("Docker inventory failed: " + DockerComposeDeploymentProvider._redact((result.stderr or result.stdout or "unknown error").strip()))
    records: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # ``docker ps`` does not include mounts.  A separate read-only inspect is
    # required to classify databases and writable application state correctly.
    ids = [str(item.get("ID") or item.get("Id") or item.get("id") or "") for item in records]
    if ids:
        try:
            inspected = execute(["docker", "inspect", "--format", "{{json .}}", *ids], timeout=30)
            if inspected.returncode == 0:
                by_id: dict[str, dict[str, Any]] = {}
                for line in (inspected.stdout or "").splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    by_id[str(item.get("Id") or item.get("ID") or "")] = item
                for index, record in enumerate(records):
                    detail = by_id.get(ids[index]) or next((value for full_id, value in by_id.items() if full_id.startswith(ids[index])), None)
                    if detail:
                        merged = dict(record)
                        merged.update(detail)
                        records[index] = merged
        except (OSError, subprocess.TimeoutExpired):
            pass
    return [_inventory_record(item) for item in records]


class DockerComposeDeploymentProvider:
    """Structured, local-only Docker Compose v2 adapter.

    ``runner`` is injectable so unit tests never need a Docker daemon.  Every
    destructive operation is scoped to a validated compose file below the
    configured project root and deliberately omits ``down``/volume deletion.
    """
    def __init__(self, spec: dict[str, Any], runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self.spec = dict(spec)
        self.project_root = Path(str(self.spec.get("project_dir") or self.spec.get("cwd") or "")).expanduser()
        compose = str(self.spec.get("compose_file") or "docker-compose.yml")
        self.compose_file = Path(compose) if Path(compose).is_absolute() else self.project_root / compose
        extra_files = [str(value) for value in self.spec.get("compose_overrides", [])]
        self.compose_files = [self.compose_file] + [Path(value) if Path(value).is_absolute() else self.project_root / value for value in extra_files]
        self.project_name = str(self.spec.get("project_name") or self._safe_name(self.spec.get("id") or self.project_root.name))
        self.required_services = [str(item) for item in self.spec.get("required_services", [])]
        self._injected_runner = runner is not None
        self._run_command = runner or self._default_run
        self._warning_cache: tuple[float, list[str]] = (0.0, [])
        self._logs_cache: dict[tuple[str, int], tuple[float, str, bool]] = {}

    @staticmethod
    def _safe_name(value: object) -> str:
        return re.sub(r"[^a-z0-9_-]", "-", str(value).lower()).strip("-") or "runner-compose"

    @staticmethod
    def _default_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _run_docker_hidden(command, timeout=kwargs.pop("timeout", 45), **kwargs)

    def _command(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "-p", self.project_name]
        for compose_file in self.compose_files:
            command.extend(["-f", str(compose_file)])
        command.extend(args)
        try:
            result = self._run_command(command, cwd=str(self.project_root), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"Docker Compose command failed: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "Docker Compose failed").strip()
            raise DeploymentError(self._redact(detail))
        return result

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(password|token|secret|api[_-]?key)=\S+", r"\1=<redacted>", value)
        value = re.sub(r"(?i)(https?://[^\s/]*/bot)\d+:[A-Za-z0-9_-]+", r"\1<redacted>", value)
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1<redacted>", value)

    def warning_summary(self, *, max_age_seconds: float = 60.0) -> list[str]:
        """Return generic recent log warnings without returning log content."""
        now = time.monotonic()
        if now - self._warning_cache[0] < max_age_seconds:
            return list(self._warning_cache[1])
        warnings: list[str] = []
        try:
            text = self._command("logs", "--no-color", "--tail", "120", timeout=30).stdout
            if re.search(r"(?i)(sslerror|certificate verify failed|tls handshake|ssleoferror|httpsconnectionpool.*max retries)", text):
                for service in self.services():
                    if re.search(rf"(?i)\b{re.escape(service)}\b", text):
                        warnings.append(f"{service}: recent SSL/TLS connection failure detected")
                if not warnings:
                    warnings.append("Recent SSL/TLS connection failure detected in Compose logs")
        except DeploymentError:
            pass
        self._warning_cache = (now, warnings)
        return list(warnings)

    def availability(self) -> DockerAvailability:
        if not self._injected_runner and not shutil.which("docker"):
            return DockerAvailability(False, False, "Docker Engine CLI is not installed")
        try:
            engine = self._run_command(["docker", "info", "--format", "{{json .ServerVersion}}"], timeout=10)
            if engine.returncode:
                return DockerAvailability(False, False, self._redact((engine.stderr or "Docker Engine is unavailable").strip()))
            compose = self._run_command(["docker", "compose", "version", "--format", "json"], timeout=10)
            if compose.returncode:
                return DockerAvailability(True, False, "Docker Compose v2 is not available")
            return DockerAvailability(True, True)
        except Exception as exc:
            return DockerAvailability(False, False, str(exc))

    def validate(self) -> dict[str, Any]:
        if not self.project_root.is_dir():
            raise DeploymentError(f"Compose project directory does not exist: {self.project_root}")
        try:
            for compose_file in self.compose_files:
                compose_file.resolve().relative_to(self.project_root.resolve())
        except ValueError as exc:
            raise DeploymentError("Compose file must be inside the approved project directory") from exc
        for compose_file in self.compose_files:
            if not compose_file.is_file():
                raise DeploymentError(f"Compose file does not exist: {compose_file}")
        result = self._command("config", "--format", "json")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DeploymentError("Docker Compose did not return structured configuration") from exc
        services = payload.get("services", {})
        if not isinstance(services, dict) or not services:
            raise DeploymentError("Compose project has no services")
        missing = sorted(set(self.required_services) - set(services))
        if missing:
            raise DeploymentError("Required Compose services are missing: " + ", ".join(missing))
        return payload

    def services(self) -> list[str]:
        return sorted(self.validate()["services"])

    def inspect(self) -> dict[str, Any]:
        """Return normalized container state; missing containers are stopped."""
        config = self.validate()
        result = self._command("ps", "--all", "--format", "json")
        records: list[dict[str, Any]] = []
        text = result.stdout.strip()
        if text:
            try:
                parsed = json.loads(text)
                records = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
        by_service = {str(item.get("Service") or item.get("service") or ""): item for item in records}
        services: dict[str, dict[str, Any]] = {}
        for name, definition in config["services"].items():
            raw = by_service.get(name, {})
            state = str(raw.get("State") or raw.get("state") or "stopped").lower()
            status = str(raw.get("Status") or raw.get("status") or state)
            health = str(raw.get("Health") or "").lower()
            if not health:
                health = "healthy" if state == "running" and not definition.get("healthcheck") else "unknown"
            services[name] = {
                "state": state, "health": health, "status": status,
                "container_id": str(raw.get("ID") or raw.get("id") or ""),
                "image": str(raw.get("Image") or definition.get("image") or "build"),
                "ports": raw.get("Publishers") or raw.get("Ports") or [],
                "depends_on": sorted((definition.get("depends_on") or {}).keys()) if isinstance(definition.get("depends_on"), dict) else list(definition.get("depends_on") or []),
            }
        return {"services": services, "running": any(item["state"] == "running" for item in services.values()), "checked_at": time.time()}

    def classify_mounts(self) -> list[dict[str, str]]:
        config = self.validate()
        output: list[dict[str, str]] = []
        syncable = {str(item).replace("\\", "/").strip("/") for item in self.spec.get("syncable_paths", [])}
        for service, definition in config["services"].items():
            image = str(definition.get("image") or "").lower()
            for mount in definition.get("volumes", []) or []:
                source = mount.get("source", "") if isinstance(mount, dict) else str(mount).split(":", 1)[0]
                target = mount.get("target", "") if isinstance(mount, dict) else (str(mount).split(":", 2)[1] if ":" in str(mount) else "")
                kind = "unknown"
                if any(term in image for term in ("mongo", "redis", "postgres", "mysql", "mariadb")):
                    kind = "database/stateful"
                elif not source:
                    kind = "stateless"
                elif Path(source).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", source):
                    try:
                        relative = Path(source).resolve().relative_to(self.project_root.resolve()).as_posix()
                        kind = "syncable files" if relative in syncable else "unknown"
                    except (OSError, ValueError):
                        kind = "external/shared storage"
                elif str(source).replace("\\", "/").lstrip("./").strip("/") in syncable:
                    kind = "syncable files"
                elif source:
                    kind = "unknown"
                output.append({"service": service, "source": source, "target": target, "classification": kind})
        return output

    def required_secret_names(self) -> list[str]:
        result: set[str] = set(str(item) for item in self.spec.get("required_secrets", []))
        config = self.validate()
        for definition in config["services"].values():
            environment = definition.get("environment") or {}
            values = environment.values() if isinstance(environment, dict) else environment
            for value in values:
                result.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", str(value)))
        return sorted(result)

    def readiness(self, available_secrets: set[str], *, require_stopped: bool = True) -> tuple[bool, list[str], dict[str, Any]]:
        reasons: list[str] = []
        availability = self.availability()
        if not availability.engine: reasons.append(availability.reason)
        elif not availability.compose: reasons.append(availability.reason)
        try:
            self.validate()
            if self.spec.get("require_healthchecks"):
                config = self.validate()
                missing_health = sorted(name for name, definition in config["services"].items() if not definition.get("healthcheck"))
                if missing_health:
                    reasons.append("Required Compose health checks are missing: " + ", ".join(missing_health))
            mounts = self.classify_mounts()
            unsafe = [item for item in mounts if item["classification"] in {"database/stateful", "unknown"}]
            if unsafe: reasons.append("Unsafe or unclassified Compose mounts: " + ", ".join(f"{item['service']}:{item['target']}" for item in unsafe))
            missing = sorted(set(self.required_secret_names()) - available_secrets)
            if missing: reasons.append("Required Compose secrets are missing: " + ", ".join(missing))
            state = self.inspect()
            if require_stopped and state["running"]: reasons.append("Standby Compose containers are running and must be stopped before readiness")
        except DeploymentError as exc:
            reasons.append(str(exc))
            mounts, state = [], {"services": {}, "running": False}
        return not reasons, reasons, {"availability": availability.__dict__, "mounts": mounts, **state}

    def prepare(self, available_secrets: set[str], *, pull: bool = True, build: bool = True) -> dict[str, Any]:
        ready, reasons, state = self.readiness(available_secrets, require_stopped=True)
        if state.get("running"):
            raise DeploymentError("Refusing standby preparation: Compose containers are already running")
        if reasons and not any("images" in reason.lower() for reason in reasons):
            raise DeploymentError("; ".join(reasons))
        if pull: self._command("pull", timeout=600)
        if build: self._command("build", timeout=900)
        ready, reasons, state = self.readiness(available_secrets, require_stopped=True)
        if not ready: raise DeploymentError("; ".join(reasons))
        return state

    def start(self) -> dict[str, Any]:
        self._command("up", "-d", timeout=180)
        deadline = time.monotonic() + float(self.spec.get("health_timeout_seconds", 90))
        last = self.inspect()
        while time.monotonic() < deadline:
            services = last["services"].values()
            allowed_health = {"healthy"} if self.spec.get("require_healthchecks") else {"healthy", "unknown"}
            if services and all(item["state"] == "running" and item["health"] in allowed_health for item in services):
                return last
            if any(item["state"] in {"exited", "dead", "unhealthy"} or item["health"] == "unhealthy" for item in services):
                break
            time.sleep(0.5)
            last = self.inspect()
        raise DeploymentError("Compose services did not become healthy before the startup timeout")

    def stop(self) -> dict[str, Any]:
        # Never use down: networks, volumes and unrelated containers survive.
        self._command("stop", timeout=90)
        return self.inspect()

    def restart(self) -> dict[str, Any]:
        self._command("restart", timeout=120)
        return self.inspect()

    def logs(self, service: str | None = None, tail: int = 500) -> str:
        bounded_tail = max(1, min(tail, 2000))
        cache_key = (service or "", bounded_tail)
        cached = self._logs_cache.get(cache_key)
        if cached and time.monotonic() < cached[0]:
            if cached[2]:
                raise DeploymentError(cached[1])
            return cached[1]
        try:
            allowed = set(self.services())
            if service and service not in allowed:
                raise DeploymentError("Unknown Compose service")
            args = ["logs", "--no-color", "--tail", str(bounded_tail)]
            if service:
                args.append(service)
            value = self._redact(self._command(*args).stdout)
        except DeploymentError as exc:
            # Docker Desktop can be down for hours. Avoid executing the same
            # doomed CLI request on every 1.8-second UI log refresh.
            self._logs_cache[cache_key] = (time.monotonic() + 30.0, str(exc), True)
            raise
        self._logs_cache[cache_key] = (time.monotonic() + 2.0, value, False)
        return value
