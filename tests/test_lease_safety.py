from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from cluster.coordinator import LocalCoordinator
from cluster.lease import LeaseConflict, LeaseStore
from cluster.ownership import OwnershipController


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None: self.value = value
    def __call__(self) -> float: return self.value


class LeaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = LeaseStore(Path(self.temp.name) / "witness.db", clock=self.clock)

    def tearDown(self): self.temp.cleanup()

    def test_only_one_node_can_acquire(self):
        lease = self.store.acquire("payments", "node-a", 15)
        with self.assertRaises(LeaseConflict):
            self.store.acquire("payments", "node-b", 15)
        self.assertEqual("node-a", self.store.get("payments").owner_node_id)

    def test_concurrent_acquisition_has_one_winner(self):
        winners, barrier = [], threading.Barrier(2)
        def acquire(node):
            barrier.wait()
            try: winners.append(self.store.acquire("bot", node, 15).owner_node_id)
            except LeaseConflict: pass
        threads = [threading.Thread(target=acquire, args=(node,)) for node in ("a", "b")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1, len(winners))

    def test_expiry_advances_epoch_and_stale_owner_cannot_renew_or_release(self):
        old = self.store.acquire("trade", "a", 10)
        self.clock.value += 11
        new = self.store.acquire("trade", "b", 10)
        self.assertGreater(new.epoch, old.epoch)
        with self.assertRaises(LeaseConflict): self.store.renew(old, 10)
        self.assertFalse(self.store.release(old))
        self.assertEqual("b", self.store.get("trade").owner_node_id)

    def test_expired_lease_cannot_be_renewed_even_without_takeover(self):
        lease = self.store.acquire("bot", "a", 5)
        self.clock.value += 6
        with self.assertRaises(LeaseConflict): self.store.renew(lease, 5)

    def test_self_fences_after_monotonic_deadline(self):
        monotonic = FakeClock(50)
        fenced = []
        controller = OwnershipController(LocalCoordinator(self.store), "a", ttl=10, renew_interval=3, fence_margin=2, on_fence=lambda g, r: fenced.append(g), monotonic=monotonic)
        controller.acquire("bot")
        self.clock.value += 11
        monotonic.value += 9
        controller.tick()
        self.assertFalse(controller.authorized("bot"))
        self.assertEqual(["bot"], fenced)

    def test_agent_restart_can_adopt_only_current_witness_lease(self):
        first = OwnershipController(LocalCoordinator(self.store), "a", ttl=10, renew_interval=3, fence_margin=2, monotonic=FakeClock(10))
        lease = first.acquire("payments")
        restarted = OwnershipController(LocalCoordinator(self.store), "a", ttl=10, renew_interval=3, fence_margin=2, monotonic=FakeClock(20))
        renewed = restarted.adopt(lease)
        self.assertEqual(lease.epoch, renewed.epoch)
        self.assertTrue(restarted.authorized("payments"))
        self.clock.value += 11
        other = self.store.acquire("payments", "b", 10)
        with self.assertRaises(LeaseConflict):
            restarted.adopt(renewed)
        self.assertEqual(other.owner_node_id, "b")


if __name__ == "__main__": unittest.main()
