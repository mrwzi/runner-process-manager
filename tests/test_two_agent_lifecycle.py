from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cluster.agent import RunnerAgent
from cluster.config import ConfigStore, atomic_json_write
from cluster.coordinator import LocalCoordinator
from cluster.lease import LeaseStore
from cluster.models import ClusterConfig, NodeRole
from cluster.peer_service import PeerService
from cluster.remote_api import RemoteAgentServer
from cluster.security import IdentityStore
from cluster.transport import TailscaleTransport


def wait_for(predicate, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TwoAgentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.witness = LocalCoordinator(LeaseStore(root / "witness.db"))
        self.roots = {name: root / name for name in ("a", "b")}
        self.ids = {}
        for name, runtime in self.roots.items():
            runtime.mkdir()
            self.ids[name] = IdentityStore(runtime).load_or_create()["node_id"]
        for name in ("a", "b"):
            self._write_config(name)
        self.a = RunnerAgent(self.roots["a"], self.witness)
        self.a.cluster.nodes = [{"node_id": self.ids["b"], "endpoint": "pending"}]
        self.b = RunnerAgent(self.roots["b"], self.witness)
        self.secret = b"z" * 32
        self.server_a = RemoteAgentServer(self.a, "127.0.0.1", 0, {self.ids["b"]: self.secret}, None, None, allow_insecure_test=True)
        self.server_b = RemoteAgentServer(self.b, "127.0.0.1", 0, {self.ids["a"]: self.secret}, None, None, allow_insecure_test=True)
        self.server_a.start(); self.server_b.start()
        self.url_a = f"http://127.0.0.1:{self.server_a.server.server_address[1]}"
        self.url_b = f"http://127.0.0.1:{self.server_b.server.server_address[1]}"
        self.a.cluster.nodes = [{"node_id": self.ids["b"], "endpoint": self.url_b}]
        self.b.cluster.nodes = [{"node_id": self.ids["a"], "endpoint": self.url_a}]
        self.transport_a = TailscaleTransport(self.ids["a"], {self.ids["b"]: self.url_b}, {self.ids["b"]: self.secret}, timeout=.3, require_https=False)
        self.transport_b = TailscaleTransport(self.ids["b"], {self.ids["a"]: self.url_a}, {self.ids["a"]: self.secret}, timeout=.3, require_https=False)
        # Tests use an in-memory pairing arrangement; prevent refresh from
        # replacing it with the production encrypted trust store.
        self.a.attach_transport(self.transport_a); self.b.attach_transport(self.transport_b)
        self.a.refresh_transport_peers = lambda: None
        self.b.refresh_transport_peers = lambda: None
        self.peers_a = PeerService(self.a, self.transport_a)
        self.peers_b = PeerService(self.b, self.transport_b)

    def tearDown(self):
        for service in (getattr(self, "peers_a", None), getattr(self, "peers_b", None)):
            if service: service.stop()
        for server in (getattr(self, "server_a", None), getattr(self, "server_b", None)):
            if server:
                try: server.stop()
                except Exception: pass
        for agent in (getattr(self, "a", None), getattr(self, "b", None)):
            if agent:
                try: agent.shutdown()
                except Exception: pass
        self.temp.cleanup()

    def _write_config(self, name):
        runtime = self.roots[name]
        project = runtime / "project"
        project.mkdir()
        script = project / "service.py"
        script.write_text("import time\nwhile True: time.sleep(.1)\n", encoding="utf-8")
        app = {
            "id": "service", "name": "Service", "runner_path": str(script), "cwd": str(project),
            "args": [], "env": {}, "interactive": False, "visible_console": False,
            "auto_start": True, "protected": True, "service_group": "service", "dependencies": [],
            "health_check": {"type": "process", "timeout_seconds": 1},
            "sync": {"enabled": True, "status": "out_of_date", "exclude": []},
            "persistence": {"strategy": "stateless", "paths": []},
            "deployments": {self.ids[name]: {"supported": True, "ready": True, "cwd": str(project), "runner_path": str(script)}},
            # This host may have hundreds of processes to reconcile.  Keep
            # the integration test realistic (rather than timing out faster
            # than a production lease would) while still exercising expiry.
            "startup_timeout_seconds": 10,
        }
        atomic_json_write(runtime / "apps.json", {"schema_version": 2, "apps": [app]})
        config = ClusterConfig(
            enabled=True, cluster_id="test", node_id=self.ids[name],
            preferred_primary_node_id=self.ids["a"], heartbeat_interval_seconds=.15,
            suspect_after_seconds=1.0, lease_ttl_seconds=4.0, lease_renew_seconds=.75,
            fence_margin_seconds=.5, automatic_failover=True, automatic_failback=True,
        )
        ConfigStore(runtime).save_cluster(config)

    def test_network_failover_rejoin_failback_and_manual_transfer(self):
        self.peers_a.start(); self.peers_b.start()
        self.assertTrue(wait_for(lambda: self.a.role == NodeRole.ACTIVE), self.a.last_transition_reason)
        self.assertTrue(wait_for(lambda: self.b.peer_status.get(self.ids["a"], {}).get("applications", {}).get("service", {}).get("status") == "Running"))
        self.assertTrue(wait_for(lambda: getattr(self.b.sync_status.get("service"), "value", "") == "synced"), self.b.sync_status)
        self.assertFalse(self.b.status()["applications"][0]["local_owner"])

        # Simulate an Agent/network loss while its protected child remains.
        self.peers_a.stop(); self.server_a.stop(); self.a.ownership.stop()
        self.assertTrue(wait_for(lambda: self.b.role == NodeRole.ACTIVE, timeout=10), self.b.last_transition_reason)
        current = self.witness.current("service")
        self.assertEqual(self.ids["b"], current.owner_node_id)
        self.assertFalse(self.a.ownership.authorized("service"))

        # A fresh Agent process returns. It must remain standby until the
        # coordinated failback releases B's lease and obtains a newer epoch.
        self.a.shutdown(stop_applications=False)
        self.a = RunnerAgent(self.roots["a"], self.witness)
        self.a.cluster.nodes = [{"node_id": self.ids["b"], "endpoint": self.url_b}]
        ConfigStore(self.roots["a"]).save_cluster(self.a.cluster)
        self.a.start_configured()
        self.server_a = RemoteAgentServer(self.a, "127.0.0.1", 0, {self.ids["b"]: self.secret}, None, None, allow_insecure_test=True)
        self.server_a.start()
        self.url_a = f"http://127.0.0.1:{self.server_a.server.server_address[1]}"
        self.b.cluster.nodes = [{"node_id": self.ids["a"], "endpoint": self.url_a}]
        self.transport_b.endpoints[self.ids["a"]] = self.url_a
        transport_a2 = TailscaleTransport(self.ids["a"], {self.ids["b"]: self.url_b}, {self.ids["b"]: self.secret}, timeout=.3, require_https=False)
        self.a.refresh_transport_peers = lambda: None
        self.peers_a = PeerService(self.a, transport_a2)
        self.peers_a.start()
        self.assertTrue(wait_for(lambda: self.a.role == NodeRole.ACTIVE, timeout=10), self.a.last_transition_reason)
        self.assertEqual(self.ids["a"], self.witness.current("service").owner_node_id)
        self.assertEqual(NodeRole.STANDBY, self.b.role)

        # Manual Transfer Here uses the same handoff protocol.
        ok, reason = self.b.transfer_here(["service"], self.ids["a"])
        self.assertTrue(ok, reason)
        self.assertEqual(self.ids["b"], self.witness.current("service").owner_node_id)


if __name__ == "__main__":
    unittest.main()
