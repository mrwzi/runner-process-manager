from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cluster.client import AgentClient, AgentUnavailable


class AgentClientLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _client(self, root: Path, **kwargs) -> AgentClient:
        client = AgentClient(root, fallback_apps=[{"id": "one", "name": "One"}], autostart=False, **kwargs)
        self.addCleanup(client.shutdown)
        return client

    def _temporary_client(self, **kwargs) -> AgentClient:
        temporary = tempfile.TemporaryDirectory()
        # unittest cleanups are LIFO: the AgentClient closes its log handler
        # before the temp directory is removed.
        self.addCleanup(temporary.cleanup)
        return self._client(Path(temporary.name), **kwargs)

    def test_refused_startup_is_nonmodal_deduplicated_and_retried(self):
        calls = []
        client = self._temporary_client(restart_callback=lambda: (calls.append("restart") is None, "requested"))
        errors = []
        states = []
        client.error_occurred.connect(lambda *_: errors.append("error"))
        client.connection_changed.connect(lambda state, detail: states.append((state, detail)))
        client._startup_deadline = 0
        client._next_restart = 0
        client._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentUnavailable("Runner Agent is unavailable: [WinError 10061] connection refused")
        )
        client._agent_process_info = lambda **_kwargs: (False, None)
        client._health_tick(now=100.0)
        self.assertEqual(client.connection_state, "reconnecting")
        self.assertEqual(calls, ["restart"])
        initial_state_count = len(states)
        client._health_tick(now=101.0)
        self.assertEqual(len(states), initial_state_count)
        self.assertEqual(errors, [])

    def test_startup_grace_and_agent_present_timeout_are_distinguished(self):
        client = self._temporary_client()
        client._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentUnavailable("Runner Agent is unavailable: timed out")
        )
        client._agent_process_info = lambda **_kwargs: (False, None)
        client._health_tick(now=client._started_at + 1)
        self.assertEqual(client.connection_state, "starting")

        client._startup_deadline = 0
        client._agent_process_info = lambda **_kwargs: (True, 1234)
        client._health_tick(now=client._started_at + 20)
        self.assertEqual(client.connection_state, "unhealthy")
        self.assertIn("not responding", client.connection_message)

    def test_connection_recovers_without_restarting_gui(self):
        client = self._temporary_client()
        client._startup_deadline = 0
        client._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentUnavailable("Runner Agent is unavailable: timed out")
        )
        client._agent_process_info = lambda **_kwargs: (True, 1234)
        client._health_tick(now=client._started_at + 20)
        self.assertEqual(client.connection_state, "unhealthy")

        client._request_json = lambda *_args, **_kwargs: {"ok": True, "node_id": "node-a", "pid": 1234}
        client._health_tick(now=client._started_at + 21)
        self.assertEqual(client.connection_state, "connected")
        self.assertEqual(client._last_agent_pid, 1234)

    def test_timeout_and_refusal_are_classified_separately(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        self.assertIn("refused", AgentClient._failure_kind(refused).lower())
        self.assertEqual(AgentClient._failure_kind(timeout), "Timed out")


if __name__ == "__main__":
    unittest.main()
