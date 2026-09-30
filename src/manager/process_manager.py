from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import shlex
import sys
import subprocess
import threading
import time
import copy
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

# The Agent is also shipped for Debian minimal installations.  Process events
# are useful to the desktop controller, but must not force a GUI toolkit onto
# a headless production server.
try:
    from PySide6.QtCore import QObject, Signal
except ImportError:  # pragma: no cover - exercised by Debian package smoke test
    class QObject:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs) -> None: pass

    class _HeadlessSignal:
        def connect(self, *_args, **_kwargs) -> None: pass
        def emit(self, *_args, **_kwargs) -> None: pass

    class Signal:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs) -> None: pass
        def __get__(self, _instance, _owner) -> _HeadlessSignal: return _HeadlessSignal()

from .start_command import build_start_command
from .deployments import DockerComposeDeploymentProvider, DeploymentError, inventory_docker_workloads


CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A full psutil process-table walk is comparatively expensive on a busy
# server.  It is needed for startup reconciliation and an explicit refresh,
# but it is not a sensible one-second polling operation for every stopped
# application.  Runner-owned processes are monitored directly between these
# reconciliation checks.
EXTERNAL_PROCESS_RECONCILE_SECONDS = 30.0
PROCESS_METRICS_INTERVAL_SECONDS = 2.0
COMPOSE_MONITOR_INTERVAL_SECONDS = 15.0
COMPOSE_MONITOR_MAX_RETRY_SECONDS = 60.0
LOG_FILE_FLUSH_BYTES = 64 * 1024
LOG_FILE_BUFFER_MAX_BYTES = 512 * 1024


class _WindowsProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_name_inventory() -> list[psutil.Process]:
    """Take one cheap PID/image-name snapshot without opening every process.

    psutil's Windows ``process_iter(['name'])`` can resolve the executable
    path for each PID. Thousands of unrelated helpers make that operation
    slow enough to starve the Qt thread through the GIL. Toolhelp returns the
    image names in one native snapshot; command lines are fetched later only
    for candidate names.
    """
    if os.name != "nt":
        return list(psutil.process_iter(["pid", "name"]))

    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WindowsProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WindowsProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if handle == ctypes.c_void_p(-1).value:
        return list(psutil.process_iter(["pid", "name"]))
    inventory: list[psutil.Process] = []
    try:
        entry = _WindowsProcessEntry()
        entry.dwSize = ctypes.sizeof(_WindowsProcessEntry)
        has_entry = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while has_entry:
            pid = int(entry.th32ProcessID)
            if pid:
                try:
                    process = psutil.Process(pid)
                    process.info = {"pid": pid, "name": str(entry.szExeFile)}
                    inventory.append(process)
                except psutil.Error:
                    pass
            has_entry = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return inventory


@dataclass(slots=True)
class AppConfig:
    app_id: str
    name: str
    runner_path: str
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    startup_input: str = ""
    interactive: bool = False
    visible_console: bool = False
    auto_start: bool = False
    protected: bool = False
    service_group: str = ""
    dependencies: list[str] = field(default_factory=list)
    health_check: dict[str, Any] = field(default_factory=dict)
    persistence: dict[str, Any] = field(default_factory=dict)
    deployments: dict[str, Any] = field(default_factory=dict)
    sync: dict[str, Any] = field(default_factory=dict)
    startup_timeout_seconds: float = 60.0
    app_type: str = "process"
    compose: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppRuntime:
    config: AppConfig
    status: str = "Stopped"
    pid: int | None = None
    cpu_percent: float | None = None
    ram_mb: float | None = None
    started_at: float | None = None
    process: subprocess.Popen[str] | None = None
    ps_process: psutil.Process | None = None
    pending_action: bool = False
    start_generation: int = 0
    start_requested_monotonic: float | None = None
    startup_deadline_monotonic: float | None = None
    startup_command: str = ""
    external_process: bool = False
    last_exit_code: int | None = None
    last_error: str = ""
    monitor_thread: threading.Thread | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    capture_thread: threading.Thread | None = None
    log_cache: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    pending_lines: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    log_file_path: Path | None = None
    log_file_lock: threading.Lock = field(default_factory=threading.Lock)
    display_lock: threading.Lock = field(default_factory=threading.Lock)
    scheduled_action: str = ""
    saw_output: bool = False
    last_output_at: float | None = None
    prompt_pending: bool = False
    stop_requested: bool = False
    last_snapshot: dict[str, Any] | None = None
    deployment: DockerComposeDeploymentProvider | None = None
    deployment_state: dict[str, Any] = field(default_factory=dict)
    last_external_process_scan: float = 0.0
    pending_file_log: list[str] = field(default_factory=list)
    pending_file_log_bytes: int = 0


