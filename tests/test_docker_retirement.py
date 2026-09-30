from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.process_manager import ProcessManager


class DockerRetirementTests(unittest.TestCase):
    def test_legacy_compose_record_is_inert_and_never_calls_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            compose_app = {
                "id": "legacy-compose",
                "name": "Legacy Compose",
                "app_type": "docker_compose",
                "cwd": temp,
                "compose": {"compose_file": "docker-compose.yml"},
            }
            with patch("manager.deployments._run_docker_hidden", side_effect=AssertionError("Docker must not be called")):
                manager = ProcessManager([compose_app], Path(temp) / "logs", reconcile_existing=False)
                try:
                    self.assertEqual([], manager.export_apps())
                finally:
                    manager.shutdown(stop_applications=False)

    def test_new_compose_apps_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessManager([], Path(temp) / "logs", reconcile_existing=False)
            try:
                with self.assertRaisesRegex(ValueError, "Unsupported application type"):
                    manager.add_app({
                        "id": "compose",
                        "name": "Compose",
                        "app_type": "docker_compose",
                        "cwd": temp,
                        "compose": {"compose_file": "docker-compose.yml"},
                    })
            finally:
                manager.shutdown(stop_applications=False)


if __name__ == "__main__":
    unittest.main()
