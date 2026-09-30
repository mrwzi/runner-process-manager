from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cluster.client import AgentClient
from ui.app import MainWindow


class AgentDegradedLocalControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if predicate():
                return True
            time.sleep(0.03)
        return bool(predicate())

    def test_connected_ui_actions_use_the_same_authoritative_snapshot_as_the_table(self):
        agent_snapshot = {"id": "app", "status": "Already Running", "pid": 38196, "protected": False}
        stale_fallback = {"id": "app", "status": "Stopped", "pid": None, "protected": False}
        window = SimpleNamespace(
            selected_app_id="app",
            manager=SimpleNamespace(
                remote_managed=True,
                connection_state="connected",
                snapshots_ready=True,
                snapshot=lambda _app_id: stale_fallback,
                app_definitions=lambda: [stale_fallback],
            ),
            model=SimpleNamespace(
                snapshot_for_app=lambda _app_id: dict(agent_snapshot),
                snapshots=lambda: [dict(agent_snapshot)],
            ),
        )

        self.assertEqual(MainWindow._selected_snapshot(window)["status"], "Already Running")
        self.assertEqual(MainWindow._action_snapshots(window)[0]["status"], "Already Running")

        # In degraded mode the locally verified fallback snapshot remains the
        # source for enabled actions, rather than stale Agent/table state.
        window.manager.connection_state = "reconnecting"
        self.assertEqual(MainWindow._selected_snapshot(window)["status"], "Stopped")
        self.assertEqual(MainWindow._action_snapshots(window)[0]["status"], "Stopped")

    def test_connected_log_reads_use_agent_output_not_local_idle_fallback(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        entry = root / "app.py"
        entry.write_text("pass\n", encoding="utf-8")
        record = {"id": "app", "name": "App", "runner_path": str(entry), "cwd": str(root), "protected": False}
        client = AgentClient(root / "runtime", fallback_apps=[record], autostart=False)
        self.addCleanup(client.shutdown)
        self.assertTrue(self._wait(lambda: client.local_manager is not None))
        local_reads = []
        client.local_manager.get_log_cache_text = lambda _app_id: local_reads.append(True) or "Idle\\n"
        requests = []
        client._request_json = lambda method, path, **kwargs: requests.append((method, path, kwargs)) or {"text": "real Agent output\\n"}
        client.snapshots_ready = True
        client._set_connection("connected", "test Agent")

        self.assertEqual(client.get_log_cache_text("app"), "real Agent output\\n")
        self.assertEqual(requests[0][0:2], ("GET", "/v1/logs/app"))
        self.assertEqual(local_reads, [])
        self.assertTrue(client.has_live_log_output("app"))
        self.assertEqual(client.drain_pending_log_lines("app"), [])
        client.clear_log_cache("app")
        self.assertFalse(client.has_live_log_output("app"))

    def test_local_controls_and_observed_state_survive_agent_failure(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        records = []
        for name in ("local-one", "local-two"):
            entry = root / f"{name}.py"
            entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            records.append({
                "id": name, "name": name, "runner_path": str(entry), "cwd": str(root),
                "app_type": "process", "protected": False, "auto_start": False,
            })

        client = AgentClient(root / "runtime", fallback_apps=records, autostart=False)
        self.addCleanup(client.shutdown)
        self.assertTrue(self._wait(lambda: client.local_manager is not None))
        client._set_connection("unhealthy", "simulated timeout")
        # Exercise control dispatch without a whole-machine discovery scan;
        # duplicate detection itself is already covered by ProcessManager.
        client.local_manager._find_existing_process_with_retry = lambda *_args, **_kwargs: None
        self.assertEqual(MainWindow._bulk_actions_available(client.app_definitions(), agent_ready=False), (True, False))

        client.start_all()
        self.assertTrue(self._wait(lambda: all(client.snapshot(item["id"])["status"] in {"Running", "Waiting Input"} for item in records)))
        pids = {item["id"]: client.snapshot(item["id"])["pid"] for item in records}
        self.assertTrue(all(pids.values()))
        self.assertEqual(MainWindow._bulk_actions_available(client.app_definitions(), agent_ready=False), (False, True))

        # API recovery updates HA health only; it must not replace or relaunch
        # locally observed processes.
        client._set_connection("connected", "simulated recovery")
        self.assertEqual(client.connection_state, "connected")
        self.assertEqual(pids, {item["id"]: client.snapshot(item["id"])["pid"] for item in records})
        client._set_connection("unhealthy", "simulated second timeout")

        client.restart_app("local-one")
        self.assertTrue(self._wait(lambda: client.snapshot("local-one")["status"] == "Running" and client.snapshot("local-one")["pid"] is not None))
        client.stop_app("local-two")
        self.assertTrue(self._wait(lambda: client.snapshot("local-two")["status"] in {"Stopped", "Stopped by Runner"}))
        self.assertEqual(client.connection_state, "unhealthy")
        self.assertIn(client.snapshot("local-two")["status"], {"Stopped", "Stopped by Runner"})
        self.assertEqual(MainWindow._bulk_actions_available(client.app_definitions(), agent_ready=False), (True, True))

        client.stop_all()
        self.assertTrue(self._wait(lambda: all(client.snapshot(item["id"])["status"] in {"Stopped", "Stopped by Runner"} for item in records)))
        self.assertEqual(client.connection_state, "unhealthy")
        self.assertEqual(MainWindow._bulk_actions_available(client.app_definitions(), agent_ready=False), (True, False))


    def test_protected_app_controls_remain_agent_gated(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        entry = root / "protected.py"
        entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        record = {"id": "protected", "name": "Protected", "runner_path": str(entry), "cwd": str(root), "protected": True}
        client = AgentClient(root / "runtime", fallback_apps=[record], autostart=False)
        self.addCleanup(client.shutdown)
        client._set_connection("reconnecting", "offline")
        self.assertEqual(MainWindow._bulk_actions_available(client.app_definitions(), agent_ready=False), (False, False))

    def test_connected_controls_use_agent_owned_processes(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        local_entry = root / "local.py"
        local_entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        protected_entry = root / "protected.py"
        protected_entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        records = [
            {"id": "local", "name": "Local", "runner_path": str(local_entry), "cwd": str(root), "protected": False},
            {"id": "protected", "name": "Protected", "runner_path": str(protected_entry), "cwd": str(root), "protected": True},
        ]
        client = AgentClient(root / "runtime", fallback_apps=records, autostart=False)
        self.addCleanup(client.shutdown)
        client.snapshots_ready = True
        client._set_connection("connected", "simulated healthy Agent")
        requests = []
        local_fallback_calls = []
        client._async_post = lambda path, payload: requests.append((path, payload))
        client._queue_batch_local_operation = lambda method: local_fallback_calls.append(method)

        client.stop_app("local")
        client.start_all()
        client.stop_all()

        self.assertEqual(local_fallback_calls, [])
        self.assertIn(("/v1/apps/stop", {"app_ids": ["local"]}), requests)
        self.assertIn(("/v1/apps/start", {"app_ids": ["local"]}), requests)
        self.assertIn(("/v1/apps/stop", {"app_ids": ["protected"]}), requests)
        self.assertIn(("/v1/ownership/acquire-and-start", {"app_ids": ["protected"]}), requests)

    def test_unknown_local_is_startable_only_through_duplicate_checked_local_path(self):
        unknown_no_pid = [{
            "id": "local", "status": "Unknown", "pid": None,
            "protected": False, "local_control": True,
        }]
        self.assertEqual(MainWindow._bulk_actions_available(unknown_no_pid, agent_ready=False), (True, False))
        unknown_with_pid = [{**unknown_no_pid[0], "pid": 12345}]
        self.assertEqual(MainWindow._bulk_actions_available(unknown_with_pid, agent_ready=False), (False, True))

    def test_local_action_does_not_wait_for_slow_local_manager_initialization(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        entry = root / "local.py"
        entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        record = {"id": "local", "name": "Local", "runner_path": str(entry), "cwd": str(root), "protected": False}
        initializing = threading.Event()
        release_init = threading.Event()
        queued_start_delivered = threading.Event()
        from cluster import client as client_module
        original_init = client_module.ProcessManager.__init__

        def delayed_init(instance, *args, **kwargs):
            initializing.set()
            if not release_init.wait(3):
                raise TimeoutError("test did not release delayed manager initialization")
            original_init(instance, *args, **kwargs)
            instance._resolve_live_process = lambda *_args, **_kwargs: None
            instance.start_app = lambda _app_id: queued_start_delivered.set()

        with patch.object(client_module.ProcessManager, "__init__", delayed_init):
            client = AgentClient(root / "runtime", fallback_apps=[record], autostart=False)
            self.addCleanup(client.shutdown)
            self.assertTrue(initializing.wait(1), "local manager did not initialize asynchronously")
            started = time.monotonic()
            client.start_app("local")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.1, "UI-facing local action waited on manager initialization")
            release_init.set()
            self.assertTrue(self._wait(lambda: client.local_manager is not None))
            self.assertTrue(self._wait(queued_start_delivered.is_set), "queued local Start was not delivered after initialization")


if __name__ == "__main__":
    unittest.main()
