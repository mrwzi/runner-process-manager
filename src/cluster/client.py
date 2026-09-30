from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

import psutil
from PySide6.QtCore import QObject, Signal
from manager import ProcessManager


class AgentUnavailable(RuntimeError):
    pass


class AgentClient(QObject):
    """Responsive local Agent proxy with non-blocking health supervision."""

    state_changed = Signal(str, object)
    batch_state_changed = Signal(bool, str)
    error_occurred = Signal(str, str)
    registry_changed = Signal(object)
    connection_changed = Signal(str, str)
    remote_managed = True

    HEALTH_INTERVAL = 2.0
    APP_REFRESH_INTERVAL = 8.0
    STARTUP_GRACE_SECONDS = 12.0
    RESTART_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 30.0)

    def __init__(
        self,
        runtime_root: Path,
        base_url: str = "http://127.0.0.1:47471",
        *,
        fallback_apps: list[dict[str, Any]] | None = None,
        agent_command: list[str] | None = None,
        autostart: bool = True,
        restart_callback: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        super().__init__()
        self.runtime_root = Path(runtime_root)
        parsed = urlsplit(base_url)
        self.base_url = base_url.rstrip("/")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.token_path = self.runtime_root / "agent-api.json"
        self.process_record_path = self.runtime_root / "agent-process.json"
        self.agent_command = list(agent_command or [])
        self._restart_callback = restart_callback
        self._token = ""
        self._token_lock = threading.Lock()
        self._snapshots = {str(item.get("id")): self._fallback_snapshot(item) for item in (fallback_apps or []) if item.get("id")}
        self._snapshot_lock = threading.RLock()
        self._local_definitions = {
            str(item.get("id")): self._config_only(item)
            for item in (fallback_apps or [])
            if item.get("id") and not item.get("protected")
        }
        self._local_verified: set[str] = set()
        self.local_manager: ProcessManager | None = None
        self._local_manager_lock = threading.RLock()
        self._pending_local_operations: list[tuple[str, str, tuple[Any, ...]]] = []
        self._local_init_thread: threading.Thread | None = None
        self._logs: dict[str, str] = {}
        self._agent_status: dict[str, Any] = {
            "mode": "cluster", "role": "unknown", "applications": list(self._snapshots.values()),
            "automatic_failover_eligible": False,
            "automatic_failover_reasons": ["Runner Agent is starting"],
        }
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._state = "starting"
        self._state_message = "Runner Agent is starting"
        self.snapshots_ready = False
        self._started_at = time.monotonic()
        self._startup_deadline = self._started_at + self.STARTUP_GRACE_SECONDS
        self._next_restart = self._started_at + self.RESTART_BACKOFF_SECONDS[0]
        self._restart_count = 0
        self._restart_pending_until = 0.0
        self._last_agent_pid: int | None = None
        self._agent_recorded_host = "unknown"
        self._agent_recorded_port: int | None = None
        self._last_failure_key = ""
        self._last_failure_logged = 0.0
        self._last_apps_refresh = 0.0
        self._refresh_lock = threading.Lock()
        self._background_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="runner-agent-client")
        self._background_slots = threading.BoundedSemaphore(8)
        self._background_lock = threading.Lock()
        self._background_active: set[str] = set()
        self._logger = self._make_logger()
        self._logger.info("Agent client initialized host=%s port=%s process_exists=%s pid=%s", self.host, self.port, *self._agent_process_info())
        self._thread: threading.Thread | None = None
        self._start_local_manager_init()
        if autostart:
            self._thread = threading.Thread(target=self._poll, name="runner-gui-agent-health", daemon=True)
            self._thread.start()

    @staticmethod
    def _fallback_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(item)
        snapshot.setdefault("name", snapshot.get("id", "Application"))
        snapshot.setdefault("app_type", snapshot.get("type", "python"))
        # App definitions contain no observed process state. Never turn a
        # disconnected Agent into a fabricated "Stopped" state.
        snapshot["status"] = "Unknown"
        snapshot["state_verified"] = False
        snapshot["local_control"] = not bool(snapshot.get("protected"))
        snapshot.setdefault("pid", None)
        snapshot.setdefault("cpu_percent", None)
        snapshot.setdefault("ram_mb", None)
        snapshot.setdefault("uptime_seconds", None)
        snapshot.setdefault("pending_action", False)
        snapshot.setdefault("last_exit_code", None)
        snapshot.setdefault("last_error", "")
        snapshot.setdefault("log_file_path", "")
        snapshot.setdefault("start_allowed", not bool(snapshot.get("protected")))
        return snapshot

    @staticmethod
    def _config_only(item: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id", "name", "runner_path", "args", "cwd", "env", "startup_input",
            "interactive", "visible_console", "auto_start", "protected", "service_group",
            "dependencies", "health_check", "persistence", "deployments", "sync",
            "startup_timeout_seconds", "app_type", "compose",
        )
        return {key: item[key] for key in keys if key in item}

    def _on_local_state_changed(self, app_id: str, snapshot: dict[str, Any]) -> None:
        with self._snapshot_lock:
            self._local_verified.add(str(app_id))
        value = dict(snapshot)
        value["state_verified"] = True
        value["local_control"] = True
        self.state_changed.emit(str(app_id), value)

    def _ensure_local_manager(self) -> ProcessManager | None:
        # Snapshot inputs while holding the small coordination lock, then do
        # filesystem setup and OS process inspection without it. UI actions
        # briefly take this lock to enqueue local work and must not wait for
        # psutil reconciliation.
        with self._local_manager_lock:
            if self.local_manager is not None or not self._local_definitions:
                return self.local_manager
            definitions = [dict(value) for value in self._local_definitions.values()]
            snapshots_ready = self.snapshots_ready
            snapshots = {key: dict(value) for key, value in self._snapshots.items()}

        manager = ProcessManager(
            definitions,
            logs_dir=self.runtime_root / "logs" / "gui-local-control",
            reconcile_existing=False,
            # The Agent already reconciles the machine-wide process table.
            # The GUI fallback verifies adopted PIDs directly and performs a
            # fresh duplicate check when Start is requested. A second 30s
            # process-table scan in the GUI can starve Qt on busy servers.
            monitor_external_processes=False,
        )
        manager.state_changed.connect(self._on_local_state_changed)
        manager.batch_state_changed.connect(lambda active, message: self.batch_state_changed.emit(active, message))
        manager.error_occurred.connect(lambda app_id, message: self.error_occurred.emit(app_id, message))
        verified: set[str] = set()
        if snapshots_ready:
            for app_id in self._local_definitions:
                previous = snapshots.get(app_id)
                if previous and manager.adopt_observed_snapshot(previous):
                    verified.add(app_id)

        duplicate_manager = False
        with self._local_manager_lock:
            # Initialization has one dedicated creator; this check keeps a
            # future alternate caller from replacing an already-live manager.
            if self.local_manager is not None:
                existing_manager = self.local_manager
                pending = []
                duplicate_manager = True
            else:
                self.local_manager = manager
                existing_manager = manager
                pending, self._pending_local_operations = self._pending_local_operations, []
        if duplicate_manager:
            manager.shutdown(stop_applications=False)
            return existing_manager
        if verified:
            with self._snapshot_lock:
                self._local_verified.update(verified)
        for app_id, method, args in pending:
            if app_id == "*":
                getattr(manager, method)(*args)
            elif app_id in self._local_definitions:
                getattr(manager, method)(app_id, *args)
        return manager

    def _start_local_manager_init(self) -> None:
        with self._local_manager_lock:
            if self.local_manager is None and self._local_definitions and self._local_init_thread is None:
                self._local_init_thread = threading.Thread(target=self._ensure_local_manager, name="runner-local-manager-init", daemon=True)
                self._local_init_thread.start()

    def _queue_local_operation(self, app_id: str, method: str, *args: Any) -> bool:
        # When the Agent has a verified live snapshot, it is the authoritative
        # owner of the process handles (including processes adopted from
        # outside Runner). The GUI fallback can only control PIDs it has
        # independently matched, so use it only while the Agent is unavailable.
        if self.connection_state == "connected" and self.snapshots_ready:
            return False
        if app_id not in self._local_definitions:
            return False
        with self._local_manager_lock:
            manager = self.local_manager
            if manager is None:
                if len(self._pending_local_operations) < 16:
                    self._pending_local_operations.append((app_id, method, args))
                return True
        getattr(manager, method)(app_id, *args)
        return True

    def _queue_batch_local_operation(self, method: str) -> None:
        with self._local_manager_lock:
            manager = self.local_manager
            if manager is None and self._local_definitions:
                if len(self._pending_local_operations) < 16:
                    self._pending_local_operations.append(("*", method, ()))
                self._start_local_manager_init()
                return
        if manager is not None:
            getattr(manager, method)()

    def _on_local_refresh_complete(self, success: bool) -> None:
        if not success or not self.local_manager:
            return
        for app_id in self._local_definitions:
            with self._snapshot_lock:
                self._local_verified.add(app_id)
            value = self.local_manager.snapshot(app_id)
            value["state_verified"] = True
            value["local_control"] = True
            self.state_changed.emit(app_id, value)

    def _make_logger(self) -> logging.Logger:
        logger_name = f"runner.agent.client.{abs(hash(str(self.runtime_root.resolve())))}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                log_dir = self.runtime_root / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    log_dir / "agent-connection.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
                )
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                logger.addHandler(handler)
            except OSError:
                logger.addHandler(logging.NullHandler())
        return logger

    @property
    def connection_state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def connection_message(self) -> str:
        with self._state_lock:
            return self._state_message

    def _set_connection(self, state: str, message: str) -> None:
        with self._state_lock:
            if (state, message) == (self._state, self._state_message):
                return
            previous = self._state
            self._state, self._state_message = state, message
        if state != "connected":
            self._start_local_manager_init()
        if state == "connected" and previous != "connected":
            self._logger.info("Agent reconnection successful pid=%s host=%s port=%s", self._last_agent_pid, self.host, self.port)
        else:
            self._logger.info("Agent state=%s previous=%s pid=%s host=%s port=%s detail=%s", state, previous, self._last_agent_pid, self.host, self.port, message)
        self.connection_changed.emit(state, message)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._health_tick()
            if self.connection_state == "connected" and time.monotonic() - self._last_apps_refresh >= self.APP_REFRESH_INTERVAL:
                self._start_snapshot_refresh()
            self._stop.wait(self.HEALTH_INTERVAL)

    def _health_tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        try:
            health = self._request_json("GET", "/v1/health", timeout=1.5)
            if not health.get("ok") or not health.get("node_id"):
                raise AgentUnavailable("Agent returned an invalid local health response")
            try:
                self._last_agent_pid = int(health.get("pid"))
            except (TypeError, ValueError):
                self._last_agent_pid = None
            self._restart_count = 0
            self._next_restart = now + self.RESTART_BACKOFF_SECONDS[0]
            self._restart_pending_until = 0.0
            self._set_connection("connected", "Runner Agent connected")
            return
        except Exception as exc:
            kind = self._failure_kind(exc)
            process_exists, pid = self._agent_process_info(check_listener="refused" not in kind.lower())
            self._last_agent_pid = pid
            if process_exists and self._agent_recorded_port and self._agent_recorded_port != self.port:
                kind = f"Wrong port (Agent listens on {self._agent_recorded_host}:{self._agent_recorded_port})"
            detail = f"{kind}; configured_host={self.host} configured_port={self.port}; agent_bind={self._agent_recorded_host}:{self._agent_recorded_port or 'unknown'}; pid={pid or 'unknown'}; process_exists={process_exists}"
            key = f"{type(exc).__name__}:{exc}:{process_exists}:{pid}"
            if key != self._last_failure_key or now - self._last_failure_logged >= 30.0:
                self._logger.warning("Agent health attempt failed result=%s error=%r pid=%s process_exists=%s configured_host=%s configured_port=%s agent_bind=%s:%s", kind, exc, pid, process_exists, self.host, self.port, self._agent_recorded_host, self._agent_recorded_port or "unknown")
                self._last_failure_key, self._last_failure_logged = key, now

            if now < self._startup_deadline:
                self._set_connection("starting", f"Runner Agent starting ({kind.lower()})")
            elif process_exists:
                self._set_connection("unhealthy", f"Runner Agent process is present but not responding ({kind.lower()})")
            else:
                self._set_connection("reconnecting", f"Runner Agent process is missing; reconnecting ({kind.lower()})")
                if now >= self._next_restart and now >= self._restart_pending_until:
                    self._request_restart(pid)

    @staticmethod
    def _failure_kind(exc: Exception) -> str:
        candidates: list[BaseException] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            candidates.append(current)
            if isinstance(current, urllib.error.URLError) and getattr(current, "reason", None) is not None:
                candidates.append(current.reason)
            current = current.__cause__ or current.__context__
        for value in candidates:
            if isinstance(value, (TimeoutError, socket.timeout)) or "timed out" in str(value).lower():
                return "Timed out"
            if "10061" in str(value) or isinstance(value, OSError) and getattr(value, "winerror", None) == 10061:
                return "Connection refused (WinError 10061)"
            if isinstance(value, ConnectionRefusedError):
                return "Connection refused"
        if "not initialized" in str(exc).lower():
            return "Agent not initialized"
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTP {exc.code}"
        return "Agent unhealthy"

    def _read_token(self) -> str:
        with self._token_lock:
            if self._token:
                return self._token
            try:
                payload = json.loads(self.token_path.read_text(encoding="utf-8"))
                self._token = str(payload["token"])
            except (OSError, KeyError, ValueError, TypeError) as exc:
                raise AgentUnavailable("Runner Agent is not initialized on this machine") from exc
            return self._token

    def _request_json(self, method: str, path: str, payload: dict | None = None, *, timeout: float = 10.0) -> Any:
        body = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=body if method == "POST" else None,
            method=method,
            headers={"Authorization": f"Bearer {self._read_token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise AgentUnavailable(f"Runner Agent is unavailable: {exc}") from exc

    def _agent_process_info(self, *, check_listener: bool = True) -> tuple[bool, int | None]:
        """Validate PID records against OS process creation time; ignore stale PID reuse."""
        try:
            record = json.loads(self.process_record_path.read_text(encoding="utf-8"))
            pid = int(record.get("pid", 0))
            self._agent_recorded_host = str(record.get("host") or "unknown")
            self._agent_recorded_port = int(record.get("port")) if record.get("port") is not None else None
            expected_start = float(record.get("process_started_at", 0))
            process = psutil.Process(pid)
            alive = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
            if alive and expected_start:
                alive = abs(process.create_time() - expected_start) < 3.0
            if alive:
                return True, pid
        except (OSError, ValueError, TypeError, psutil.Error, json.JSONDecodeError):
            pass
        # An old Agent build has no PID record. A live listener is stronger
        # evidence than a possibly stale PID file and prevents duplicate starts.
        if check_listener:
            try:
                for connection in psutil.net_connections(kind="tcp"):
                    if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == self.port and connection.pid:
                        return True, int(connection.pid)
            except (OSError, psutil.Error):
                pass
        return False, None

    def _request_restart(self, pid: int | None) -> None:
        delay = self.RESTART_BACKOFF_SECONDS[min(self._restart_count, len(self.RESTART_BACKOFF_SECONDS) - 1)]
        self._restart_count += 1
        now = time.monotonic()
        self._next_restart = now + delay
        self._logger.warning("Agent restart attempt=%s pid=%s host=%s port=%s", self._restart_count, pid, self.host, self.port)
        def restart() -> None:
            try:
                if self._restart_callback:
                    success, detail = self._restart_callback()
                else:
                    success, detail = self._restart_agent_process()
                self._logger.info("Agent restart result=%s detail=%s pid=%s", success, detail, pid)
                if success:
                    self._restart_pending_until = time.monotonic() + self.STARTUP_GRACE_SECONDS
            except Exception as exc:
                self._logger.exception("Agent restart failed pid=%s error=%r", pid, exc)
        self._submit_background("agent-restart", restart)

    def _submit_background(self, key: str, operation: Callable[[], None]) -> bool:
        with self._background_lock:
            if key in self._background_active or not self._background_slots.acquire(blocking=False):
                self._logger.warning("Background Agent work coalesced/dropped key=%s", key)
                return False
            self._background_active.add(key)
        def run() -> None:
            try:
                operation()
            finally:
                with self._background_lock:
                    self._background_active.discard(key)
                self._background_slots.release()
        try:
            self._background_executor.submit(run)
            return True
        except RuntimeError:
            with self._background_lock:
                self._background_active.discard(key)
            self._background_slots.release()
            return False

    def _restart_agent_process(self) -> tuple[bool, str]:
        if os.name == "nt":
            query = subprocess.run(
                ["schtasks", "/Query", "/TN", "Runner Agent"], capture_output=True, text=True, timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if query.returncode == 0:
                result = subprocess.run(
                    ["schtasks", "/Run", "/TN", "Runner Agent"], capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0:
                    return True, "scheduled task start requested"
                raise RuntimeError((result.stderr or result.stdout or "Runner Agent task could not be started").strip())
        elif Path("/usr/bin/systemctl").exists():
            result = subprocess.run(["systemctl", "start", "runner-agent.service"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True, "systemd start requested"
        if not self.agent_command:
            raise RuntimeError("Agent task/service is missing and no safe Agent launch command is available")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            self.agent_command, cwd=str(self.runtime_root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=creationflags,
        )
        return True, "detached Agent process launched because task/service is absent"

    def _start_snapshot_refresh(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        def worker() -> None:
            try:
                self._refresh(timeout=20.0)
            except Exception as exc:
                # A slow readiness/status payload is a data refresh problem,
                # not evidence that the local Agent process has died.
                self._logger.warning("Agent application snapshot refresh failed pid=%s host=%s port=%s error=%r", self._last_agent_pid, self.host, self.port, exc)
            finally:
                # Bound failed snapshot retries too; repeatedly issuing a
                # slow status read can otherwise consume the Agent's request
                # pool while liveness is already known independently.
                self._last_apps_refresh = time.monotonic()
                self._refresh_lock.release()
        if not self._submit_background("application-snapshot-refresh", worker):
            self._refresh_lock.release()

    def app_definitions(self) -> list[dict[str, Any]]:
        with self._snapshot_lock:
            snapshots = {key: dict(value) for key, value in self._snapshots.items()}
            local_verified = set(self._local_verified)
        if self.local_manager:
            for app_id in self._local_definitions:
                if app_id in local_verified:
                    value = self.local_manager.snapshot(app_id)
                    value["state_verified"] = True
                    value["local_control"] = True
                    snapshots[app_id] = value
                elif self.connection_state != "connected":
                    value = dict(snapshots.get(app_id, {}))
                    if value:
                        value["last_known_status"] = value.get("status")
                        value["status"] = "Unknown"
                        value["state_verified"] = False
                        value["state_stale"] = True
                        value["local_control"] = True
                        snapshots[app_id] = value
        return list(snapshots.values())

    def export_apps(self) -> list[dict[str, Any]]:
        return self.app_definitions()

    def snapshot(self, app_id: str) -> dict[str, Any]:
        if self.local_manager and app_id in self._local_definitions:
            with self._snapshot_lock:
                verified = app_id in self._local_verified
            if verified:
                value = self.local_manager.snapshot(app_id)
                value["state_verified"] = True
                value["local_control"] = True
                return value
        with self._snapshot_lock:
            value = dict(self._snapshots[app_id])
        if self.local_manager and app_id in self._local_definitions and self.connection_state != "connected":
            value["last_known_status"] = value.get("status")
            value["status"] = "Unknown"
            value["state_verified"] = False
            value["state_stale"] = True
            value["local_control"] = True
            return value
        value["state_stale"] = self.connection_state != "connected" and self.snapshots_ready
        value["state_verified"] = bool(self.snapshots_ready)
        return value

    def requires_agent(self, app_id: str) -> bool:
        return bool(self._snapshots.get(app_id, {}).get("protected"))

    def start_app(self, app_id: str) -> None:
        record = self._snapshots[app_id]
        if self._queue_local_operation(app_id, "start_app"):
            return
        remote_owner = record.get("owner_node_id") and not record.get("local_owner")
        path = "/v1/ownership/transfer-here" if remote_owner else ("/v1/ownership/acquire-and-start" if record.get("protected") else "/v1/apps/start")
        self._async_post(path, {"app_ids": [app_id]})
    def stop_app(self, app_id: str) -> None:
        if self._queue_local_operation(app_id, "stop_app"):
            return
        else:
            self._async_post("/v1/apps/stop", {"app_ids": [app_id]})
    def restart_app(self, app_id: str) -> None:
        if self._queue_local_operation(app_id, "restart_app"):
            return
        else:
            self._async_post("/v1/apps/restart", {"app_ids": [app_id]})
    def force_stop_app(self, app_id: str) -> None:
        if self._queue_local_operation(app_id, "force_stop_app"):
            return
        else:
            self._async_post("/v1/apps/force-stop", {"app_ids": [app_id]})
    def send_input(self, app_id: str, text: str, append_newline: bool = True) -> None:
        if self._queue_local_operation(app_id, "send_input", text, append_newline):
            return
        else:
            self._async_post("/v1/apps/input", {"app_id": app_id, "text": text})
    def start_all(self) -> None:
        if not self._agent_available_for_protected_actions():
            self._queue_batch_local_operation("start_all")
            return
        local = [app_id for app_id, value in self._snapshots.items() if not value.get("protected")]
        if local:
            self._async_post("/v1/apps/start", {"app_ids": local})
        protected = [app_id for app_id, value in self._snapshots.items() if value.get("protected")]
        if protected:
            self._async_post("/v1/ownership/acquire-and-start", {"app_ids": protected})
    def stop_all(self) -> None:
        if not self._agent_available_for_protected_actions():
            self._queue_batch_local_operation("stop_all")
            return
        local = [app_id for app_id, value in self._snapshots.items() if not value.get("protected")]
        if local:
            self._async_post("/v1/apps/stop", {"app_ids": local})
        protected = [app_id for app_id, value in self._snapshots.items() if value.get("protected")]
        if protected:
            self._async_post("/v1/apps/stop", {"app_ids": protected})

    def _agent_available_for_protected_actions(self) -> bool:
        return self.connection_state == "connected" and self.snapshots_ready
    def start_auto_start_apps(self) -> None: return
    def refresh_all(self) -> None:
        local = self.local_manager
        if local:
            local.refresh_all(on_complete=self._on_local_refresh_complete)
        self._start_snapshot_refresh()
    def clear_log_cache(self, app_id: str) -> None:
        self._logs[app_id] = ""
        if self._agent_available_for_protected_actions():
            return
        local = self.local_manager
        if local and app_id in self._local_definitions:
            local.clear_log_cache(app_id)
    def has_live_log_output(self, app_id: str) -> bool:
        if self._agent_available_for_protected_actions():
            return bool(self._logs.get(app_id))
        local = self.local_manager
        return bool(local and app_id in self._local_definitions and local.has_live_log_output(app_id)) or bool(self._logs.get(app_id))
    def drain_pending_log_lines(self, app_id: str) -> list[str]:
        if self._agent_available_for_protected_actions():
            return []
        local = self.local_manager
        if local and app_id in self._local_definitions:
            return local.drain_pending_log_lines(app_id)
        return []
    def get_log_cache_text(self, app_id: str) -> str:
        # The local manager is only a degraded-mode control fallback. When
        # connected, the Agent owns the real process pipes and log cache;
        # preferring the fallback here showed placeholder text such as
        # "Idle" instead of the managed app's actual output.
        if self._agent_available_for_protected_actions():
            result = self._request_json("GET", f"/v1/logs/{app_id}", timeout=20.0)
            self._logs[app_id] = str(result.get("text", ""))
            return self._logs[app_id]
        local = self.local_manager
        if local and app_id in self._local_definitions:
            return local.get_log_cache_text(app_id)
        if app_id in self._local_definitions:
            return "Local process monitor is initializing.\n"
        result = self._request_json("GET", f"/v1/logs/{app_id}", timeout=20.0)
        self._logs[app_id] = str(result.get("text", ""))
        return self._logs[app_id]
    def add_app(self, app: dict[str, Any]) -> dict[str, Any]:
        self._request_json("POST", "/v1/apps/add", {"app": app}); self._refresh(); return self._snapshots[str(app["id"])]
    def update_app(self, app_id: str, app: dict[str, Any]) -> dict[str, Any]:
        self._request_json("POST", "/v1/apps/update", {"app_id": app_id, "app": app}); self._refresh(); return self._snapshots[app_id]
    def remove_app(self, app_id: str) -> None:
        self._request_json("POST", "/v1/apps/remove", {"app_id": app_id}); self._refresh()
    def create_pairing_offer(self) -> dict[str, Any]: return self._request_json("POST", "/v1/pairing/offer", {})
    def pair_remote(self, endpoint: str, code: str) -> dict[str, Any]:
        result = self._request_json("POST", "/v1/pairing/connect", {"endpoint": endpoint, "code": code}); self._refresh(); return result
    def remove_peer(self, node_id: str) -> dict[str, Any]:
        result = self._request_json("POST", "/v1/pairing/remove", {"node_id": node_id}); self._refresh(); return result
    def update_cluster(self, values: dict[str, Any]) -> dict[str, Any]:
        result = self._request_json("POST", "/v1/cluster/config", values); self._refresh(); return result
    def configure_witness(self, url: str, shared_secret: str) -> dict[str, Any]:
        result = self._request_json("POST", "/v1/witness/configure", {"url": url, "shared_secret": shared_secret}); self._refresh(); return result

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        local_init = self._local_init_thread
        if local_init and local_init is not threading.current_thread():
            local_init.join(timeout=2)
        if self.local_manager:
            self.local_manager.shutdown(stop_applications=False)
        self._background_executor.shutdown(wait=False, cancel_futures=True)
        for handler in list(self._logger.handlers):
            try:
                handler.flush()
                handler.close()
            finally:
                self._logger.removeHandler(handler)

    def agent_status(self) -> dict[str, Any]:
        return dict(self._agent_status)

    def _refresh(self, timeout: float = 20.0) -> None:
        response = self._request_json("GET", "/v1/apps", timeout=timeout)
        records = response["applications"]
        self._agent_status = dict(response.get("cluster", {}))
        new = {record["id"]: record for record in records}
        changed_registry = set(new) != set(self._snapshots)
        with self._snapshot_lock:
            previous = self._snapshots
        self._snapshots = new
        self._sync_local_definitions(records)
        if self.local_manager:
            for app_id in self._local_definitions:
                if app_id not in self._local_verified and app_id in new:
                    if self.local_manager.adopt_observed_snapshot(new[app_id]):
                        with self._snapshot_lock:
                            self._local_verified.add(app_id)
        if changed_registry:
            self.registry_changed.emit(records)
        for app_id, record in new.items():
            if previous.get(app_id) != record:
                self.state_changed.emit(app_id, record)
        first_ready = not self.snapshots_ready
        self.snapshots_ready = True
        if first_ready and self.connection_state == "connected":
            self._set_connection("connected", "Runner Agent connected; application state loaded")

    def _sync_local_definitions(self, records: list[dict[str, Any]]) -> None:
        desired = {
            str(item.get("id")): self._config_only(item)
            for item in records
            if item.get("id") and not item.get("protected")
        }
        if self.local_manager is None and desired:
            with self._local_manager_lock:
                self._local_definitions = desired
                self._start_local_manager_init()
            return
        if self.local_manager is None:
            self._local_definitions = desired
            return
        for app_id in set(self._local_definitions) - set(desired):
            try:
                self.local_manager.remove_app(app_id)
            except (KeyError, ValueError):
                self._logger.warning("Could not remove local fallback definition for %s", app_id)
            self._local_verified.discard(app_id)
            self._local_definitions.pop(app_id, None)
        for app_id, definition in desired.items():
            try:
                if app_id not in self._local_definitions:
                    self.local_manager.add_app(definition)
                    self._local_definitions[app_id] = definition
                elif self._local_definitions[app_id] != definition:
                    self.local_manager.update_app(app_id, definition)
                    self._local_definitions[app_id] = definition
            except (KeyError, ValueError) as exc:
                # An in-use app cannot be reconfigured through either UI path;
                # preserve the current local definition until it is stopped.
                self._logger.warning("Could not update local fallback definition for %s: %s", app_id, exc)

    def _async_post(self, path: str, payload: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                self._request_json("POST", path, payload, timeout=20.0)
            except Exception as exc:
                self._logger.warning("Agent control request failed path=%s pid=%s host=%s port=%s error=%r", path, self._last_agent_pid, self.host, self.port, exc)
                if isinstance(exc, AgentUnavailable):
                    kind = self._failure_kind(exc)
                    # A slow operation can time out while the independent
                    # /health endpoint is still working. Let liveness polling
                    # decide whether the Agent itself is degraded.
                    self._logger.warning("Agent operation timed out path=%s classification=%s; awaiting independent health probe", path, kind)
                else:
                    self.error_occurred.emit("", str(exc))
        self._submit_background(f"agent-command:{path}:{json.dumps(payload, sort_keys=True, default=str)}", worker)