class ProcessManager(QObject):
    state_changed = Signal(str, object)
    batch_state_changed = Signal(bool, str)
    error_occurred = Signal(str, str)
    registry_changed = Signal(object)

    def __init__(
        self, apps: list[dict[str, Any]], logs_dir: str | Path, *,
        reconcile_existing: bool = True, monitor_external_processes: bool = True,
    ) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._stats_wakeup = threading.Event()
        self._async_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="runner-manager-work")
        self._async_slots = threading.BoundedSemaphore(16)
        self._async_lock = threading.Lock()
        self._async_active: set[str] = set()
        self._logs_dir = Path(logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._batch_busy = False
        self._monitor_external_processes = monitor_external_processes
        self._apps: dict[str, AppRuntime] = {}
        self._ordered_ids: list[str] = []
        self._start_authorizer: Callable[[dict[str, Any]], tuple[bool, str]] | None = None

        # Compose support has been retired. Keep old entries inert until the
        # registry migration removes them; in particular, never probe Docker
        # or operate on containers while upgrading Runner.
        for raw_app in apps:
            if str(raw_app.get("app_type") or "process") == "docker_compose":
                continue
            config = self._build_config(raw_app)
            runtime = AppRuntime(config=config)
            if not reconcile_existing:
                runtime.last_external_process_scan = time.monotonic()
            if config.app_type == "docker_compose":
                runtime.deployment = DockerComposeDeploymentProvider({"id": config.app_id, "cwd": config.cwd, **config.compose})
            runtime.log_file_path = self._logs_dir / f"{self._safe_name(config.name)}.log"
            self._apps[config.app_id] = runtime
            self._ordered_ids.append(config.app_id)

        self._stats_thread = threading.Thread(
            target=self._stats_loop,
            name="runner-overview",
            daemon=True,
        )
        self._stats_thread.start()

    def app_definitions(self) -> list[dict[str, Any]]:
        return [self.snapshot(app_id) for app_id in self._ordered_ids]

    def snapshot(self, app_id: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._apps[app_id]
            return self._snapshot_locked(runtime)

    def adopt_observed_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Adopt only a locally verified unprotected process/Compose snapshot."""
        app_id = str(snapshot.get("id") or "")
        if not app_id or snapshot.get("protected"):
            return False
        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None:
                return False
            config = runtime.config
        status = str(snapshot.get("status") or "Unknown")
        if config.app_type == "docker_compose":
            # Compose state has no trustworthy PID. The local manager's
            # read-only Docker inspect loop establishes current state instead
            # of adopting a possibly stale API snapshot.
            return False
        pid = snapshot.get("pid")
        if status in {"Running", "Already Running", "Waiting Input", "Degraded"} and pid:
            try:
                process = psutil.Process(int(pid))
                if not process.is_running() or not self._process_matches_config(process, config):
                    return False
                started_at = self._safe_create_time(process)
            except (psutil.Error, OSError, TypeError, ValueError):
                return False
            with self._lock:
                runtime = self._apps[app_id]
                runtime.ps_process = process
                runtime.pid = process.pid
                runtime.started_at = started_at
                runtime.external_process = True
                runtime.status = "Already Running"
                runtime.last_external_process_scan = time.monotonic()
                self._emit_snapshot_locked(runtime)
            return True
        # A prior Agent "Stopped" report is historical, not proof of the
        # process state now. External-process reconciliation must verify it.
        return False

    def set_start_authorizer(self, authorizer: Callable[[dict[str, Any]], tuple[bool, str]] | None) -> None:
        """Install the Agent ownership gate used before every process start."""
        with self._lock:
            self._start_authorizer = authorizer

    def get_log_cache_text(self, app_id: str) -> str:
        with self._lock:
            runtime = self._apps[app_id]
            lines = list(runtime.log_cache)
            status = runtime.status
            visible_console = runtime.config.visible_console
            deployment = runtime.deployment
        if deployment is not None:
            service = ""
            try:
                return deployment.logs(service or None)
            except DeploymentError as exc:
                return f"Docker Compose logs unavailable: {exc}\n"
        if lines:
            return "".join(lines)
        if visible_console and status in {"Running", "Waiting Input"}:
            return "Visible Console mode is active. Use that console window for input and output.\n"
        if status == "Running":
            return "Waiting for log output...\n"
        if status == "Already Running":
            return "A matching process is already running outside Runner. Live pipe logs and Send Input are unavailable.\n"
        if status in {"Starting", "Stopping"}:
            return f"{status}...\n"
        if status == "Waiting Input":
            return "Waiting for input. Use Send Input in Runner.\n"
        if status == "Stopped by Runner":
            return "Stopped by Runner.\n"
        return "Idle\n"

    def clear_log_cache(self, app_id: str) -> None:
        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None:
                return
            runtime.log_cache.clear()
            with runtime.display_lock:
                runtime.pending_lines.clear()

    def has_live_log_output(self, app_id: str) -> bool:
        with self._lock:
            runtime = self._apps.get(app_id)
            return bool(runtime and runtime.log_cache)

    def drain_pending_log_lines(self, app_id: str) -> list[str]:
        with self._lock:
            runtime = self._apps[app_id]
            with runtime.display_lock:
                lines = list(runtime.pending_lines)
                runtime.pending_lines.clear()
        return lines

    def start_app(self, app_id: str) -> None:
        self._queue_app_action(app_id, "start", "Starting", self._start_app_worker)

    def start_app_for_agent(self, app_id: str) -> None:
        """Start from an Agent state-machine worker, not the Qt/UI thread.

        Lease acquisition and process launch must be one deterministic critical
        sequence.  The normal GUI API remains asynchronous; this method is
        intentionally reserved for the background Agent orchestration path.
        """
        self._start_app_internal(app_id, keep_pending=True)

    def stop_app(self, app_id: str) -> None:
        self._queue_app_action(app_id, "stop", "Stopping", self._stop_app_worker)

    def restart_app(self, app_id: str) -> None:
        self._queue_app_action(app_id, "restart", "", self._restart_app_worker)

    def force_stop_app(self, app_id: str) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            if self._batch_busy or runtime.scheduled_action == "force-stop":
                return
            runtime.pending_action = True
            runtime.scheduled_action = "force-stop"
            runtime.status = "Stopping"
            self._emit_snapshot_locked(runtime)
        self._run_async(f"force-stop-{app_id}", self._force_stop_app_worker, app_id)

    def send_input(self, app_id: str, text: str, append_newline: bool = True) -> None:
        self._run_async(f"stdin-{app_id}", self._send_input_worker, app_id, text, append_newline)

    def start_all(self) -> None:
        self._run_async("start-all", self._start_all_worker)

    def start_auto_start_apps(self) -> None:
        self._run_async("start-auto-start", self._start_auto_start_worker)

    def stop_all(self) -> None:
        self._run_async("stop-all", self._stop_all_worker)

    def refresh_all(self, on_complete: Callable[[bool], None] | None = None) -> None:
        self._run_async("refresh-overview", self._refresh_once_worker, on_complete)

    def shutdown(self, *, stop_applications: bool = True) -> None:
        self._shutdown_event.set()
        self._stats_wakeup.set()
        if stop_applications:
            for app_id in self._ordered_ids:
                try:
                    self._stop_app_internal(app_id, clear_pending=False, emit=False)
                except Exception:
                    continue
        if self._stats_thread.is_alive():
            self._stats_thread.join(timeout=2.0)
        self._async_executor.shutdown(wait=False, cancel_futures=True)
        self._flush_pending_log_files_sync()

    def export_apps(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._serialize_config(self._apps[app_id].config) for app_id in self._ordered_ids]

    def docker_inventory(self) -> list[dict[str, Any]]:
        """Return a read-only inventory for the Docker management page.

        Discovery is intentionally explicit and not part of the high-frequency
        process metrics loop.  It cannot start, stop, inspect logs, or read
        container environment values.
        """
        return [asdict(item) for item in inventory_docker_workloads()]

    def docker_status(self) -> dict[str, Any]:
        """Return a read-only Docker Engine/workload summary for the UI.

        This is deliberately separate from the normal process refresh loop.  It
        performs no lifecycle operation and never reads container environments.
        """
        provider = DockerComposeDeploymentProvider({"id": "runner-docker-overview", "cwd": str(Path.cwd())})
        availability = provider.availability()
        result = {"engine": asdict(availability), "workloads": []}
        if availability.engine:
            try:
                result["workloads"] = self.docker_inventory()
            except Exception as exc:
                result["error"] = str(exc)
        return result

    def add_app(self, app_data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            config = self._build_config(app_data)
            if config.app_id in self._apps:
                raise ValueError(f"App id '{config.app_id}' already exists")
            self._validate_config_definition(config)
            runtime = AppRuntime(config=config)
            if config.app_type == "docker_compose":
                runtime.deployment = DockerComposeDeploymentProvider({"id": config.app_id, "cwd": config.cwd, **config.compose})
            runtime.log_file_path = self._logs_dir / f"{self._safe_name(config.name)}.log"
            self._apps[config.app_id] = runtime
            self._ordered_ids.append(config.app_id)
            snapshot = self._snapshot_locked(runtime)
        self.registry_changed.emit(self.app_definitions())
        return snapshot

    def update_app(self, app_id: str, app_data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process or runtime.pending_action or runtime.external_process:
                raise ValueError("Stop the app before editing it")
            merged = self._serialize_config(runtime.config)
            merged.update(app_data)
            merged["id"] = app_id
            config = self._build_config(merged)
            self._validate_config_definition(config)
            runtime.config = config
            runtime.deployment = DockerComposeDeploymentProvider({"id": config.app_id, "cwd": config.cwd, **config.compose}) if config.app_type == "docker_compose" else None
            runtime.log_file_path = self._logs_dir / f"{self._safe_name(config.name)}.log"
            snapshot = self._snapshot_locked(runtime)
            self._emit_snapshot_locked(runtime)
        self.registry_changed.emit(self.app_definitions())
        return snapshot

    @staticmethod
    def _validate_config_definition(config: AppConfig) -> None:
        """Validate local paths without executing an app or Docker command."""
        if not config.cwd:
            raise ValueError("Working directory is required")
        cwd = Path(config.cwd).expanduser()
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")
        if config.app_type != "docker_compose":
            if not config.runner_path:
                raise ValueError("Runner file is required")
            if not Path(config.runner_path).expanduser().is_file():
                raise ValueError(f"Runner file does not exist: {config.runner_path}")
            return
        compose = dict(config.compose)
        declared_root = Path(str(compose.get("project_dir") or config.cwd)).expanduser().resolve()
        if declared_root != cwd.resolve():
            raise ValueError("Compose project directory must match the approved working directory")
        compose_file = Path(str(compose.get("compose_file") or "docker-compose.yml"))
        if compose_file.is_absolute():
            raise ValueError("Compose file must be relative to the approved project directory")
        root = cwd.resolve()
        resolved = (root / compose_file).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("Compose file must be inside the approved project directory")
        if not resolved.is_file():
            raise ValueError(f"Compose file does not exist: {resolved}")

    def remove_app(self, app_id: str) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            is_retired_compose = runtime.config.app_type == "docker_compose"
            if runtime.process or runtime.pending_action or (runtime.external_process and not is_retired_compose):
                raise ValueError("Stop the app before deleting it")
            del self._apps[app_id]
            self._ordered_ids = [existing_id for existing_id in self._ordered_ids if existing_id != app_id]
        self.registry_changed.emit(self.app_definitions())

    def _build_config(self, raw_app: dict[str, Any]) -> AppConfig:
        app_type = str(raw_app.get("app_type") or "process")
        if app_type != "process":
            raise ValueError(f"Unsupported application type: {app_type}")
        app_id = str(raw_app.get("id") or raw_app.get("name") or len(self._ordered_ids))
        runner_path = str(raw_app.get("runner_path") or raw_app.get("runner_file") or raw_app.get("command") or "").strip()
        cwd = str(raw_app.get("cwd") or raw_app.get("folder") or "").strip()
        startup_input = str(raw_app.get("startup_input") or "")
        visible_console = bool(raw_app.get("visible_console", False))
        if not cwd and runner_path:
            runner_parent = Path(runner_path).expanduser().resolve().parent if Path(runner_path).suffix else Path(runner_path).parent
            cwd = str(runner_parent) if str(runner_parent) not in {".", ""} else ""
        return AppConfig(
            app_id=app_id,
            name=str(raw_app.get("name") or app_id),
            runner_path=runner_path,
            args=self._normalize_args(raw_app.get("args", raw_app.get("arguments", []))),
            cwd=cwd,
            env={str(key): str(value) for key, value in raw_app.get("env", {}).items()},
            startup_input=startup_input,
            interactive=bool(raw_app.get("interactive", False) or startup_input.strip() or visible_console),
            visible_console=visible_console,
            auto_start=bool(raw_app.get("auto_start", False)),
            protected=bool(raw_app.get("protected", False)),
            service_group=str(raw_app.get("service_group") or raw_app.get("id") or ""),
            dependencies=[str(value) for value in raw_app.get("dependencies", [])],
            health_check=dict(raw_app.get("health_check") or {"type": "process", "timeout_seconds": 5}),
            persistence=dict(raw_app.get("persistence") or {"strategy": "stateless"}),
            deployments=dict(raw_app.get("deployments") or {}),
            sync=dict(raw_app.get("sync") or {"enabled": False, "status": "not_configured"}),
            startup_timeout_seconds=float(raw_app.get("startup_timeout_seconds", 60)),
            app_type="process",
            compose=dict(raw_app.get("compose") or {}),
        )

    @staticmethod
    def _normalize_args(raw_args: Any) -> list[str]:
        if raw_args is None:
            return []
        if isinstance(raw_args, (list, tuple)):
            return [str(arg) for arg in raw_args if str(arg)]
        args_text = str(raw_args).strip()
        if not args_text:
            return []
        return [str(arg) for arg in shlex.split(args_text, posix=os.name != "nt")]

    def _run_async(self, name: str, target: Any, *args: Any) -> bool:
        with self._async_lock:
            if name in self._async_active or not self._async_slots.acquire(blocking=False):
                return False
            self._async_active.add(name)
        def run() -> None:
            try:
                target(*args)
            finally:
                with self._async_lock:
                    self._async_active.discard(name)
                self._async_slots.release()
        try:
            self._async_executor.submit(run)
        except RuntimeError:
            with self._async_lock:
                self._async_active.discard(name)
            self._async_slots.release()
            return False
        return True

    def _begin_start_attempt_locked(self, runtime: AppRuntime) -> None:
        now = time.monotonic()
        # This deadline covers resolving/spawning/verifying a process. Once a
        # PID is verified, normal application warm-up is no longer "Starting".
        # Cap even legacy 60-second settings so a wedged resolver cannot leave
        # the UI in a transitional state indefinitely.
        timeout = float(runtime.config.startup_timeout_seconds or 15.0)
        timeout = max(3.0, min(timeout, 20.0))
        runtime.start_generation += 1
        runtime.start_requested_monotonic = now
        runtime.startup_deadline_monotonic = now + timeout
        runtime.startup_command = ""

    @staticmethod
    def _startup_duration_locked(runtime: AppRuntime) -> float:
        started = runtime.start_requested_monotonic
        return max(0.0, time.monotonic() - started) if started is not None else 0.0

    @staticmethod
    def _clear_start_attempt_locked(runtime: AppRuntime) -> None:
        runtime.start_requested_monotonic = None
        runtime.startup_deadline_monotonic = None
        runtime.startup_command = ""

    def _check_startup_watchdogs(self) -> None:
        """Settle starts using the existing stats thread; no per-start thread."""
        now = time.monotonic()
        with self._lock:
            candidates = [
                (app_id, runtime.process, runtime.ps_process, runtime.start_generation,
                 runtime.start_requested_monotonic, runtime.startup_deadline_monotonic,
                 runtime.config.interactive, runtime.config.visible_console,
                 runtime.config.startup_input, runtime.saw_output, runtime.prompt_pending)
                for app_id, runtime in self._apps.items()
                if runtime.status == "Starting" and runtime.config.app_type != "docker_compose"
            ]

        for (app_id, process, ps_process, generation, requested, deadline,
             interactive, visible_console, startup_input, saw_output, prompt_pending) in candidates:
            elapsed = max(0.0, now - requested) if requested is not None else 0.0
            if process is None:
                if deadline is None or now < deadline:
                    continue
                with self._lock:
                    runtime = self._apps.get(app_id)
                    if runtime is None or runtime.start_generation != generation or runtime.status != "Starting" or runtime.process is not None:
                        continue
                    timeout = max(0.0, deadline - (requested or deadline))
                    runtime.start_generation += 1  # invalidate any delayed resolver before it can Popen
                    runtime.status = "Unknown"
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.last_external_process_scan = 0.0
                    runtime.last_error = f"Runner could not verify a process/PID within {timeout:.0f}s; the start result is unknown. Check the launch log before retrying."
                    self._append_log_line_locked(runtime, f"[Runner] Startup watchdog expired after {elapsed:.2f}s without a verified PID: {runtime.last_error}\n")
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)
                continue

            exit_code = process.poll()
            if exit_code is not None:
                self._finalize_process(app_id, process, exit_code, expected_stop=False, clear_pending=True)
                continue

            verified = False
            try:
                verified = bool(psutil.pid_exists(process.pid) and (ps_process or psutil.Process(process.pid)).is_running())
            except psutil.NoSuchProcess:
                verified = False
            except psutil.Error:
                # Popen.poll() just verified this child is alive. AccessDenied
                # from a transient system process query is not evidence that
                # Runner lost the child.
                verified = True

            # Allow a short settle window so immediate exits are caught by
            # Popen/monitor, while keeping normal starts responsive.
            if verified and (elapsed >= 0.9 or saw_output or visible_console):
                with self._lock:
                    runtime = self._apps.get(app_id)
                    if runtime is None or runtime.start_generation != generation or runtime.process is not process or runtime.status != "Starting":
                        continue
                    runtime.status = (
                        "Waiting Input" if interactive and (prompt_pending or (not saw_output and not startup_input.strip()))
                        else "Running"
                    )
                    runtime.pending_action = False
                    runtime.last_error = ""
                    duration = self._startup_duration_locked(runtime)
                    self._append_log_line_locked(runtime, f"[Runner] Startup verified state={runtime.status} PID={process.pid} duration={duration:.2f}s exit_code=N/A\n")
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)
                continue

            if deadline is not None and now >= deadline:
                with self._lock:
                    runtime = self._apps.get(app_id)
                    if runtime is None or runtime.start_generation != generation or runtime.process is not process or runtime.status != "Starting":
                        continue
                    runtime.status = "Unknown"
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.last_error = f"PID {process.pid} exists but Runner could not verify it is alive; Stop remains available."
                    self._append_log_line_locked(runtime, f"[Runner] Startup verification expired after {elapsed:.2f}s PID={process.pid}: {runtime.last_error}\n")
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)

    def _queue_app_action(self, app_id: str, action: str, status: str, worker: Any) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action or runtime.scheduled_action or self._batch_busy:
                return
            if action == "start" and (
                runtime.status == "Starting"
                or runtime.external_process
                or (runtime.process is not None and runtime.process.poll() is None)
                or (runtime.status == "Unknown" and runtime.pid is not None)
            ):
                # After Popen succeeds pending_action is intentionally cleared
                # so Stop is available. Keep Start deduplicated until the
                # watchdog settles the same PID into a terminal/runtime state.
                return
            runtime.scheduled_action = action
            runtime.pending_action = True
            if action == "start":
                self._begin_start_attempt_locked(runtime)
            if action == "restart":
                runtime.status = "Stopping" if runtime.process or runtime.external_process else "Starting"
            elif status:
                runtime.status = status
            self._emit_snapshot_locked(runtime)
        if not self._run_async(f"{action}-{app_id}", worker, app_id):
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.status = "Running" if runtime.process or runtime.external_process else "Stopped"
                runtime.last_error = "Runner is busy; retry this action shortly."
                if action == "start":
                    runtime.status = "Unknown" if runtime.pid is not None else "Crashed"
                    self._append_log_line_locked(runtime, f"[Runner] Start failed: {runtime.last_error}\n")
                self._clear_start_attempt_locked(runtime)
                self._emit_snapshot_locked(runtime)

    def _start_app_worker(self, app_id: str) -> None:
        try:
            self._start_app_internal(app_id, keep_pending=True)
        except Exception as exc:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None:
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    if runtime.process is None and not runtime.external_process:
                        runtime.status = "Crashed"
                        runtime.last_error = str(exc)
                        duration = self._startup_duration_locked(runtime)
                        self._append_log_line_locked(runtime, f"[Runner] Start failed after {duration:.2f}s: {exc}\n")
                    elif runtime.status == "Starting":
                        # Popen may have succeeded before monitor/reader setup
                        # failed. Preserve its PID so Stop remains available,
                        # but never leave the UI claiming an unverified start.
                        runtime.status = "Unknown"
                        runtime.last_error = f"Process PID {runtime.pid} exists, but Runner could not complete startup monitoring: {exc}"
                        self._append_log_line_locked(runtime, f"[Runner] Startup monitoring failed PID={runtime.pid}: {exc}\n")
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)
            self._emit_error(app_id, f"Start failed: {exc}")
        finally:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None and runtime.scheduled_action == "start":
                    runtime.scheduled_action = ""

    def _stop_app_worker(self, app_id: str) -> None:
        try:
            self._stop_app_internal(app_id, clear_pending=True)
        except Exception as exc:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None:
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.last_error = f"Stop failed: {exc}"
                    self._append_log_line_locked(runtime, f"[Runner] Stop failed: {exc}\n")
                    if runtime.process is None and not runtime.external_process:
                        runtime.status = "Unknown"
                    self._emit_snapshot_locked(runtime)
            self._emit_error(app_id, f"Stop failed: {exc}")
        finally:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None and runtime.scheduled_action == "stop":
                    runtime.scheduled_action = ""

    def _force_stop_app_worker(self, app_id: str) -> None:
        try:
            self._force_stop_app_internal(app_id, clear_pending=True)
        except Exception as exc:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None:
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    if runtime.process is None and not runtime.external_process:
                        runtime.status = "Stopped"
                    self._emit_snapshot_locked(runtime)
            self._emit_error(app_id, f"Force stop failed: {exc}")
        finally:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None and runtime.scheduled_action == "force-stop":
                    runtime.scheduled_action = ""

    def _send_input_worker(self, app_id: str, text: str, append_newline: bool) -> None:
        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None:
                return
            process = runtime.process
            stdin_pipe = process.stdin if process is not None else None
            if runtime.config.visible_console:
                self._emit_error(app_id, "Input is disabled while Visible Console mode is active.")
                return
            if runtime.external_process:
                self._emit_error(app_id, "Input is disabled because this process was started outside Runner.")
                return
            if process is None or process.poll() is not None or stdin_pipe is None:
                self._emit_error(app_id, "Input failed: app is not accepting stdin.")
                return
            payload = text
            if append_newline and not payload.endswith("\n"):
                payload += "\n"

        try:
            stdin_pipe.write(payload)
            stdin_pipe.flush()
        except Exception as exc:
            self._emit_error(app_id, f"Input failed: {exc}")
            return

        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None or runtime.process is not process:
                return
            if runtime.config.interactive:
                runtime.status = "Running"
                runtime.prompt_pending = False
            self._append_log_line_locked(runtime, "[Runner] Input sent\n")
            self._emit_snapshot_locked(runtime)

    def _restart_app_worker(self, app_id: str) -> None:
        if self._shutdown_event.is_set():
            return
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.scheduled_action not in {"", "restart"} or self._batch_busy:
                if runtime.scheduled_action == "restart":
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    self._emit_snapshot_locked(runtime)
                return
            runtime.status = "Stopping" if runtime.process else "Starting"
            self._emit_snapshot_locked(runtime)

        try:
            self._stop_app_internal(app_id, clear_pending=False)
            if not self._shutdown_event.is_set():
                self._start_app_internal(app_id, keep_pending=True)
        except Exception as exc:
            self._emit_error(app_id, f"Restart failed: {exc}")
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.last_error = f"Restart failed: {exc}"
                self._append_log_line_locked(runtime, f"[Runner] Restart failed: {exc}\n")
                if not runtime.process:
                    runtime.status = "Crashed"
                self._emit_snapshot_locked(runtime)
        finally:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is not None and runtime.scheduled_action == "restart":
                    runtime.scheduled_action = ""

    def _start_all_worker(self) -> None:
        if not self._begin_batch("Starting all apps..."):
            return
        try:
            for app_id in self._ordered_ids:
                if self._shutdown_event.is_set():
                    break
                self._start_one_from_batch(app_id)
        finally:
            self._end_batch()

    def _start_one_from_batch(self, app_id: str) -> None:
        """Queue one batch member through the same guarded start lifecycle."""
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action or runtime.scheduled_action:
                return
            if runtime.status in {"Starting", "Stopping"}:
                return
            if runtime.process is not None and runtime.process.poll() is None:
                return
            if runtime.external_process:
                return
            runtime.pending_action = True
            runtime.scheduled_action = "start"
            self._begin_start_attempt_locked(runtime)
            runtime.status = "Starting"
            runtime.last_error = ""
            self._emit_snapshot_locked(runtime)
        # Catches launch/monitor setup failures per app, allowing later batch
        # entries to proceed and ensuring the failed entry leaves Starting.
        self._start_app_worker(app_id)

    def _start_auto_start_worker(self) -> None:
        auto_ids = [
            app_id
            for app_id in self._ordered_ids
            if self._apps[app_id].config.auto_start
        ]
        if not auto_ids:
            return
        if not self._begin_batch("Starting auto-start apps..."):
            return
        try:
            for app_id in auto_ids:
                if self._shutdown_event.is_set():
                    break
                self._start_one_from_batch(app_id)
        finally:
            self._end_batch()

    def _stop_all_worker(self) -> None:
        if not self._begin_batch("Stopping all apps..."):
            return
        try:
            for app_id in self._ordered_ids:
                if self._shutdown_event.is_set():
                    break
                # Stop is isolated per app: a process-tree/permission failure
                # is reported on that app and must not strand later entries.
                self._stop_app_worker(app_id)
        finally:
            self._end_batch()

    def _refresh_once_worker(self, on_complete: Callable[[bool], None] | None = None) -> None:
        succeeded = False
        try:
            self._refresh_stats_once(force_emit=True)
            succeeded = True
        except Exception as exc:
            self.error_occurred.emit("", f"Refresh failed: {exc}")
        finally:
            if on_complete:
                on_complete(succeeded)

    def _refresh_compose_deployments(self) -> bool:
        with self._lock:
            entries = [(app_id, runtime.deployment) for app_id, runtime in self._apps.items() if runtime.deployment]
        succeeded = True
        for app_id, provider in entries:
            try:
                state = provider.inspect()
                health = self._compose_is_healthy(state)
                warning_reader = getattr(provider, "warning_summary", None)
                warnings = warning_reader() if warning_reader else []
                with self._lock:
                    runtime = self._apps[app_id]
                    runtime.deployment_state = state
                    runtime.external_process = state["running"]
                    runtime.status = "Running" if state["running"] and health else ("Degraded" if state["running"] else "Stopped")
                    # A previous failed start must not leave the deployment
                    # permanently red in the GUI after Docker later reports a
                    # healthy service group.
                    runtime.last_error = "; ".join(warnings)
                    self._emit_snapshot_locked(runtime)
            except Exception as exc:
                succeeded = False
                with self._lock:
                    runtime = self._apps[app_id]
                    runtime.status = "Degraded"
                    runtime.last_error = str(exc)
                    self._emit_snapshot_locked(runtime)
        return succeeded

    @staticmethod
    def _compose_monitor_retry_seconds(consecutive_failures: int) -> float:
        if consecutive_failures <= 0:
            return COMPOSE_MONITOR_INTERVAL_SECONDS
        return min(COMPOSE_MONITOR_MAX_RETRY_SECONDS, COMPOSE_MONITOR_INTERVAL_SECONDS * (2 ** min(consecutive_failures, 3)))

    @staticmethod
    def _compose_is_healthy(state: dict[str, Any]) -> bool:
        services = list(dict(state.get("services", {})).values())
        return bool(services) and all(
            item.get("state") == "running" and item.get("health") in {"healthy", "unknown"}
            for item in services
        )

    def _begin_batch(self, message: str) -> bool:
        with self._lock:
            if self._batch_busy:
                return False
            self._batch_busy = True
        self.batch_state_changed.emit(True, message)
        return True

    def _end_batch(self) -> None:
        with self._lock:
            self._batch_busy = False
        self.batch_state_changed.emit(False, "")

    def _start_app_internal(self, app_id: str, keep_pending: bool = False) -> None:
        if self._shutdown_event.is_set():
            return

        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action and not keep_pending:
                return
            if runtime.start_requested_monotonic is None:
                self._begin_start_attempt_locked(runtime)
            generation = runtime.start_generation
            probe = copy.copy(runtime)
            config = runtime.config
        # psutil can call into OS process APIs. Never hold the shared snapshot
        # lock while probing a PID or reading executable/cmdline metadata.
        if probe.process is not None and probe.process.poll() is None:
            with self._lock:
                runtime = self._apps[app_id]
                if runtime.process is probe.process:
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.status = "Running"
                    runtime.last_error = ""
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)
            return
        existing_process = self._resolve_live_process(probe)
        existing_started_at = self._safe_create_time(existing_process) if existing_process is not None else None
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process is not probe.process or runtime.pid != probe.pid:
                return
            if existing_process is not None:
                runtime.process = None
                runtime.ps_process = existing_process
                runtime.pid = existing_process.pid
                runtime.started_at = existing_started_at
                runtime.cpu_percent = None
                runtime.ram_mb = None
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = True
                runtime.status = "Already Running"
                runtime.last_error = f"{runtime.config.name} is already running."
                self._clear_start_attempt_locked(runtime)
                self._append_log_line_locked(runtime, f"[Runner] Start blocked: PID {existing_process.pid} is already running\n")
                self._emit_snapshot_locked(runtime)
                if not self._batch_busy:
                    self._emit_error(app_id, f"{runtime.config.name} is already running. Stop it first or use Restart.")
                return
            runtime.pending_action = True
            runtime.external_process = False
            runtime.status = "Starting"
            runtime.last_error = ""
            runtime.stop_requested = False
            runtime.last_exit_code = None
            runtime.cpu_percent = None
            runtime.ram_mb = None
            runtime.saw_output = False
            runtime.last_output_at = None
            runtime.prompt_pending = False
            runtime.log_cache.clear()
            with runtime.display_lock:
                runtime.pending_lines.clear()
            self._emit_snapshot_locked(runtime)
            config = runtime.config

        if self._start_authorizer is not None:
            allowed, reason = self._start_authorizer(self._serialize_config(config))
            if not allowed:
                with self._lock:
                    runtime = self._apps[app_id]
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.status = "Stopped"
                    runtime.last_error = reason
                    self._append_log_line_locked(runtime, f"[Runner] Start denied: {reason}\n")
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)
                self._emit_error(app_id, reason)
                return

        if config.app_type == "docker_compose":
            self._start_compose_internal(app_id, keep_pending=keep_pending)
            return

        # Cluster-protected starts occur only after the Agent has reconciled
        # the local process table and acquired a lease.  Do not perform a
        # second, unbounded whole-machine scan after acquiring that lease:
        # on a busy server it can consume the fencing window itself.  The
        # runtime/PID reconciliation above still detects an adopted process;
        # standalone applications retain the legacy duplicate scan.
        existing_process = None if config.protected else self._find_existing_process_with_retry(config)
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.start_generation != generation or runtime.status != "Starting":
                return
        if existing_process is not None:
            existing_started_at = self._safe_create_time(existing_process)
            with self._lock:
                runtime = self._apps[app_id]
                runtime.process = None
                runtime.ps_process = existing_process
                runtime.pid = existing_process.pid
                runtime.started_at = existing_started_at
                runtime.cpu_percent = None
                runtime.ram_mb = None
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = True
                runtime.status = "Already Running"
                runtime.last_error = f"{runtime.config.name} is already running."
                self._clear_start_attempt_locked(runtime)
                self._append_log_line_locked(runtime, f"[Runner] Start blocked: PID {existing_process.pid} is already running\n")
                self._emit_snapshot_locked(runtime)
            if not self._batch_busy:
                self._emit_error(
                    app_id,
                    f"{config.name} is already running. Stop it first or use Restart.",
                )
            return

        if not config.runner_path:
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.status = "Crashed"
                runtime.last_error = "Missing runner file in apps.json"
                duration = self._startup_duration_locked(runtime)
                self._append_log_line_locked(runtime, f"[Runner] Start failed after {duration:.2f}s: {runtime.last_error}\n")
                self._clear_start_attempt_locked(runtime)
                self._emit_snapshot_locked(runtime)
            return

        try:
            process: subprocess.Popen[str] | None = None
            env = os.environ.copy()
            env.update(config.env)
            command, cwd, stdin_text = build_start_command(
                {
                    "folder": config.cwd,
                    "runner_file": config.runner_path,
                    "arguments": config.args,
                    "startup_input": config.startup_input,
                }
            )
            if config.protected:
                safe_group = self._safe_name(config.service_group)
                guard_file = self._logs_dir.parent / "guards" / f"{safe_group}.deadline"
                if getattr(sys, "frozen", False):
                    command = [sys.executable, "--process-guard", "--guard-file", str(guard_file), "--", *command]
                else:
                    guard_script = Path(__file__).resolve().parents[1] / "cluster" / "process_guard.py"
                    command = [sys.executable, str(guard_script), "--guard-file", str(guard_file), "--", *command]
            display_command = subprocess.list2cmdline([str(part) for part in command])
            with self._lock:
                runtime = self._apps[app_id]
                if runtime.start_generation != generation or runtime.status != "Starting":
                    return
                runtime.startup_command = display_command
                self._append_log_line_locked(runtime, f"[Runner] Launch command: {display_command} | cwd={cwd}\n")
                deadline = runtime.startup_deadline_monotonic
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Startup deadline expired before process creation ({config.startup_timeout_seconds:.0f}s)")
            creationflags = 0
            startupinfo = None
            stdin_target: Any = subprocess.PIPE
            stdout_target: Any = subprocess.PIPE
            stderr_target: Any = subprocess.PIPE
            batch_capture_path: Path | None = None
            if os.name == "nt":
                creationflags = CREATE_NEW_PROCESS_GROUP
                # Keep non-interactive apps hidden. Interactive apps can opt into a real console
                # when they require native console input instead of Runner's stdin pipe.
                if config.visible_console:
                    creationflags |= CREATE_NEW_CONSOLE
                    stdin_target = None
                    stdout_target = None
                    stderr_target = None
                elif Path(config.runner_path).suffix.lower() in {".bat", ".cmd"} and not config.interactive:
                    # Windows commands such as `timeout` abort when stdin is redirected. Give
                    # Batch a real (hidden) console and tail a session file for captured output.
                    batch_capture_path = self._logs_dir / f".{self._safe_name(config.name)}-batch-session.log"
                    batch_capture_path.write_text("", encoding="utf-8")
                    command.extend([">>", str(batch_capture_path), "2>&1"])
                    creationflags |= CREATE_NEW_CONSOLE
                    stdin_target = None
                    stdout_target = None
                    stderr_target = None
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
                else:
                    creationflags |= CREATE_NO_WINDOW
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
            process = subprocess.Popen(
                command,
                cwd=cwd or None,
                env=env,
                stdin=stdin_target,
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            try:
                ps_process = psutil.Process(process.pid)
                ps_process.cpu_percent(None)
            except psutil.Error:
                # Popen.poll remains authoritative if metadata is temporarily
                # unavailable; the stats-thread watchdog will report Unknown
                # rather than silently leaving Starting forever.
                ps_process = None
        except Exception as exc:
            with self._lock:
                runtime = self._apps[app_id]
                if runtime.start_generation != generation:
                    # A watchdog/Stop/new attempt superseded this resolver.
                    # Never let its late exception overwrite the newer state.
                    if process is not None:
                        self._append_log_line_locked(runtime, f"[Runner] Superseded launch failed PID={process.pid}: {exc}\n")
                    return
                duration = self._startup_duration_locked(runtime)
                runtime.pending_action = False
                runtime.scheduled_action = ""
                if process is None:
                    runtime.process = None
                    runtime.ps_process = None
                    runtime.pid = None
                    runtime.status = "Crashed"
                else:
                    runtime.process = process
                    runtime.pid = process.pid
                    runtime.status = "Unknown"
                runtime.last_error = str(exc)
                runtime.saw_output = False
                self._append_log_line_locked(runtime, f"[Runner] Start failed after {duration:.2f}s command={runtime.startup_command or config.runner_path!r}: {exc}\n")
                self._clear_start_attempt_locked(runtime)
                self._emit_snapshot_locked(runtime)
            return

        with self._lock:
            runtime = self._apps[app_id]
            if runtime.start_generation != generation or runtime.status != "Starting":
                cancelled = True
            else:
                cancelled = False
            if cancelled:
                pass
            else:
                runtime.process = process
                runtime.ps_process = ps_process
                runtime.pid = process.pid
                runtime.started_at = time.time()
                runtime.status = "Starting"
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = False
                runtime.last_error = ""
                runtime.saw_output = False
                runtime.last_output_at = None
                runtime.prompt_pending = False
                duration = self._startup_duration_locked(runtime)
                self._append_log_line_locked(runtime, f"[Runner] Spawn succeeded PID={process.pid} after {duration:.2f}s command={runtime.startup_command}\n")
                if runtime.config.interactive:
                    if runtime.config.visible_console:
                        self._append_log_line_locked(runtime, "[Runner] Visible Console mode is active for this app\n")
                    else:
                        self._append_log_line_locked(runtime, "[Runner] Interactive mode is active. Send Input will enable when a prompt is detected\n")
                self._emit_snapshot_locked(runtime)

                runtime.monitor_thread = threading.Thread(
                    target=self._monitor_process,
                    args=(app_id, process),
                    name=f"monitor-{app_id}",
                    daemon=True,
                )
                runtime.stdout_thread = threading.Thread(
                    target=self._read_stream,
                    args=(app_id, process, process.stdout, ""),
                    name=f"stdout-{app_id}",
                    daemon=True,
                )
                runtime.stderr_thread = threading.Thread(
                    target=self._read_stream,
                    args=(app_id, process, process.stderr, "[stderr] "),
                    name=f"stderr-{app_id}",
                    daemon=True,
                )
                runtime.capture_thread = (
                    threading.Thread(
                        target=self._read_capture_file,
                        args=(app_id, process, batch_capture_path),
                        name=f"batch-log-{app_id}",
                        daemon=True,
                    )
                    if batch_capture_path is not None
                    else None
                )
                for monitor in (runtime.stdout_thread, runtime.stderr_thread, runtime.capture_thread, runtime.monitor_thread):
                    if monitor is None:
                        continue
                    try:
                        monitor.start()
                    except Exception as exc:
                        self._append_log_line_locked(runtime, f"[Runner] Startup monitor could not start ({monitor.name}): {exc}; watchdog will verify PID {process.pid}\n")

        if cancelled:
            # The bounded startup watchdog invalidated this attempt while
            # Popen was in progress. Only terminate the child this exact
            # attempt just created; never touch a previously managed PID.
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            return

        self._stats_wakeup.set()
        self._send_startup_input(app_id, process, stdin_text)

    def _start_compose_internal(self, app_id: str, keep_pending: bool = False) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            provider = runtime.deployment
        if provider is None:
            raise RuntimeError("Docker Compose provider is not configured")
        try:
            state = provider.start()
            healthy = self._compose_is_healthy(state)
            warning_reader = getattr(provider, "warning_summary", None)
            warnings = warning_reader() if warning_reader else []
            with self._lock:
                runtime = self._apps[app_id]
                runtime.deployment_state = state
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = True
                runtime.started_at = time.time()
                runtime.status = "Running" if healthy else "Degraded"
                self._clear_start_attempt_locked(runtime)
                runtime.last_error = "; ".join(warnings) if warnings else ("One or more Compose services are not healthy" if not healthy else "")
                self._append_log_line_locked(runtime, "[Runner] Docker Compose deployment started\n")
                self._emit_snapshot_locked(runtime)
        except Exception as exc:
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.status = "Crashed"
                runtime.last_error = str(exc)
                self._clear_start_attempt_locked(runtime)
                self._append_log_line_locked(runtime, f"[Runner] Docker Compose start failed: {exc}\n")
                self._emit_snapshot_locked(runtime)
            self._emit_error(app_id, str(exc))

    def _stop_compose_internal(self, app_id: str, clear_pending: bool = True, emit: bool = True) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            provider = runtime.deployment
            # A configured standby has no containers to stop.  In particular,
            # adding/importing a Compose app must never issue `compose stop`
            # merely because a controller/process-manager is closing.
            if not runtime.external_process and runtime.status not in {"Running", "Starting", "Stopping", "Degraded"}:
                runtime.pending_action = False if clear_pending else runtime.pending_action
                if clear_pending:
                    runtime.scheduled_action = ""
                runtime.status = "Stopped"
                if emit:
                    self._emit_snapshot_locked(runtime)
                return
            runtime.status = "Stopping"
            if emit: self._emit_snapshot_locked(runtime)
        try:
            state = provider.stop() if provider else {"services": {}}
            with self._lock:
                runtime = self._apps[app_id]
                runtime.deployment_state = state
                runtime.pending_action = False if clear_pending else runtime.pending_action
                if clear_pending: runtime.scheduled_action = ""
                runtime.external_process = False
                runtime.started_at = None
                runtime.status = "Stopped"
                runtime.last_error = ""
                self._append_log_line_locked(runtime, "[Runner] Docker Compose deployment stopped\n")
                if emit: self._emit_snapshot_locked(runtime)
        except Exception as exc:
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.status = "Degraded"
                runtime.last_error = str(exc)
                if emit: self._emit_snapshot_locked(runtime)
            self._emit_error(app_id, str(exc))

    def _stop_app_internal(self, app_id: str, clear_pending: bool = True, emit: bool = True) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            compose_app = runtime.config.app_type == "docker_compose"
        if compose_app:
            self._stop_compose_internal(app_id, clear_pending=clear_pending, emit=emit)
            return
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action and clear_pending and runtime.scheduled_action != "stop":
                if runtime.scheduled_action == "start" and runtime.process is None:
                    # Cancel a start which has not attached a child PID yet.
                    # The generation check after discovery/Popen prevents a
                    # late worker from publishing or retaining that launch.
                    runtime.start_generation += 1
                    runtime.pending_action = False
                    runtime.scheduled_action = ""
                    runtime.status = "Stopped"
                    runtime.last_error = "Start cancelled by Stop request."
                    self._clear_start_attempt_locked(runtime)
                    self._append_log_line_locked(runtime, "[Runner] Start cancelled before PID attachment\n")
                    if emit:
                        self._emit_snapshot_locked(runtime)
                    return
                return
            process = runtime.process
            external_pid = runtime.pid if runtime.external_process else None
            if external_pid is not None and (process is None or process.poll() is not None):
                if clear_pending:
                    runtime.pending_action = True
                runtime.status = "Stopping"
                if emit:
                    self._emit_snapshot_locked(runtime)
            elif process is None or process.poll() is not None:
                runtime.process = None
                runtime.ps_process = None
                runtime.pid = None
                runtime.cpu_percent = None
                runtime.ram_mb = None
                runtime.started_at = None
                runtime.pending_action = False if clear_pending else runtime.pending_action
                if clear_pending:
                    runtime.scheduled_action = ""
                runtime.start_generation += 1
                self._clear_start_attempt_locked(runtime)
                runtime.external_process = False
                runtime.status = "Stopped"
                if emit:
                    self._emit_snapshot_locked(runtime)
                return
            else:
                if clear_pending:
                    runtime.pending_action = True
                    runtime.scheduled_action = "stop"
                runtime.stop_requested = True
                runtime.status = "Stopping"
                if emit:
                    self._emit_snapshot_locked(runtime)

        if external_pid is not None and (process is None or process.poll() is not None):
            self._stop_external_process(app_id, external_pid, clear_pending=clear_pending)
            return

        exit_code: int | None = None
        try:
            self._terminate_process_tree(process.pid)
            exit_code = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=2.0)
        except psutil.Error:
            try:
                process.kill()
                exit_code = process.wait(timeout=2.0)
            except Exception:
                exit_code = process.poll()
        except Exception as exc:
            self._emit_error(app_id, f"Stop failed: {exc}")
            exit_code = process.poll()

        self._finalize_process(app_id, process, exit_code, expected_stop=True, clear_pending=clear_pending)

    def _force_stop_app_internal(self, app_id: str, clear_pending: bool = True, emit: bool = True) -> None:
        if self._apps[app_id].config.app_type == "docker_compose":
            self._stop_compose_internal(app_id, clear_pending=clear_pending, emit=emit)
            return
        with self._lock:
            runtime = self._apps[app_id]
            process = runtime.process
            external_pid = runtime.pid if runtime.external_process else None
            if process is None and external_pid is None:
                runtime.pending_action = False if clear_pending else runtime.pending_action
                if clear_pending:
                    runtime.scheduled_action = ""
                runtime.status = "Stopped"
                if emit:
                    self._emit_snapshot_locked(runtime)
                return
            if clear_pending:
                runtime.pending_action = True
                runtime.scheduled_action = "force-stop"
            runtime.stop_requested = True
            runtime.status = "Stopping"
            if emit:
                self._emit_snapshot_locked(runtime)

        if external_pid is not None and (process is None or process.poll() is not None):
            self._stop_external_process(app_id, external_pid, clear_pending=clear_pending, forced=True)
            return

        exit_code: int | None = None
        try:
            self._kill_process_tree(process.pid)
            exit_code = process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=2.0)
        except psutil.Error:
            try:
                process.kill()
                exit_code = process.wait(timeout=2.0)
            except Exception:
                exit_code = process.poll()
        except Exception as exc:
            self._emit_error(app_id, f"Force stop failed: {exc}")
            exit_code = process.poll()

        self._finalize_process(
            app_id,
            process,
            exit_code,
            expected_stop=True,
            clear_pending=clear_pending,
            stop_message="[Runner] Force-stopped by Runner\n",
        )

    def _terminate_process_tree(self, pid: int) -> None:
        try:
            root = psutil.Process(pid)
        except psutil.Error:
            return

        children = root.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(children, timeout=2.0)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                continue
        try:
            root.terminate()
        except psutil.Error:
            return

    def _kill_process_tree(self, pid: int) -> None:
        try:
            root = psutil.Process(pid)
        except psutil.Error:
            return

        children = root.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(children, timeout=1.5)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                continue
        try:
            root.kill()
        except psutil.Error:
            return

    def _monitor_process(self, app_id: str, process: subprocess.Popen[str]) -> None:
        try:
            exit_code = process.wait()
        except Exception as exc:
            self._emit_error(app_id, f"Monitor failed: {exc}")
            exit_code = process.poll()
        with self._lock:
            runtime = self._apps.get(app_id)
            capture_thread = runtime.capture_thread if runtime and runtime.process is process else None
            expected_stop = bool(runtime and runtime.process is process and runtime.stop_requested)
        if capture_thread is not None:
            capture_thread.join(timeout=1.0)
        self._finalize_process(app_id, process, exit_code, expected_stop=expected_stop, clear_pending=True)

    def _read_capture_file(self, app_id: str, process: subprocess.Popen[str], capture_path: Path) -> None:
        try:
            with capture_path.open("r", encoding="utf-8", errors="replace") as stream:
                while not self._shutdown_event.is_set():
                    line = stream.readline()
                    if line:
                        with self._lock:
                            runtime = self._apps.get(app_id)
                            if runtime is None or runtime.process is not process:
                                break
                            self._append_log_line_locked(runtime, line)
                            runtime.last_output_at = time.time()
                            runtime.saw_output = True
                            if runtime.status == "Starting":
                                runtime.status = "Running"
                                self._clear_start_attempt_locked(runtime)
                                self._emit_snapshot_locked(runtime)
                        continue
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
        except OSError as exc:
            self._emit_error(app_id, f"Batch log reader failed: {exc}")

    def _finalize_process(
        self,
        app_id: str,
        process: subprocess.Popen[str],
        exit_code: int | None,
        expected_stop: bool,
        clear_pending: bool,
        stop_message: str | None = None,
    ) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except (OSError, ValueError):
                pass
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process is not process:
                return
            duration = max(0.0, time.time() - runtime.started_at) if runtime.started_at is not None else 0.0
            runtime.process = None
            runtime.ps_process = None
            runtime.pid = None
            runtime.cpu_percent = None
            runtime.ram_mb = None
            runtime.started_at = None
            runtime.last_exit_code = exit_code
            runtime.saw_output = False
            runtime.last_output_at = None
            runtime.prompt_pending = False
            if clear_pending:
                runtime.pending_action = False
                runtime.scheduled_action = ""
            self._clear_start_attempt_locked(runtime)
            runtime.external_process = False
            runtime.status = "Stopped by Runner" if expected_stop else ("Stopped" if exit_code in (0, None) else "Crashed")
            runtime.stop_requested = False
            if expected_stop or exit_code in (0, None):
                runtime.last_error = ""
            else:
                runtime.last_error = f"Process exited with code {exit_code if exit_code is not None else 'unknown'}"
            if expected_stop:
                self._append_log_line_locked(runtime, stop_message or "[Runner] Stopped by Runner\n")
            elif exit_code in (0, None):
                self._append_log_line_locked(runtime, f"[Runner] Process exited PID={process.pid} duration={duration:.2f}s exit_code={exit_code}\n")
            else:
                self._append_log_line_locked(
                    runtime,
                    f"[Runner] Process exited PID={process.pid} duration={duration:.2f}s exit_code={exit_code if exit_code is not None else 'unknown'}\n",
                )
            self._emit_snapshot_locked(runtime)

    def _read_stream(
        self,
        app_id: str,
        process: subprocess.Popen[str],
        stream: Any,
        prefix: str,
    ) -> None:
        if stream is None:
            return
        try:
            while not self._shutdown_event.is_set():
                line = stream.readline()
                if line:
                    with self._lock:
                        runtime = self._apps[app_id]
                        if runtime.process is not process:
                            break
                        self._append_log_line_locked(runtime, f"{prefix}{line}")
                        previous_status = runtime.status
                        runtime.last_output_at = time.time()
                        prompt_candidate = (
                            runtime.config.interactive
                            and not runtime.config.visible_console
                            and self._line_requests_input(line)
                        )
                        runtime.prompt_pending = bool(prompt_candidate)
                        if runtime.status in {"Starting", "Waiting Input", "Running"}:
                            runtime.status = "Waiting Input" if prompt_candidate else "Running"
                            self._clear_start_attempt_locked(runtime)
                        runtime.saw_output = True
                        if prompt_candidate:
                            baseline_output_at = runtime.last_output_at
                        else:
                            baseline_output_at = None
                        if runtime.status != previous_status:
                            self._emit_snapshot_locked(runtime)
                    if baseline_output_at is not None:
                        self._schedule_waiting_input_settle(app_id, process, baseline_output_at)
                    continue
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        except Exception as exc:
            self._emit_error(app_id, f"Log reader failed: {exc}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _line_requests_input(line: str) -> bool:
        text = line.strip().lower()
        if not text:
            return False
        if text.startswith("[runner]") or text.startswith("[stderr]"):
            return False
        if "ctrl+c" in text or "to quit" in text:
            return False
        menu_markers = (
            "choose",
            "select",
            "enter",
            "your choice",
            "menu choice",
            "pick",
            "option",
            "input",
            "press",
            "type",
            "choice",
        )
        if any(marker in text for marker in menu_markers):
            return True
        stripped = text.lstrip()
        if stripped and stripped[0].isdigit() and any(ch in stripped for ch in (".", ")", ":")):
            return True
        return False

    def _append_log_line_locked(self, runtime: AppRuntime, line: str) -> None:
        runtime.log_cache.append(line)
        with runtime.display_lock:
            runtime.pending_lines.append(line)
        if runtime.log_file_path:
            # Output readers can receive thousands of short lines. Keep the
            # UI cache bounded and write durable logs in batches rather than
            # opening a file for each individual line.
            encoded_size = len(line.encode("utf-8", errors="replace"))
            if encoded_size <= LOG_FILE_BUFFER_MAX_BYTES:
                while runtime.pending_file_log and runtime.pending_file_log_bytes + encoded_size > LOG_FILE_BUFFER_MAX_BYTES:
                    discarded = runtime.pending_file_log.pop(0)
                    runtime.pending_file_log_bytes -= len(discarded.encode("utf-8", errors="replace"))
                runtime.pending_file_log.append(line)
                runtime.pending_file_log_bytes += encoded_size
            if runtime.pending_file_log_bytes >= LOG_FILE_FLUSH_BYTES:
                # This method is called under the shared app-state lock.
                # Queue a coalesced flush instead of touching the filesystem
                # here; a slow disk must never stall UI snapshot readers.
                self._run_async(
                    f"log-flush-{runtime.config.app_id}",
                    self._flush_runtime_log_file,
                    runtime.config.app_id,
                )

    def _flush_runtime_log_file(self, app_id: str) -> None:
        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None or not runtime.log_file_path:
                return
            log_path = runtime.log_file_path
            file_lock = runtime.log_file_lock
        # Serialize flushers for this file, then take a brief state lock only
        # to detach the bounded in-memory payload. The actual write is outside
        # the manager lock so status/UI reads remain responsive.
        with file_lock:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is None or runtime.log_file_path != log_path or not runtime.pending_file_log:
                    return
                payload = "".join(runtime.pending_file_log)
                runtime.pending_file_log.clear()
                runtime.pending_file_log_bytes = 0
            try:
                with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(payload)
            except OSError:
                # A full or unwritable disk must not create an unbounded
                # memory queue. The bounded live display cache remains.
                return

    def _flush_pending_log_files(self) -> None:
        with self._lock:
            app_ids = [
                app_id for app_id, runtime in self._apps.items()
                if runtime.pending_file_log and runtime.log_file_path
            ]
        for app_id in app_ids:
            # Reuses the bounded lifecycle worker pool and coalesces per app.
            self._run_async(f"log-flush-{app_id}", self._flush_runtime_log_file, app_id)

    def _flush_pending_log_files_sync(self) -> None:
        """Final durability flush, used after the GUI event loop has closed."""
        with self._lock:
            app_ids = [
                app_id for app_id, runtime in self._apps.items()
                if runtime.pending_file_log and runtime.log_file_path
            ]
        for app_id in app_ids:
            self._flush_runtime_log_file(app_id)

    def _schedule_waiting_input_settle(
        self,
        app_id: str,
        process: subprocess.Popen[str],
        baseline_output_at: float | None,
        delay: float = 0.75,
    ) -> None:
        def worker() -> None:
            time.sleep(delay)
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is None or runtime.process is not process:
                    return
                if process.poll() is not None or not runtime.config.interactive:
                    return
                if runtime.last_output_at != baseline_output_at:
                    return
                if not runtime.prompt_pending:
                    return
                if runtime.status != "Waiting Input":
                    runtime.status = "Waiting Input"
                self._emit_snapshot_locked(runtime)

        threading.Thread(target=worker, name=f"stdin-settle-{app_id}", daemon=True).start()

    def _stats_loop(self) -> None:
        while not self._shutdown_event.is_set():
            # Startup resolution is supervised by this existing lightweight
            # monitor thread, not one detached thread per launch.
            self._check_startup_watchdogs()
            self._refresh_stats_once(force_emit=False)
            self._flush_pending_log_files()
            self._stats_wakeup.wait(timeout=PROCESS_METRICS_INTERVAL_SECONDS)
            self._stats_wakeup.clear()

    def _refresh_stats_once(self, force_emit: bool) -> None:
        with self._lock:
            app_ids = list(self._ordered_ids)

        # Several stopped apps may need reconciliation on the same tick.
        # Share one cheap PID/name inventory and each candidate's command line
        # across those apps. On a host with thousands of Python helpers,
        # repeating the full Windows process query for every app can starve
        # the GUI's Python callbacks even though this loop has its own thread.
        process_inventory: list[psutil.Process] | None = None
        command_line_cache: dict[int, list[str] | None] = {}

        for app_id in app_ids:
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is None:
                    continue
                process = runtime.process
                ps_process = runtime.ps_process
                started_at = runtime.started_at
                config = runtime.config
                status = runtime.status

            if process is not None:
                exit_code = process.poll()
                if exit_code is not None:
                    self._finalize_process(app_id, process, exit_code, expected_stop=False, clear_pending=True)
                    continue
                if status == "Unknown":
                    # Popen.poll is authoritative for this exact child. Recover
                    # from a transient startup/psutil verification failure.
                    with self._lock:
                        runtime = self._apps.get(app_id)
                        if runtime is not None and runtime.process is process and runtime.status == "Unknown":
                            runtime.status = "Running"
                            runtime.last_error = ""
                            self._append_log_line_locked(runtime, f"[Runner] Reconciled live child PID={process.pid} as Running\n")
                            self._emit_snapshot_locked(runtime)
                    status = "Running"

            # Process discovery is deliberately rate limited.  Force refresh
            # (used by startup reconciliation and the user Refresh action)
            # remains immediate, so adoption of an already-running legacy
            # process is never delayed during recovery.
            scan_due = force_emit or (
                self._monitor_external_processes
                and time.monotonic() - runtime.last_external_process_scan >= EXTERNAL_PROCESS_RECONCILE_SECONDS
            )
            if process is None and (ps_process is None or status != "Already Running") and scan_due:
                with self._lock:
                    current = self._apps.get(app_id)
                    if current is not None:
                        current.last_external_process_scan = time.monotonic()
                if process_inventory is None:
                    process_inventory = _process_name_inventory()
                existing_process = self._find_existing_process(
                    config, process_inventory=process_inventory, command_line_cache=command_line_cache
                )
                if existing_process is not None:
                    with self._lock:
                        runtime = self._apps.get(app_id)
                        if runtime is None:
                            continue
                        runtime.process = None
                        runtime.ps_process = existing_process
                        runtime.pid = existing_process.pid
                        runtime.started_at = self._safe_create_time(existing_process)
                        runtime.external_process = True
                        runtime.status = "Already Running"
                        runtime.pending_action = False
                        runtime.last_error = f"{runtime.config.name} is already running."
                        self._emit_snapshot_locked(runtime)
                    ps_process = existing_process
                    started_at = self._safe_create_time(existing_process)
                    status = "Already Running"
                else:
                    if force_emit or status in {"Already Running", "Unknown"}:
                        with self._lock:
                            runtime = self._apps.get(app_id)
                            if runtime is None:
                                continue
                            if runtime.status == "Already Running":
                                runtime.ps_process = None
                                runtime.pid = None
                                runtime.cpu_percent = None
                                runtime.ram_mb = None
                                runtime.started_at = None
                            runtime.external_process = False
                            runtime.last_error = ""
                            runtime.scheduled_action = ""
                            runtime.status = "Stopped"
                            runtime.pending_action = False
                            self._clear_start_attempt_locked(runtime)
                            if status == "Unknown":
                                self._append_log_line_locked(runtime, "[Runner] Reconciliation verified no matching process is running\n")
                        self._emit_snapshot_locked(runtime)
                    continue

            if ps_process is None:
                if force_emit:
                    with self._lock:
                        runtime = self._apps.get(app_id)
                        if runtime is not None:
                            self._emit_snapshot_locked(runtime)
                continue
            try:
                cpu_percent = ps_process.cpu_percent(None)
                ram_mb = ps_process.memory_info().rss / (1024 * 1024)
            except psutil.Error:
                with self._lock:
                    runtime = self._apps.get(app_id)
                    if runtime is None:
                        continue
                    if runtime.status == "Already Running":
                        runtime.ps_process = None
                        runtime.pid = None
                        runtime.cpu_percent = None
                        runtime.ram_mb = None
                        runtime.started_at = None
                        runtime.external_process = False
                        runtime.last_error = ""
                        runtime.scheduled_action = ""
                        runtime.status = "Stopped"
                        self._emit_snapshot_locked(runtime)
                continue
            changed = force_emit
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is None:
                    continue
                if runtime.process is not process and not runtime.external_process:
                    continue
                if runtime.cpu_percent is None or abs(runtime.cpu_percent - cpu_percent) >= 0.25:
                    runtime.cpu_percent = cpu_percent
                    changed = True
                if runtime.ram_mb is None or abs(runtime.ram_mb - ram_mb) >= 0.5:
                    runtime.ram_mb = ram_mb
                    changed = True
                if changed:
                    self._emit_snapshot_locked(runtime)

    def _emit_snapshot_locked(self, runtime: AppRuntime) -> None:
        snapshot = self._snapshot_locked(runtime)
        if runtime.last_snapshot == snapshot:
            return
        runtime.last_snapshot = snapshot
        self.state_changed.emit(runtime.config.app_id, snapshot)

    def _snapshot_locked(self, runtime: AppRuntime) -> dict[str, Any]:
        uptime_seconds = None
        if runtime.started_at and (
            (runtime.process and runtime.process.poll() is None) or runtime.external_process
        ):
            uptime_seconds = max(0, int(time.time() - runtime.started_at))
        return {
            "id": runtime.config.app_id,
            "name": runtime.config.name,
            "runner_path": None if runtime.config.app_type == "docker_compose" else runtime.config.runner_path,
            "app_type": runtime.config.app_type,
            "compose": dict(runtime.config.compose),
            "docker": dict(runtime.deployment_state),
            "args": list(runtime.config.args),
            "cwd": runtime.config.cwd,
            "startup_input": runtime.config.startup_input,
            "interactive": runtime.config.interactive,
            "auto_start": runtime.config.auto_start,
            "protected": runtime.config.protected,
            "service_group": runtime.config.service_group,
            "dependencies": list(runtime.config.dependencies),
            "health_check": dict(runtime.config.health_check),
            "persistence": dict(runtime.config.persistence),
            "deployments": dict(runtime.config.deployments),
            "sync": dict(runtime.config.sync),
            "startup_timeout_seconds": runtime.config.startup_timeout_seconds,
            "can_accept_input": bool(
                runtime.process is not None
                and runtime.process.poll() is None
                and runtime.process.stdin is not None
                and not runtime.external_process
                and not runtime.config.visible_console
            ),
            "visible_console": runtime.config.visible_console,
            "status": runtime.status,
            "pid": runtime.pid,
            "cpu_percent": runtime.cpu_percent if (runtime.process or runtime.external_process) else None,
            "ram_mb": runtime.ram_mb if (runtime.process or runtime.external_process) else None,
            "uptime_seconds": uptime_seconds,
            "pending_action": runtime.pending_action,
            "last_exit_code": runtime.last_exit_code,
            "last_error": runtime.last_error,
            "log_file_path": str(runtime.log_file_path) if runtime.log_file_path else "",
            "full_command": self._full_command_for_config(runtime.config),
        }

    @staticmethod
    def _full_command_for_config(config: AppConfig) -> str:
        if config.app_type == "docker_compose":
            compose = dict(config.compose)
            return f"docker compose -f {compose.get('compose_file', 'docker-compose.yml')} up -d"
        # Snapshot generation runs under the shared state lock and is called
        # by GUI selection/status refreshes. Do not resolve interpreters here:
        # build_start_command scans the project tree, PATH and virtualenvs.
        # Actual resolution remains on the asynchronous app-start worker.
        suffix = Path(config.runner_path).suffix.lower()
        if suffix == ".py":
            command = ["python", "-u", config.runner_path, *config.args]
        elif suffix in {".js", ".mjs"}:
            command = ["node", config.runner_path, *config.args]
        elif suffix in {".bat", ".cmd"}:
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/D", "/C", config.runner_path, *config.args]
        else:
            command = [config.runner_path, *config.args]
        return subprocess.list2cmdline([part for part in command if part])

    def _emit_error(self, app_id: str, message: str) -> None:
        self.error_occurred.emit(app_id, message)

    def _send_startup_input(self, app_id: str, process: subprocess.Popen[str], stdin_text: str) -> None:
        def worker() -> None:
            with self._lock:
                runtime = self._apps.get(app_id)
                if not runtime or runtime.process is not process:
                    return
            if not stdin_text or process.stdin is None:
                return
            try:
                process.stdin.write(stdin_text)
                process.stdin.flush()
            except Exception as exc:
                self._emit_error(app_id, f"Startup input failed: {exc}")
                return
            with self._lock:
                runtime = self._apps.get(app_id)
                if runtime is None or runtime.process is not process:
                    return
                self._append_log_line_locked(runtime, "[Runner] Startup input sent\n")
                if runtime.config.interactive:
                    runtime.status = "Running"
                    runtime.prompt_pending = False
                    self._clear_start_attempt_locked(runtime)
                    self._emit_snapshot_locked(runtime)

        threading.Thread(target=worker, name=f"stdin-{app_id}", daemon=True).start()

    def _stop_external_process(self, app_id: str, pid: int, clear_pending: bool, forced: bool = False) -> None:
        exit_code: int | None = None
        try:
            if forced:
                self._kill_process_tree(pid)
            else:
                self._terminate_process_tree(pid)
            try:
                external = psutil.Process(pid)
                external.wait(timeout=5.0)
            except psutil.Error:
                pass
            exit_code = 0
        except Exception as exc:
            self._emit_error(app_id, f"Stop failed: {exc}")
            exit_code = None

        with self._lock:
            runtime = self._apps[app_id]
            runtime.process = None
            runtime.ps_process = None
            runtime.pid = None
            runtime.cpu_percent = None
            runtime.ram_mb = None
            runtime.started_at = None
            runtime.last_exit_code = exit_code
            if clear_pending:
                runtime.pending_action = False
                runtime.scheduled_action = ""
            self._clear_start_attempt_locked(runtime)
            runtime.external_process = False
            runtime.last_error = ""
            runtime.saw_output = False
            runtime.last_output_at = None
            runtime.prompt_pending = False
            runtime.status = "Stopped by Runner"
            self._append_log_line_locked(
                runtime,
                "[Runner] Force-stopped external process\n" if forced else "[Runner] Stopped by Runner\n",
            )
            self._emit_snapshot_locked(runtime)

    def _resolve_live_process(self, runtime: AppRuntime) -> psutil.Process | None:
        if runtime.process is not None and runtime.process.poll() is None:
            try:
                return psutil.Process(runtime.process.pid)
            except psutil.Error:
                return None
        if runtime.ps_process is not None:
            try:
                if runtime.ps_process.is_running():
                    return runtime.ps_process
            except psutil.Error:
                pass
        if runtime.pid is not None and psutil.pid_exists(runtime.pid):
            try:
                process = psutil.Process(runtime.pid)
            except psutil.Error:
                return None
            if self._process_matches_config(process, runtime.config):
                return process
        return None

    def _find_existing_process(
        self,
        config: AppConfig,
        *,
        process_inventory: list[psutil.Process] | None = None,
        command_line_cache: dict[int, list[str] | None] | None = None,
    ) -> psutil.Process | None:
        target_runner = self._safe_resolve_path(config.runner_path)
        if not target_runner:
            return None

        target_suffix = Path(target_runner).suffix.lower()
        target_cwd = self._safe_resolve_path(config.cwd) or ""
        target_name = Path(target_runner).name.lower()

        # Fetch only PID/name for the full process table. On Windows,
        # process_iter(..., "cmdline", "cwd") calls into the OS for every
        # process before this loop can reject an irrelevant name. A large
        # population of Python helpers made that eager query monopolize the
        # GIL and leave the Qt window unresponsive.
        processes = process_inventory if process_inventory is not None else _process_name_inventory()
        candidate_count = 0
        for process in processes:
            try:
                info = getattr(process, "info", {}) or {}
                process_name = str(info.get("name") or "").lower()
                if target_suffix == ".py":
                    if not process_name.startswith("python"):
                        continue
                elif target_suffix in {".js", ".mjs"}:
                    if not process_name.startswith("node"):
                        continue
                elif target_suffix in {".bat", ".cmd"}:
                    if process_name not in {"cmd.exe", "powershell.exe", "pwsh.exe"}:
                        continue
                else:
                    if process_name and process_name != target_name:
                        continue
                    info["exe"] = info.get("exe") or self._safe_process_exe(process)
                    exe_path = self._safe_resolve_path(info.get("exe"))
                    if exe_path and exe_path == target_runner:
                        return process
                candidate_count += 1
                if candidate_count % 32 == 0:
                    # This runs only on a monitor/start worker. Yield to Qt's
                    # main thread during unusually large candidate scans.
                    time.sleep(0)
                if command_line_cache is not None and process.pid in command_line_cache:
                    info["cmdline"] = command_line_cache[process.pid]
                else:
                    if os.name == "nt":
                        try:
                            # A standard-user GUI cannot inspect elevated or
                            # SYSTEM command lines. The token check avoids an
                            # expensive command-line query for those PIDs.
                            process.username()
                        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                            if command_line_cache is not None:
                                command_line_cache[process.pid] = None
                            continue
                    info["cmdline"] = self._safe_process_cmdline(process)
                    if command_line_cache is not None:
                        command_line_cache[process.pid] = info["cmdline"]
                if not info["cmdline"]:
                    continue
                if self._process_matches_config(
                    process,
                    config,
                    target_runner=target_runner,
                    target_cwd=target_cwd,
                    target_suffix=target_suffix,
                ):
                    return process
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return None

    def _find_existing_process_with_retry(self, config: AppConfig, attempts: int = 1, delay: float = 0.15) -> psutil.Process | None:
        # This runs on the critical path between acquiring a protected lease
        # and starting its guarded child.  A full psutil scan can be expensive
        # on a busy server, and repeating it three times can consume a short
        # lease/fencing window.  One complete snapshot is enough to reject an
        # existing duplicate; regular monitoring continues to reconcile it.
        for attempt in range(attempts):
            process = self._find_existing_process(config)
            if process is not None:
                return process
            if attempt < attempts - 1 and not self._shutdown_event.is_set():
                time.sleep(delay)
        return None

    def _process_matches_config(
        self,
        process: psutil.Process,
        config: AppConfig,
        *,
        target_runner: str | None = None,
        target_cwd: str | None = None,
        target_suffix: str | None = None,
    ) -> bool:
        target_runner = target_runner or self._safe_resolve_path(config.runner_path)
        if not target_runner:
            return False
        target_suffix = target_suffix or Path(target_runner).suffix.lower()
        target_cwd = target_cwd if target_cwd is not None else (self._safe_resolve_path(config.cwd) or "")
        info = getattr(process, "info", {}) or {}
        raw_cmdline = info.get("cmdline")
        if raw_cmdline is None:
            raw_cmdline = self._safe_process_cmdline(process) or []
        cmdline = [str(part) for part in raw_cmdline]
        process_cwd: str | None = info.get("cwd")

        if target_suffix in {".py", ".js", ".mjs"}:
            if not cmdline:
                return False
            runner_name = Path(target_runner).name.lower()
            for part in cmdline[1:]:
                if Path(part).name.lower() != runner_name:
                    continue
                if Path(part).is_absolute():
                    candidate = self._safe_resolve_arg_path(part, None)
                else:
                    if process_cwd is None:
                        process_cwd = self._safe_process_cwd(process)
                    candidate = self._safe_resolve_arg_path(part, process_cwd)
                if candidate and candidate == target_runner:
                    return True
            if target_cwd and any(Path(part).name.lower() == runner_name for part in cmdline[1:]):
                if process_cwd is None:
                    process_cwd = self._safe_process_cwd(process)
                if process_cwd and process_cwd == target_cwd:
                    return True
            return False

        if target_suffix in {".bat", ".cmd"}:
            if not cmdline:
                return False
            runner_name = Path(target_runner).name.lower()
            for part in cmdline[1:]:
                if Path(part).name.lower() != runner_name:
                    continue
                if Path(part).is_absolute():
                    candidate = self._safe_resolve_arg_path(part, None)
                else:
                    if process_cwd is None:
                        process_cwd = self._safe_process_cwd(process)
                    candidate = self._safe_resolve_arg_path(part, process_cwd)
                if candidate and candidate == target_runner:
                    return True
            if target_cwd and any(Path(part).name.lower() == runner_name for part in cmdline[1:]):
                if process_cwd is None:
                    process_cwd = self._safe_process_cwd(process)
                if process_cwd and process_cwd == target_cwd:
                    return True
            return False

        exe_path = self._safe_resolve_path(info.get("exe")) or self._safe_process_exe(process)
        if exe_path and exe_path == target_runner:
            return True
        if target_cwd and cmdline and Path(cmdline[0]).name.lower() == Path(target_runner).name.lower():
            if process_cwd is None:
                process_cwd = self._safe_process_cwd(process)
            if process_cwd and process_cwd == target_cwd:
                candidate = self._safe_resolve_arg_path(cmdline[0], process_cwd)
                if candidate and candidate == target_runner:
                    return True
        return False

    @staticmethod
    def _safe_resolve_path(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return os.path.normcase(str(Path(value).expanduser().resolve()))
        except OSError:
            return None

    @staticmethod
    def _safe_resolve_arg_path(value: str, cwd: str | None) -> str | None:
        candidate = Path(value)
        try:
            if candidate.is_absolute():
                return os.path.normcase(str(candidate.resolve()))
            if cwd:
                return os.path.normcase(str((Path(cwd) / candidate).resolve()))
            return os.path.normcase(str(candidate.resolve()))
        except OSError:
            return None

    @staticmethod
    def _safe_process_cwd(process: psutil.Process) -> str | None:
        try:
            return os.path.normcase(str(Path(process.cwd()).resolve()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    @staticmethod
    def _safe_process_cmdline(process: psutil.Process) -> list[str] | None:
        try:
            return [str(part) for part in process.cmdline()]
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    @staticmethod
    def _safe_process_exe(process: psutil.Process) -> str | None:
        try:
            return os.path.normcase(str(Path(process.exe()).resolve()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    @staticmethod
    def _safe_create_time(process: psutil.Process) -> float | None:
        try:
            return process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def _serialize_config(self, config: AppConfig) -> dict[str, Any]:
        return {
            "id": config.app_id,
            "name": config.name,
            "runner_path": None if config.app_type == "docker_compose" else config.runner_path,
            "args": list(config.args),
            "cwd": config.cwd,
            "startup_input": config.startup_input,
            "interactive": config.interactive,
            "visible_console": config.visible_console,
            "auto_start": config.auto_start,
            "protected": config.protected,
            "service_group": config.service_group,
            "dependencies": list(config.dependencies),
            "health_check": dict(config.health_check),
            "persistence": dict(config.persistence),
            "deployments": dict(config.deployments),
            "sync": dict(config.sync),
            "startup_timeout_seconds": config.startup_timeout_seconds,
            "env": dict(config.env),
            "app_type": config.app_type,
            "compose": dict(config.compose),
        }

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name)
        return cleaned or "app"
