from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from cluster.config import ConfigStore
from launcher.run import complete_first_run_setup
from ui.setup_wizard import SetupWizard


class _ResultWizard:
    def __init__(self, _runtime_root: Path, result, mode="standalone"):
        self.result = result
        self.mode = mode
    def exec(self): return self.result
    def selected_mode(self): return self.mode


class SetupWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wizard(self, root: Path, mode: str) -> SetupWizard:
        wizard = SetupWizard(root)
        getattr(wizard, mode).setChecked(True)
        return wizard

    def _wait_for_result(self, wizard: SetupWizard, expected: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while wizard.result() != expected and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertEqual(wizard.result(), expected)

    def test_regression_finish_uses_qdialog_dialogcode(self):
        accepted, mode = complete_first_run_setup(
            self.app, Path("unused"),
            lambda root: _ResultWizard(root, QDialog.DialogCode.Accepted, "primary"),
        )
        self.assertTrue(accepted)
        self.assertEqual(mode, "primary")

    def test_primary_finish_preserves_existing_apps_and_starts_agent_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = {"apps": [{"id": f"project-{number}", "name": f"Project {number}"} for number in range(7)]}
            apps_path = root / "apps.json"
            apps_path.write_text(json.dumps(apps), encoding="utf-8")
            wizard = self._wizard(root, "primary")
            with patch.object(wizard.controller, "start_agent_task") as start_agent, \
                patch.object(wizard.controller, "local_agent_health", return_value=(True, "healthy")):
                wizard.accept()
                self._wait_for_result(wizard, QDialog.DialogCode.Accepted)
            self.assertEqual(wizard.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(json.loads(apps_path.read_text(encoding="utf-8")), apps)
            self.assertTrue(ConfigStore(root).load_cluster().enabled)
            self.assertTrue((root / "onboarding.json").exists())
            start_agent.assert_called_once()

    def test_backup_and_standalone_finish(self):
        for mode, enabled in (("backup", True), ("standalone", False)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                wizard = self._wizard(Path(temporary), mode)
                with patch.object(wizard.controller, "start_agent_task") as start_agent, \
                     patch.object(wizard.controller, "local_agent_health", return_value=(True, "healthy")):
                    wizard.accept()
                    self._wait_for_result(wizard, QDialog.DialogCode.Accepted)
                self.assertEqual(wizard.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(ConfigStore(Path(temporary)).load_cluster().enabled, enabled)
                self.assertEqual(start_agent.called, mode != "standalone")

    def test_failed_agent_provisioning_does_not_commit_cluster_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wizard = self._wizard(root, "primary")
            with patch.object(wizard.controller, "start_agent_task"), \
                 patch.object(wizard.controller, "local_agent_health", return_value=(False, "no API")), \
                 patch("ui.setup_wizard.QMessageBox.warning"):
                wizard.accept()
                deadline = time.monotonic() + 5
                while wizard._finishing and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.app.processEvents()
            self.assertNotEqual(wizard.result(), QDialog.DialogCode.Accepted)
            self.assertFalse(ConfigStore(root).load_cluster().enabled)
            self.assertFalse((root / "onboarding.json").exists())
            wizard.reject()

    def test_back_cancel_and_window_close_return_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            wizard = self._wizard(Path(temporary), "primary")
            wizard.show()
            self.app.processEvents()
            wizard.next()
            self.assertEqual(wizard.currentId(), 1)
            wizard.back()
            self.assertEqual(wizard.currentId(), 0)
            wizard.reject()
            self.assertEqual(wizard.result(), QDialog.DialogCode.Rejected)
        with tempfile.TemporaryDirectory() as temporary:
            wizard = self._wizard(Path(temporary), "primary")
            wizard.close()
            self.assertEqual(wizard.result(), QDialog.DialogCode.Rejected)


if __name__ == "__main__":
    unittest.main()
