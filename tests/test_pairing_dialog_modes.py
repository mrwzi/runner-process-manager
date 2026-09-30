from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.app import PairingDialog


class _Manager:
    def agent_status(self):
        return {
            "mode": "standalone",
            "cluster_name": "Test",
            "node_name": "test-node",
            "node_id": "test-id",
            "peers": {},
            "leases": {},
            "automatic_failover": False,
            "automatic_failback": False,
            "automatic_failover_eligible": False,
            "automatic_failover_reasons": ["Cluster mode is not enabled"],
        }

    def create_pairing_offer(self):
        return {"code": "123-456-789"}


class PairingDialogModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, setup_mode: bool) -> PairingDialog:
        peers_patch = patch("cluster.tailscale.available_peers", return_value=[])
        peers_patch.start()
        self.addCleanup(peers_patch.stop)
        return PairingDialog(_Manager(), setup_mode=setup_mode)

    def test_finish_setup_is_a_focused_view_and_can_expand_witness_setup(self):
        dialog = self._dialog(setup_mode=True)
        self.assertEqual(dialog.windowTitle(), "Finish HA Setup")
        self.assertTrue(dialog.summary.isHidden())
        self.assertTrue(dialog.remove_button.isHidden())
        self.assertTrue(dialog.failover.isHidden())
        self.assertTrue(dialog.failback.isHidden())
        self.assertTrue(dialog.witness_button.isHidden())

        dialog.setup_advanced_toggle.setChecked(True)
        self.assertFalse(dialog.witness_button.isHidden())
        self.assertFalse(dialog.witness_url.isHidden())
        self.assertFalse(dialog.witness_secret.isHidden())

    def test_servers_remains_the_full_management_view(self):
        dialog = self._dialog(setup_mode=False)
        self.assertEqual(dialog.windowTitle(), "Cluster / Servers")
        self.assertFalse(dialog.summary.isHidden())
        self.assertFalse(dialog.remove_button.isHidden())
        self.assertFalse(dialog.failover.isHidden())
        self.assertFalse(dialog.failback.isHidden())
        self.assertTrue(dialog.setup_advanced_toggle.isHidden())


if __name__ == "__main__":
    unittest.main()
