from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from manager.process_manager import ProcessManager


class StartupStateMachineTests(unittest.TestCase):
    def make_manager(self, root: Path, source: str, *, interactive: bool = False) -> ProcessManager:
        app = root / "test_app.py"
        app.write_text(source, encoding="utf-8")
        manager = ProcessManager([{
            "id": "test", "name": "Isolated Test", "runner_path": str(app),
            "cwd": str(root), "interactive": interactive, "startup_timeout_seconds": 8,
        }], root / "logs", reconcile_existing=False)
        # The tested contract is launch/state reconciliation, not walking the
        # host process table; all candidate applications are private temp files.
        manager._find_existing_process_with_retry = lambda _config, **_kwargs: None
        self.addCleanup(lambda: self.cleanup_manager(manager))
        return manager

    @staticmethod
    def cleanup_manager(manager: ProcessManager) -> None:
        # Test-owned children only; production processes/config are never used.
        for runtime in list(manager._apps.values()):
            process = runtime.process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        manager.shutdown(stop_applications=False)

    @staticmethod
    def wait_for(manager: ProcessManager, *statuses: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = manager.snapshot("test")
            if snapshot["status"] in statuses:
                return snapshot
            time.sleep(0.05)
        return manager.snapshot("test")

    def test_successful_start_transitions_to_running_with_pid_and_diagnostics(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "import time\ntime.sleep(10)\n")
            manager.start_app("test")
            snapshot = self.wait_for(manager, "Running")
            self.assertEqual("Running", snapshot["status"])
            self.assertIsInstance(snapshot["pid"], int)
            log = manager.get_log_cache_text("test")
            self.assertIn("Launch command:", log)
            self.assertIn(f"Spawn succeeded PID={snapshot['pid']}", log)
            self.assertIn("Startup verified state=Running", log)

    def test_immediate_nonzero_exit_leaves_starting_and_reports_exit_code(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "import sys\nsys.exit(7)\n")
            manager.start_app("test")
            snapshot = self.wait_for(manager, "Crashed", "Stopped")
            self.assertIn(snapshot["status"], {"Crashed", "Stopped"})
            self.assertNotEqual("Starting", snapshot["status"])
            self.assertIn("exit_code=7", manager.get_log_cache_text("test"))

    def test_invalid_executable_reports_actual_spawn_error(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            root = Path(directory)
            manager = ProcessManager([{
                "id": "test", "name": "Bad executable", "runner_path": str(root / "missing.exe"),
                "cwd": str(root),
            }], root / "logs", reconcile_existing=False)
            manager._find_existing_process_with_retry = lambda _config, **_kwargs: None
            self.addCleanup(lambda: self.cleanup_manager(manager))
            manager.start_app("test")
            snapshot = self.wait_for(manager, "Crashed")
            self.assertEqual("Crashed", snapshot["status"])
            self.assertTrue(snapshot["last_error"])
            self.assertIn("Start failed", manager.get_log_cache_text("test"))

    def test_interactive_prompt_transitions_to_waiting_input(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "print('Enter choice:', flush=True)\ninput()\n", interactive=True)
            manager.start_app("test")
            snapshot = self.wait_for(manager, "Waiting Input")
            self.assertEqual("Waiting Input", snapshot["status"])
            self.assertIsInstance(snapshot["pid"], int)
            self.assertNotEqual("Starting", snapshot["status"])

    def test_repeated_start_clicks_do_not_spawn_duplicates_during_verification(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "import time\ntime.sleep(10)\n")
            for _ in range(20):
                manager.start_app("test")
            snapshot = self.wait_for(manager, "Running")
            self.assertIsNotNone(snapshot["pid"])
            self.assertEqual(1, manager.get_log_cache_text("test").count("Spawn succeeded PID="))

    def test_restart_and_individual_stop_complete_their_state_transitions(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            manager = self.make_manager(Path(temp_directory.name), "import time\ntime.sleep(10)\n")
            manager.start_app("test")
            first = self.wait_for(manager, "Running")
            first_pid = first["pid"]
            manager.restart_app("test")
            restarted = self.wait_for(manager, "Running")
            self.assertIsNotNone(restarted["pid"])
            self.assertNotEqual(first_pid, restarted["pid"])
            manager.stop_app("test")
            stopped = self.wait_for(manager, "Stopped by Runner", "Stopped")
            self.assertIn(stopped["status"], {"Stopped by Runner", "Stopped"})
            self.assertIsNone(stopped["pid"])

    def test_already_running_matching_process_is_external_not_relaunched(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            root = Path(directory)
            app = root / "test_app.py"
            app.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            child = subprocess.Popen([sys.executable, str(app)], cwd=root)
            def stop_test_child() -> None:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
            self.addCleanup(stop_test_child)
            manager = ProcessManager([{
                "id": "test", "name": "Isolated Test", "runner_path": str(app), "cwd": str(root),
            }], root / "logs", reconcile_existing=False)
            self.addCleanup(lambda: self.cleanup_manager(manager))
            with patch.object(manager, "_find_existing_process_with_retry", return_value=psutil.Process(child.pid)):
                manager.start_app("test")
                deadline = time.monotonic() + 3
                while manager.snapshot("test")["status"] == "Starting" and time.monotonic() < deadline:
                    time.sleep(0.02)
            snapshot = manager.snapshot("test")
            self.assertEqual("Already Running", snapshot["status"])
            self.assertEqual(child.pid, snapshot["pid"])
            self.assertNotIn("Spawn succeeded PID=", manager.get_log_cache_text("test"))

    def test_watchdog_bounds_no_pid_start_and_allows_retry(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "import time\ntime.sleep(10)\n")
            runtime = manager._apps["test"]
            with manager._lock:
                manager._begin_start_attempt_locked(runtime)
                runtime.status = "Starting"
                runtime.startup_deadline_monotonic = time.monotonic() - 1
                runtime.pending_action = True
            manager._check_startup_watchdogs()
            snapshot = manager.snapshot("test")
            self.assertEqual("Unknown", snapshot["status"])
            self.assertFalse(snapshot["pending_action"])
            self.assertIn("watchdog expired", manager.get_log_cache_text("test").lower())

    def test_temporary_process_verification_failure_recovers_without_restart(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            directory = temp_directory.name
            manager = self.make_manager(Path(directory), "import time\ntime.sleep(10)\n")
            manager.start_app("test")
            deadline = time.monotonic() + 5
            while manager.snapshot("test")["pid"] is None and time.monotonic() < deadline:
                time.sleep(0.02)
            pid = manager.snapshot("test")["pid"]
            self.assertIsNotNone(pid)
            original = psutil.pid_exists
            calls = 0

            def transient(pid_value: int) -> bool:
                nonlocal calls
                calls += 1
                return False if calls == 1 else original(pid_value)

            runtime = manager._apps["test"]
            with manager._lock:
                runtime.status = "Starting"
                manager._begin_start_attempt_locked(runtime)
                runtime.start_requested_monotonic = time.monotonic() - 1.1
            with patch("manager.process_manager.psutil.pid_exists", side_effect=transient):
                manager._check_startup_watchdogs()
                manager._check_startup_watchdogs()
            snapshot = manager.snapshot("test")
            self.assertEqual("Running", snapshot["status"])
            self.assertEqual(pid, snapshot["pid"])
            self.assertEqual(1, manager.get_log_cache_text("test").count("Spawn succeeded PID="))

    def test_start_all_continues_after_one_app_fails_and_stop_all_cleans_successes(self):
        if True:
            temp_directory = tempfile.TemporaryDirectory()
            self.addCleanup(temp_directory.cleanup)
            root = Path(temp_directory.name)
            records = []
            for app_id, file_name, source in (
                ("good-one", "one.py", "import time\ntime.sleep(10)\n"),
                ("bad", "missing.exe", ""),
                ("good-two", "two.py", "import time\ntime.sleep(10)\n"),
            ):
                if source:
                    (root / file_name).write_text(source, encoding="utf-8")
                records.append({
                    "id": app_id, "name": app_id, "runner_path": str(root / file_name),
                    "cwd": str(root), "startup_timeout_seconds": 8,
                })
            manager = ProcessManager(records, root / "logs", reconcile_existing=False)
            manager._find_existing_process_with_retry = lambda _config, **_kwargs: None
            self.addCleanup(lambda: self.cleanup_manager(manager))
            manager.start_all()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                states = {app_id: manager.snapshot(app_id)["status"] for app_id in ("good-one", "bad", "good-two")}
                if states["good-one"] == "Running" and states["bad"] == "Crashed" and states["good-two"] == "Running" and not manager._batch_busy:
                    break
                time.sleep(0.05)
            self.assertEqual("Running", manager.snapshot("good-one")["status"])
            self.assertEqual("Crashed", manager.snapshot("bad")["status"])
            self.assertEqual("Running", manager.snapshot("good-two")["status"])
            self.assertIn("Start failed", manager.get_log_cache_text("bad"))

            manager.stop_all()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(manager.snapshot(app_id)["status"] in {"Stopped", "Stopped by Runner", "Crashed"} for app_id in ("good-one", "bad", "good-two")) and not manager._batch_busy:
                    break
                time.sleep(0.05)
            self.assertIn(manager.snapshot("good-one")["status"], {"Stopped", "Stopped by Runner"})
            self.assertIn(manager.snapshot("good-two")["status"], {"Stopped", "Stopped by Runner"})


if __name__ == "__main__":
    unittest.main()
