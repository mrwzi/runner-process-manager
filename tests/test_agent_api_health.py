from __future__ import annotations

import json
import socket
import threading
import tempfile
import time
import urllib.error
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from cluster.api import AgentApiServer
from cluster.client import AgentClient


class _FastApiAgent:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.cluster = SimpleNamespace(node_id="node-test")
        self.status_calls = []

    def status(self, *, refresh_readiness=True):
        self.status_calls.append(refresh_readiness)
        if refresh_readiness:
            raise AssertionError("Local API status must not synchronously run readiness checks")
        return {"node_id": "node-test", "applications": [], "automatic_failover_eligible": False}


class AgentApiHealthTests(unittest.TestCase):
    def test_authenticated_liveness_and_apps_status_are_fast_and_singleton(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime_root = Path(temporary.name)
        agent = _FastApiAgent(runtime_root)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = AgentApiServer(agent, "127.0.0.1", port)
        self.addCleanup(server.stop)
        server.start()
        token = json.loads((runtime_root / "agent-api.json").read_text(encoding="utf-8"))["token"]

        def get(path: str):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))

        health = get("/v1/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["node_id"], "node-test")
        self.assertEqual(get("/v1/apps")["cluster"]["node_id"], "node-test")
        self.assertEqual(agent.status_calls, [False])

        client = AgentClient(runtime_root, f"http://127.0.0.1:{port}", autostart=False)
        self.addCleanup(client.shutdown)
        client._health_tick()
        self.assertEqual(client.connection_state, "connected")
        self.assertFalse(client.snapshots_ready)
        client._refresh()
        self.assertTrue(client.snapshots_ready)
        record = json.loads((runtime_root / "agent-process.json").read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], __import__("os").getpid())

        with self.assertRaises(OSError):
            AgentApiServer(agent, "127.0.0.1", port)

    def test_slow_status_refresh_never_blocks_local_liveness(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime_root = Path(temporary.name)
        release_status = threading.Event()
        agent = _FastApiAgent(runtime_root)
        original_status = agent.status

        def slow_status(*, refresh_readiness=True):
            release_status.wait(10)
            return original_status(refresh_readiness=refresh_readiness)

        agent.status = slow_status
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = AgentApiServer(agent, "127.0.0.1", port)
        self.addCleanup(server.stop)
        server.start()
        token = json.loads((runtime_root / "agent-api.json").read_text(encoding="utf-8"))["token"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": f"Bearer {token}"}
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=1) as response:
            self.assertTrue(json.loads(response.read().decode())["ok"])
        self.assertLess(time.monotonic() - started, 0.5)
        release_status.set()

    def test_real_agent_api_restart_is_detected_without_restarting_client(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime_root = Path(temporary.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        agent = _FastApiAgent(runtime_root)
        first = AgentApiServer(agent, "127.0.0.1", port)
        first.start()
        client = AgentClient(runtime_root, f"http://127.0.0.1:{port}", autostart=False)
        self.addCleanup(client.shutdown)
        client._health_tick()
        self.assertEqual(client.connection_state, "connected")

        first.stop()
        client._startup_deadline = 0
        client._next_restart = time.monotonic() + 60
        client._health_tick()
        self.assertEqual(client.connection_state, "reconnecting")

        second = AgentApiServer(agent, "127.0.0.1", port)
        self.addCleanup(second.stop)
        second.start()
        client._health_tick()
        self.assertEqual(client.connection_state, "connected")
        self.assertEqual(client._last_agent_pid, __import__("os").getpid())


if __name__ == "__main__":
    unittest.main()
