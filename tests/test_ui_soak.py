from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONPATH", "src")

import psutil
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.app import MainWindow


class _SoakWindow(MainWindow):
    """Avoid invoking the host's real Windows startup-task query in this test."""

    def _startup_task_exists(self) -> bool:
        return False

    def _startup_command_path(self) -> Path:
        return Path(tempfile.gettempdir()) / "runner-ui-soak-startup.cmd"


class _DashboardManager(QObject):
    state_changed = Signal(str, object)
    batch_state_changed = Signal(bool, str)
    error_occurred = Signal(str, str)
    registry_changed = Signal(object)
    connection_changed = Signal(str, str)
    remote_managed = True
    snapshots_ready = True

    def __init__(self) -> None:
        super().__init__()
        self.connection_state = "connected"
        self._apps = [self._snapshot(name, "Running") for name in ("one", "two", "docker")]
        self._apps[-1]["app_type"] = "docker_compose"

    @staticmethod
    def _snapshot(name: str, status: str) -> dict:
        return {
            "id": name, "name": name, "app_type": "process", "status": status,
            "pending_action": False, "pid": None, "cpu_percent": None, "ram_mb": None,
            "uptime_seconds": None, "last_exit_code": None, "protected": False,
            "local_control": True, "start_allowed": True, "auto_start": False,
            "args": [], "cwd": "", "runner_path": "", "last_error": "",
            "log_file_path": "", "docker": {}, "compose": {}, "dependencies": [],
        }

    def app_definitions(self): return [dict(app) for app in self._apps]
    def export_apps(self): return self.app_definitions()
    def snapshot(self, app_id): return next(dict(app) for app in self._apps if app["id"] == app_id)
    def agent_status(self):
        return {"role": "ACTIVE", "node_name": "Isolated UI test", "applications": self.app_definitions(), "peers": {}, "automatic_failover_eligible": False}
    def get_log_cache_text(self, _app_id):
        time.sleep(0.15)  # Deliberately slow isolated I/O must stay off the UI thread.
        return "isolated log output\n"
    def has_live_log_output(self, _app_id): return False
    def drain_pending_log_lines(self, _app_id): return []
    def shutdown(self): pass
    def start_all(self): pass
    def stop_all(self): pass
    def start_app(self, _app_id): pass
    def stop_app(self, _app_id): pass
    def restart_app(self, _app_id): pass
    def start_auto_start_apps(self): pass


@unittest.skipUnless(os.environ.get("RUNNER_UI_SOAK") == "1", "set RUNNER_UI_SOAK=1 for the 10-minute isolated GUI soak")
class RunnerUiSoakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_idle_and_monitoring_soak_10_minutes(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = _DashboardManager()
            window = _SoakWindow(manager, Path(temp) / "apps.json")
            window.show()
            proc = psutil.Process()
            proc.cpu_percent(None)
            cpu_start = proc.cpu_times().user + proc.cpu_times().system
            rss_start = proc.memory_info().rss
            threads_start = proc.num_threads()
            started = time.monotonic()
            heartbeat = time.monotonic()
            longest_ui_gap = 0.0
            duration = float(os.environ.get("RUNNER_UI_SOAK_SECONDS", "600"))
            while time.monotonic() - started < duration:
                self.app.processEvents()
                now = time.monotonic()
                longest_ui_gap = max(longest_ui_gap, now - heartbeat)
                heartbeat = now
                # Normal idle monitoring only; no apps/services are launched.
                time.sleep(0.05)
            # After the idle interval, exercise rapid selection plus simulated
            # Agent loss/recovery and ordinary local-process snapshots.
            for turn in range(120):
                self.app.processEvents()
                heartbeat = time.monotonic()
                if turn % 4 == 0:
                    window.table.selectRow((turn // 4) % 3)
                if turn % 20 == 0:
                    state = "unhealthy" if (turn // 20) % 2 else "connected"
                    manager.connection_state = state
                    window._agent_connection_changed(state, "isolated simulated Agent timeout/recovery")
                now = time.monotonic()
                longest_ui_gap = max(longest_ui_gap, now - heartbeat)
                time.sleep(0.01)
            elapsed = time.monotonic() - started
            cpu_end = proc.cpu_times().user + proc.cpu_times().system
            rss_end = proc.memory_info().rss
            threads_end = proc.num_threads()
            print(
                f"UI soak seconds={elapsed:.1f} CPU_avg_percent={(cpu_end-cpu_start)/elapsed*100:.3f} "
                f"RAM_start_MB={rss_start/1048576:.1f} RAM_end_MB={rss_end/1048576:.1f} "
                f"threads_start={threads_start} threads_end={threads_end} longest_event_gap={longest_ui_gap:.3f}s"
            )
            slow_log = Path(temp) / "logs" / "runner-ui-performance.log"
            if slow_log.exists():
                print("Slow UI/background operations:\n" + slow_log.read_text(encoding="utf-8", errors="replace"))
            window.close()
            self.assertLess(longest_ui_gap, 0.25, "Qt event loop missed responsiveness bound")
            self.assertLessEqual(threads_end, threads_start + 8, "background worker count grew unexpectedly")
            self.assertLess(rss_end - rss_start, 32 * 1024 * 1024, "UI memory grew excessively during idle soak")


if __name__ == "__main__":
    unittest.main()
