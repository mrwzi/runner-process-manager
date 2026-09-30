from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cluster.config import ConfigStore
from cluster.onboarding import OnboardingController
from cluster.security import IdentityStore


class OnboardingTests(unittest.TestCase):
    def test_separate_installations_create_distinct_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = OnboardingController(root / "first")
            second = OnboardingController(root / "second")
            first.select_mode("primary")
            second.select_mode("backup")
            self.assertNotEqual(
                IdentityStore(root / "first").load_or_create()["node_id"],
                IdentityStore(root / "second").load_or_create()["node_id"],
            )

    def test_primary_setup_enables_cluster_without_auto_failover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = OnboardingController(root)
            config = controller.select_mode("primary")
            self.assertTrue(config.enabled)
            self.assertEqual(config.node_id, config.preferred_primary_node_id)
            self.assertFalse(config.automatic_failover)
            self.assertTrue(controller.complete())
            self.assertTrue(ConfigStore(root).load_cluster().enabled)


if __name__ == "__main__":
    unittest.main()
