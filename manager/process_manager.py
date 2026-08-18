from __future__ import annotations

import os
import shlex
import sys
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from PySide6.QtCore import QObject, Signal

from .start_command import build_start_command


CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


class ProcessManager(QObject):
    state_changed = Signal(str, object)
    batch_state_changed = Signal(bool, str)
    error_occurred = Signal(str, str)
    registry_changed = Signal(object)

    def __init__(self, apps: list[dict[str, Any]], logs_dir: str | Path) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._stats_wakeup = threading.Event()
        self._logs_dir = Path(logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._batch_busy = False
        self._apps: dict[str, AppRuntime] = {}
        self._ordered_ids: list[str] = []

        for raw_app in apps:
            config = self._build_config(raw_app)
            runtime = AppRuntime(config=config)
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

    def get_log_cache_text(self, app_id: str) -> str:
        with self._lock:
            runtime = self._apps[app_id]
            lines = list(runtime.log_cache)
            status = runtime.status
            visible_console = runtime.config.visible_console
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

    def refresh_all(self) -> None:
        self._run_async("refresh-overview", self._refresh_once_worker)

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._stats_wakeup.set()
        for app_id in self._ordered_ids:
            try:
                self._stop_app_internal(app_id, clear_pending=False, emit=False)
            except Exception:
                continue
        if self._stats_thread.is_alive():
            self._stats_thread.join(timeout=2.0)

    def export_apps(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._serialize_config(self._apps[app_id].config) for app_id in self._ordered_ids]

    def add_app(self, app_data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            config = self._build_config(app_data)
            if config.app_id in self._apps:
                raise ValueError(f"App id '{config.app_id}' already exists")
            runtime = AppRuntime(config=config)
            runtime.log_file_path = self._logs_dir / f"{self._safe_name(config.name)}.log"
            self._apps[config.app_id] = runtime
            self._ordered_ids.append(config.app_id)
            snapshot = self._snapshot_locked(runtime)
        self.registry_changed.emit(self.app_definitions())
        return snapshot

    def update_app(self, app_id: str, app_data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process or runtime.pending_action:
                raise ValueError("Stop the app before editing it")
            merged = self._serialize_config(runtime.config)
            merged.update(app_data)
            merged["id"] = app_id
            config = self._build_config(merged)
            runtime.config = config
            runtime.log_file_path = self._logs_dir / f"{self._safe_name(config.name)}.log"
            snapshot = self._snapshot_locked(runtime)
            self._emit_snapshot_locked(runtime)
        self.registry_changed.emit(self.app_definitions())
        return snapshot

    def remove_app(self, app_id: str) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process or runtime.pending_action:
                raise ValueError("Stop the app before deleting it")
            del self._apps[app_id]
            self._ordered_ids = [existing_id for existing_id in self._ordered_ids if existing_id != app_id]
        self.registry_changed.emit(self.app_definitions())

    def _build_config(self, raw_app: dict[str, Any]) -> AppConfig:
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

    def _run_async(self, name: str, target: Any, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()

    def _queue_app_action(self, app_id: str, action: str, status: str, worker: Any) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action or runtime.scheduled_action or self._batch_busy:
                return
            runtime.scheduled_action = action
            runtime.pending_action = True
            if action == "restart":
                runtime.status = "Stopping" if runtime.process or runtime.external_process else "Starting"
            elif status:
                runtime.status = status
            self._emit_snapshot_locked(runtime)
        self._run_async(f"{action}-{app_id}", worker, app_id)

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
                    if runtime.process is None and not runtime.external_process:
                        runtime.status = "Stopped"
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
                if not runtime.process:
                    runtime.status = "Stopped"
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
                self._start_app_internal(app_id)
        finally:
            self._end_batch()

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
                self._start_app_internal(app_id)
        finally:
            self._end_batch()

    def _stop_all_worker(self) -> None:
        if not self._begin_batch("Stopping all apps..."):
            return
        try:
            for app_id in self._ordered_ids:
                self._stop_app_internal(app_id)
        finally:
            self._end_batch()

    def _refresh_once_worker(self) -> None:
        try:
            self._refresh_stats_once(force_emit=True)
        except Exception as exc:
            self.error_occurred.emit("", f"Refresh failed: {exc}")

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
            if runtime.process and runtime.process.poll() is None:
                runtime.pending_action = False
                runtime.scheduled_action = ""
                self._emit_snapshot_locked(runtime)
                return
            existing_process = self._resolve_live_process(runtime)
            if existing_process is not None:
                runtime.process = None
                runtime.ps_process = existing_process
                runtime.pid = existing_process.pid
                runtime.started_at = self._safe_create_time(existing_process)
                runtime.cpu_percent = None
                runtime.ram_mb = None
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = True
                runtime.status = "Already Running"
                runtime.last_error = f"{runtime.config.name} is already running."
                self._append_log_line_locked(runtime, f"[Runner] Start blocked: PID {existing_process.pid} is already running\n")
                self._emit_snapshot_locked(runtime)
                if not self._batch_busy:
                    self._emit_error(
                        app_id,
                        f"{runtime.config.name} is already running. Stop it first or use Restart.",
                    )
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

        existing_process = self._find_existing_process_with_retry(config)
        if existing_process is not None:
            with self._lock:
                runtime = self._apps[app_id]
                runtime.process = None
                runtime.ps_process = existing_process
                runtime.pid = existing_process.pid
                runtime.started_at = self._safe_create_time(existing_process)
                runtime.cpu_percent = None
                runtime.ram_mb = None
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.external_process = True
                runtime.status = "Already Running"
                runtime.last_error = f"{runtime.config.name} is already running."
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
                self._emit_snapshot_locked(runtime)
            return

        try:
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
            ps_process = psutil.Process(process.pid)
            ps_process.cpu_percent(None)
        except Exception as exc:
            with self._lock:
                runtime = self._apps[app_id]
                runtime.pending_action = False
                runtime.scheduled_action = ""
                runtime.process = None
                runtime.ps_process = None
                runtime.pid = None
                runtime.status = "Crashed"
                runtime.last_error = str(exc)
                runtime.saw_output = False
                self._append_log_line_locked(runtime, f"[Runner] Start failed: {exc}\n")
                self._emit_snapshot_locked(runtime)
            return

        with self._lock:
            runtime = self._apps[app_id]
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
            self._append_log_line_locked(runtime, f"[Runner] Started (PID {process.pid})\n")
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
            runtime.stdout_thread.start()
            runtime.stderr_thread.start()
            if runtime.capture_thread is not None:
                runtime.capture_thread.start()
            runtime.monitor_thread.start()

        self._stats_wakeup.set()
        self._send_startup_input(app_id, process, stdin_text)
        threading.Thread(
            target=self._settle_start_state,
            args=(app_id, process),
            name=f"settle-{app_id}",
            daemon=True,
        ).start()

    def _stop_app_internal(self, app_id: str, clear_pending: bool = True, emit: bool = True) -> None:
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.pending_action and clear_pending and runtime.scheduled_action != "stop":
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
        with self._lock:
            runtime = self._apps[app_id]
            if runtime.process is not process:
                return
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
                self._append_log_line_locked(runtime, "[Runner] Process exited cleanly\n")
            else:
                self._append_log_line_locked(
                    runtime,
                    f"[Runner] Process exited with code {exit_code if exit_code is not None else 'unknown'}\n",
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
            try:
                with runtime.log_file_lock:
                    with runtime.log_file_path.open("a", encoding="utf-8", errors="replace") as handle:
                        handle.write(line)
            except OSError:
                pass

    def _settle_start_state(self, app_id: str, process: subprocess.Popen[str]) -> None:
        time.sleep(0.9)
        with self._lock:
            runtime = self._apps.get(app_id)
            if runtime is None or runtime.process is not process:
                return
            if process.poll() is not None:
                return
            if runtime.status != "Starting":
                return
            if runtime.config.visible_console:
                runtime.status = "Waiting Input" if runtime.config.interactive else "Running"
            elif runtime.config.interactive and not runtime.saw_output and not runtime.config.startup_input.strip():
                runtime.status = "Waiting Input"
            else:
                runtime.status = "Running"
            self._emit_snapshot_locked(runtime)

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
            self._refresh_stats_once(force_emit=False)
            self._stats_wakeup.wait(timeout=1.0)
            self._stats_wakeup.clear()

    def _refresh_stats_once(self, force_emit: bool) -> None:
        with self._lock:
            app_ids = list(self._ordered_ids)

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

            if process is not None and process.poll() is not None:
                process = None

            if process is None and (ps_process is None or status != "Already Running"):
                existing_process = self._find_existing_process(config)
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
                    if force_emit or status == "Already Running":
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
            "runner_path": runtime.config.runner_path,
            "args": list(runtime.config.args),
            "cwd": runtime.config.cwd,
            "startup_input": runtime.config.startup_input,
            "interactive": runtime.config.interactive,
            "auto_start": runtime.config.auto_start,
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
        command: list[str]
        try:
            command, _, _ = build_start_command(
                {
                    "folder": config.cwd,
                    "runner_file": config.runner_path,
                    "arguments": config.args,
                    "startup_input": config.startup_input,
                }
            )
        except Exception:
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

    def _find_existing_process(self, config: AppConfig) -> psutil.Process | None:
        target_runner = self._safe_resolve_path(config.runner_path)
        if not target_runner:
            return None

        target_suffix = Path(target_runner).suffix.lower()
        target_cwd = self._safe_resolve_path(config.cwd) or ""
        target_name = Path(target_runner).name.lower()

        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = getattr(process, "info", {}) or {}
                process_name = str(info.get("name") or Path(str(info.get("exe") or "")).name).lower()
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
                    exe_path = self._safe_resolve_path(info.get("exe"))
                    if exe_path and exe_path == target_runner:
                        return process
                    if process_name and process_name != target_name:
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

    def _find_existing_process_with_retry(self, config: AppConfig, attempts: int = 3, delay: float = 0.15) -> psutil.Process | None:
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
        process_cwd: str | None = None

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
            "runner_path": config.runner_path,
            "args": list(config.args),
            "cwd": config.cwd,
            "startup_input": config.startup_input,
            "interactive": config.interactive,
            "visible_console": config.visible_console,
            "auto_start": config.auto_start,
            "env": dict(config.env),
        }

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name)
        return cleaned or "app"
