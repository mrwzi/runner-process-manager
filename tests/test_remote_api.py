from __future__ import annotations

import json
import urllib.error
import urllib.request
import unittest

from cluster.remote_api import RemoteAgentServer
from cluster.transport import TailscaleTransport


class FakeManager:
    def get_log_cache_text(self, app_id): return "safe log"


class FakeCluster:
    node_id = "node-b"


class FakeAgent:
    cluster = FakeCluster()
    manager = FakeManager()
    def status(self): return {"node_id": "node-b", "protocol_version": 1, "secret": "must-redact"}
    def receive_heartbeat(self, payload): return {"accepted": True, "sequence": payload["sequence"]}


class RemoteApiTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"s" * 32
        self.server = RemoteAgentServer(FakeAgent(), "127.0.0.1", 0, {"node-a": self.secret}, None, None, allow_insecure_test=True)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def tearDown(self): self.server.stop()

    def test_unauthenticated_remote_status_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.url + "/v1/status", timeout=2)
        self.assertEqual(401, raised.exception.code)

    def test_paired_transport_can_exchange_heartbeat_and_redacts(self):
        transport = TailscaleTransport("node-a", {"node-b": self.url}, {"node-b": self.secret}, require_https=False)
        result = transport.request("node-b", "POST", "/v1/heartbeat", {"sequence": 9})
        self.assertTrue(result["accepted"])
        status = transport.request("node-b", "GET", "/v1/status")
        self.assertEqual("[REDACTED]", status["secret"])


if __name__ == "__main__": unittest.main()
