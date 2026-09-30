from __future__ import annotations

import tempfile
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from manager.process_manager import AppConfig, EXTERNAL_PROCESS_RECONCILE_SECONDS, ProcessManager


class MonitoringPollingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process access behavior")
    def test_inaccessible_python_helpers_do_not_trigger_command_line_queries(self):
        class Candidate:
            def __init__(self, pid, command=None):
                self.pid = pid
                self.info = {"pid": pid, "name": "pythonw.exe"}
                self.command = command
                self.command_queries = 0

            def username(self):
                if self.command is None:
                    raise psutil.AccessDenied(self.pid)
                return "server"

            def cmdline(self):
                self.command_queries += 1
                return self.command

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.py"
            inaccessible = [Candidate(pid) for pid in range(1000, 3000)]
            match = Candidate(3000, ["pythonw.exe", str(target)])
            manager = ProcessManager([], root / "logs")
            self.addCleanup(lambda: manager.shutdown(stop_applications=False))
            with patch("manager.process_manager._process_name_inventory", return_value=[*inaccessible, match]):
                found = manager._find_existing_process(AppConfig("target", "Target", str(target)))
            self.assertIs(found, match)
            self.assertEqual(sum(candidate.command_queries for candidate in inaccessible), 0)
            self.assertEqual(match.command_queries, 1)

    def test_gui_fallback_does_not_repeat_agent_process_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ProcessManager([{
                "id": "local", "name": "Local", "runner_path": str(root / "missing.py"), "cwd": str(root),
            }], root / "logs", reconcile_existing=False, monitor_external_processes=False)
            self.addCleanup(lambda: manager.shutdown(stop_applications=False))
            with patch.object(manager, "_find_existing_process", return_value=None) as find, patch(
                "manager.process_manager._process_name_inventory", return_value=[]
            ):
                manager._apps["local"].last_external_process_scan = 0
                manager._refresh_stats_once(force_emit=False)
                self.assertEqual(find.call_count, 0)
                manager._refresh_stats_once(force_emit=True)
                self.assertEqual(find.call_count, 1)

    def test_stopped_process_is_not_globally_scanned_every_metrics_tick(self):
        """Startup/manual refresh is immediate; idle discovery is bounded."""
        with tempfile.TemporaryDirectory() as directory, patch("manager.process_manager._process_name_inventory", return_value=[]):
            root = Path(directory)
            manager = ProcessManager([{
                "id": "stopped", "name": "Stopped", "runner_path": str(root / "missing.py"), "cwd": str(root),
            }], root / "logs")
            with patch.object(manager, "_find_existing_process", return_value=None) as find:
                manager._apps["stopped"].last_external_process_scan = time.monotonic() - EXTERNAL_PROCESS_RECONCILE_SECONDS - 1
                manager._refresh_stats_once(force_emit=False)
                manager._refresh_stats_once(force_emit=False)
                self.assertEqual(find.call_count, 1)
                manager._apps["stopped"].last_external_process_scan -= EXTERNAL_PROCESS_RECONCILE_SECONDS + 1
                manager._refresh_stats_once(force_emit=False)
                self.assertEqual(find.call_count, 2)
                manager._refresh_stats_once(force_emit=True)
                self.assertEqual(find.call_count, 3)
            manager.shutdown()

    def test_log_output_is_batched_and_bounded_before_disk_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ProcessManager([{
                "id": "logger", "name": "Logger", "runner_path": str(root / "missing.py"), "cwd": str(root),
            }], root / "logs")
            runtime = manager._apps["logger"]
            with manager._lock:
                for index in range(100):
                    manager._append_log_line_locked(runtime, f"line-{index}\\n")
                self.assertEqual(runtime.log_file_path.exists(), False)
                self.assertEqual(len(runtime.log_cache), 100)
                self.assertGreater(runtime.pending_file_log_bytes, 0)
            manager._flush_pending_log_files()
            deadline = time.monotonic() + 2
            while not runtime.log_file_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(runtime.log_file_path.read_text(encoding="utf-8").count("line-"), 100)
            self.assertEqual(runtime.pending_file_log_bytes, 0)
            manager.shutdown()

    def test_slow_log_disk_write_does_not_hold_process_snapshot_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessManager(
                [{"id": "log-test", "name": "Log Test", "runner_path": "missing.py", "cwd": temp}],
                Path(temp) / "logs",
                reconcile_existing=False,
            )
            self.addCleanup(lambda: manager.shutdown(stop_applications=False))
            runtime = manager._apps["log-test"]
            log_path = runtime.log_file_path
            self.assertIsNotNone(log_path)
            writing = threading.Event()
            release_write = threading.Event()
            path_type = type(log_path)
            original_open = path_type.open

            def delayed_open(path, *args, **kwargs):
                if path == log_path:
                    writing.set()
                    release_write.wait(2)
                return original_open(path, *args, **kwargs)

            with patch.object(path_type, "open", delayed_open):
                with manager._lock:
                    manager._append_log_line_locked(runtime, "x" * (70 * 1024) + "\n")
                self.assertTrue(writing.wait(1), "buffer threshold did not schedule a background flush")
                started = time.monotonic()
                snapshot = manager.snapshot("log-test")
                elapsed = time.monotonic() - started
                self.assertEqual(snapshot["status"], "Stopped")
                self.assertLess(elapsed, 0.1, "UI-facing snapshot waited on a slow log file write")
                release_write.set()

    def test_status_snapshot_does_not_scan_virtualenv_or_path(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessManager([{
                "id": "snapshot", "name": "Snapshot", "runner_path": str(Path(temp) / "main.py"), "cwd": temp,
            }], Path(temp) / "logs", reconcile_existing=False)
            self.addCleanup(lambda: manager.shutdown(stop_applications=False))
            with patch("manager.process_manager.build_start_command", side_effect=AssertionError("snapshot scanned project files")):
                snapshot = manager.snapshot("snapshot")
            self.assertEqual(snapshot["status"], "Stopped")
            self.assertIn("python", snapshot["full_command"].lower())


if __name__ == "__main__":
    unittest.main()
