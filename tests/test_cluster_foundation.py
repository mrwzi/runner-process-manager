from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cluster.config import ConfigStore
from cluster.dependency import DependencyError, shutdown_order, startup_order
from cluster.security import AuthenticationError, RequestVerifier, sign_request
from cluster.sync import AtomicDeployment, build_manifest, persistence_ready, safe_destination
from cluster.heartbeat import HeartbeatMonitor
from cluster.ingress import CloudflareTunnelProvider
from cluster.models import ConnectivityState, HealthState, Heartbeat, NodeRole, SyncStatus
from cluster.update_safety import can_update_node


class ClusterFoundationTests(unittest.TestCase):
    def test_dependency_order_and_reverse_shutdown(self):
        apps = [
            {"id": "db", "dependencies": []},
            {"id": "api", "dependencies": ["db"]},
            {"id": "bot", "dependencies": ["api"]},
        ]
        self.assertEqual(["db", "api", "bot"], startup_order(apps, {"bot"}))
        self.assertEqual(["bot", "api", "db"], shutdown_order(apps, {"bot"}))

    def test_dependency_cycle_is_rejected(self):
        with self.assertRaises(DependencyError):
            startup_order([{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}])

    def test_legacy_config_is_backed_up_and_defaults_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apps.json").write_text(json.dumps({"apps": [{"id": "x", "name": "X"}]}))
            result = ConfigStore(root).migrate_apps()
            app = result["apps"][0]
            self.assertFalse(app["protected"])
            self.assertFalse(app["sync"]["enabled"])
            self.assertTrue(list((root / "config-backups").glob("*.bak")))

    def test_retired_compose_entries_are_backed_up_and_removed_without_affecting_process_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_apps = [
                {"id": f"process-{index}", "name": f"Process {index}", "app_type": "process"}
                for index in range(7)
            ] + [
                {"id": "ai-bridge", "name": "AI Bridge", "app_type": "docker_compose", "compose": {"compose_file": "docker-compose.yml"}},
                {"id": "isolated-compose", "name": "Isolated", "app_type": "docker_compose"},
            ]
            registry = root / "apps.json"
            registry.write_text(json.dumps({"schema_version": 3, "apps": source_apps}), encoding="utf-8")

            result = ConfigStore(root).migrate_apps()

            self.assertEqual([f"process-{index}" for index in range(7)], [item["id"] for item in result["apps"]])
            backups = list((root / "config-backups").glob("apps.json.*.bak"))
            self.assertEqual(1, len(backups))
            self.assertEqual(source_apps, json.loads(backups[0].read_text(encoding="utf-8"))["apps"])
            persisted = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(7, len(persisted["apps"]))

    def test_request_signatures_reject_tamper_and_replay(self):
        secret, body = b"x" * 32, b'{"ok":true}'
        signature = sign_request(secret, "POST", "/v1/test", 1000, "nonce", body)
        verifier = RequestVerifier({"a": secret}, max_skew_seconds=10_000_000_000)
        verifier.verify("a", "POST", "/v1/test", 1000, "nonce", body, signature)
        with self.assertRaises(AuthenticationError):
            verifier.verify("a", "POST", "/v1/test", 1000, "nonce", body, signature)
        bad = RequestVerifier({"a": secret}, max_skew_seconds=10_000_000_000)
        with self.assertRaises(AuthenticationError):
            bad.verify("a", "POST", "/v1/test", 1000, "nonce2", b"tampered", signature)

    def test_sync_stages_verifies_and_atomically_activates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, deployments = root / "source", root / "deployments"
            source.mkdir(); (source / "app.py").write_text("print('ok')")
            manifest = build_manifest(source)
            stage = AtomicDeployment(deployments).stage_from_directory("app", source, manifest)
            active = AtomicDeployment(deployments).activate("app", stage, manifest.version)
            self.assertEqual("print('ok')", (active / "app.py").read_text())

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): safe_destination(Path(directory), "../../outside")

    def test_live_sqlite_is_never_ready_by_plain_file_sync(self):
        ready, reason = persistence_ready({"strategy": "sqlite"})
        self.assertFalse(ready)
        self.assertIn("application-aware", reason)

    def test_heartbeat_uses_local_monotonic_time_and_rejects_old_sequence(self):
        clock = type("Clock", (), {"value": 10.0, "__call__": lambda self: self.value})()
        monitor = HeartbeatMonitor(8, monotonic=clock)
        heartbeat = Heartbeat("a", 2, -999999, "2.0", 1, "linux", NodeRole.ACTIVE, ConnectivityState.ONLINE, HealthState.HEALTHY, SyncStatus.SYNCED)
        self.assertTrue(monitor.observe(heartbeat))
        self.assertFalse(monitor.observe(heartbeat))
        clock.value = 19
        self.assertFalse(monitor.reachable("a"))

    def test_stale_ingress_epoch_is_rejected(self):
        provider = CloudflareTunnelProvider()
        provider.activate("web", "a", 4)
        with self.assertRaises(PermissionError): provider.activate("web", "b", 3)

    def test_active_node_update_is_blocked_without_standby(self):
        decision = can_update_node("a", {"payments": "a"}, {"payments": []})
        self.assertFalse(decision.allowed)


if __name__ == "__main__": unittest.main()
