from __future__ import annotations

import base64
import hashlib
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cluster.updates import ReleaseClient, SemanticVersion, UpdateError, backup_runtime, update_plan


class _Response:
    def __init__(self, body: bytes): self.body = body; self.offset = 0
    def read(self, size=-1):
        if self.offset >= len(self.body): return b""
        if size < 0: size = len(self.body) - self.offset
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class UpdateTests(unittest.TestCase):
    def test_semantic_versions(self):
        self.assertLess(SemanticVersion.parse("4.1.0-beta.1"), SemanticVersion.parse("4.1.0"))
        self.assertLess(SemanticVersion.parse("4.1.0"), SemanticVersion.parse("4.2.0"))

    def test_signed_manifest_and_corrupt_download_rejection(self):
        private = Ed25519PrivateKey.generate()
        key = base64.b64encode(private.public_key().public_bytes_raw()).decode()
        payload = {"version": "4.2.0", "release_notes": "Safer updates", "artifacts": {"windows": {"url": "https://example.test/RunnerSetup.exe", "sha256": hashlib.sha256(b"good").hexdigest()}}}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["signature"] = base64.b64encode(private.sign(canonical)).decode()
        def opener(url, timeout=0):
            return _Response(json.dumps(payload).encode()) if url.endswith("manifest.json") else _Response(b"bad")
        client = ReleaseClient(key, opener)
        release = client.fetch("https://example.test/manifest.json")
        self.assertEqual(release.version, "4.2.0")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(UpdateError):
                client.download(release.artifacts["windows"], Path(temp) / "bad.exe")

    def test_backup_preserves_existing_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("apps.json", "cluster.json", "identity.json", "secrets.enc", "secrets.key", "ui_state.json"):
                (root / name).write_text(name, encoding="utf-8")
            backup = backup_runtime(root)
            self.assertEqual((backup / "identity.json").read_text(encoding="utf-8"), "identity.json")
            self.assertEqual((root / "secrets.enc").read_text(encoding="utf-8"), "secrets.enc")

    def test_active_node_requires_rolling_update(self):
        allowed, reason = update_plan({"mode": "cluster", "role": "active", "peers": {"b": {"connection": "connected", "ready": True}}})
        self.assertFalse(allowed)
        self.assertIn("standby", reason.lower())
        allowed, _ = update_plan({"mode": "cluster", "role": "standby", "peers": {}})
        self.assertTrue(allowed)

    def test_installer_failure_does_not_attempt_a_second_locked_binary_overwrite(self):
        module_path = Path(__file__).resolve().parents[1] / "build" / "scripts" / "update_runner.py"
        spec = importlib.util.spec_from_file_location("runner_update_helper", module_path)
        helper = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, runtime = root / "install", root / "runtime"
            install_dir.mkdir()
            (install_dir / "Runner.exe").write_bytes(b"old-runner")
            (install_dir / "UpdateRunner.exe").write_bytes(b"old-updater")
            package = root / "RunnerSetup.exe"
            package.write_bytes(b"package")
            with patch.object(helper.subprocess, "run", return_value=type("R", (), {"returncode": 1})()), \
                    patch.object(helper, "restore_binaries", side_effect=AssertionError("unsafe second overwrite")):
                self.assertEqual(helper.install(package, install_dir, runtime, restart_gui=False), 1)
            self.assertEqual((install_dir / "Runner.exe").read_bytes(), b"old-runner")

    def test_successful_update_restarts_agent_task(self):
        module_path = Path(__file__).resolve().parents[1] / "build" / "scripts" / "update_runner.py"
        spec = importlib.util.spec_from_file_location("runner_update_helper_success", module_path)
        helper = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, runtime = root / "install", root / "runtime"
            install_dir.mkdir()
            (install_dir / "Runner.exe").write_bytes(b"runner")
            package = root / "RunnerSetup.exe"
            package.write_bytes(b"package")
            calls = []
            def run(command, **_kwargs):
                calls.append(command)
                return type("R", (), {"returncode": 0})()
            with patch.object(helper.subprocess, "run", side_effect=run), patch.object(helper.sys, "platform", "win32"):
                self.assertEqual(helper.install(package, install_dir, runtime, restart_gui=False), 0)
            self.assertTrue(any(command[:2] == ["schtasks", "/End"] for command in calls))
            self.assertTrue(any(command[:2] == ["schtasks", "/Run"] for command in calls))

    def test_update_front_process_copies_itself_before_returning(self):
        module_path = Path(__file__).resolve().parents[1] / "build" / "scripts" / "update_runner.py"
        spec = importlib.util.spec_from_file_location("runner_update_worker", module_path)
        helper = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "installed-updater.exe"
            source.write_bytes(b"test updater image")
            worker_dir = root / "bootstrap"
            worker_dir.mkdir()
            args = ["--package", "release.exe", "--install-dir", "C:/Runner", "--runtime-root", "C:/ProgramData/Runner_V4"]
            with patch.object(helper.sys, "executable", str(source)), \
                    patch.object(helper.tempfile, "mkdtemp", return_value=str(worker_dir)), \
                    patch.object(helper.subprocess, "Popen") as popen:
                self.assertEqual(helper.launch_detached_worker(args), 0)
            worker = worker_dir / "UpdateRunner.exe"
            self.assertEqual(worker.read_bytes(), source.read_bytes())
            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(worker))
            self.assertEqual(command[1], "--worker")
            self.assertEqual(command[2:], args)


if __name__ == "__main__":
    unittest.main()
