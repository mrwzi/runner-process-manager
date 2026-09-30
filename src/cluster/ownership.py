from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .coordinator import Coordinator
from .models import Lease


@dataclass(slots=True)
class LocalAuthority:
    lease: Lease
    safe_until_monotonic: float


class OwnershipController:
    """Maintains leases and self-fences before local authority can become ambiguous."""

    def __init__(
        self,
        coordinator: Coordinator,
        node_id: str,
        ttl: float = 15.0,
        renew_interval: float = 5.0,
        fence_margin: float = 2.0,
        on_fence: Callable[[str, str], None] | None = None,
        on_authority: Callable[[str, float], None] | None = None,
        monotonic=time.monotonic,
    ) -> None:
        if renew_interval + fence_margin >= ttl:
            raise ValueError("Lease renewal interval plus fencing margin must be less than TTL")
        self.coordinator = coordinator
        self.node_id = node_id
        self.ttl = ttl
        self.renew_interval = renew_interval
        self.fence_margin = fence_margin
        self.on_fence = on_fence or (lambda group, reason: None)
        self.on_authority = on_authority or (lambda group, deadline: None)
        self.monotonic = monotonic
        self._authorities: dict[str, LocalAuthority] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self, group_id: str) -> Lease:
        started = self.monotonic()
        lease = self.coordinator.acquire(group_id, self.node_id, self.ttl)
        with self._lock:
            self._authorities[group_id] = LocalAuthority(lease, started + self.ttl - self.fence_margin)
        try:
            self.on_authority(group_id, started + self.ttl - self.fence_margin)
        except Exception as exc:
            with self._lock:
                self._authorities.pop(group_id, None)
            try:
                self.coordinator.release(lease)
            finally:
                self.on_fence(group_id, f"Could not publish protected-process authorization: {exc}")
            raise
        return lease

    def adopt(self, lease: Lease) -> Lease:
        """Re-establish authority after a short Agent-only restart.

        The witness renews the exact lease id and fencing epoch; a stale or
        expired process therefore cannot be adopted merely because its PID is
        still alive.
        """
        if lease.owner_node_id != self.node_id:
            raise RuntimeError("Cannot adopt a lease owned by another node")
        started = self.monotonic()
        renewed = self.coordinator.renew(lease, self.ttl)
        with self._lock:
            self._authorities[lease.group_id] = LocalAuthority(renewed, started + self.ttl - self.fence_margin)
        try:
            self.on_authority(lease.group_id, started + self.ttl - self.fence_margin)
        except Exception:
            with self._lock:
                self._authorities.pop(lease.group_id, None)
            self.on_fence(lease.group_id, "Could not publish adopted protected-process authorization")
            raise
        return renewed

    def release(self, group_id: str) -> bool:
        with self._lock:
            authority = self._authorities.pop(group_id, None)
        return bool(authority and self.coordinator.release(authority.lease))

    def authorized(self, group_id: str) -> bool:
        with self._lock:
            authority = self._authorities.get(group_id)
            return bool(authority and self.monotonic() < authority.safe_until_monotonic)

    def lease(self, group_id: str) -> Lease | None:
        with self._lock:
            authority = self._authorities.get(group_id)
            return authority.lease if authority else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runner-lease-renewal", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.renew_interval + 1)

    def tick(self) -> None:
        with self._lock:
            groups = list(self._authorities)
        for group_id in groups:
            with self._lock:
                authority = self._authorities.get(group_id)
            if authority is None:
                continue
            started = self.monotonic()
            try:
                renewed = self.coordinator.renew(authority.lease, self.ttl)
            except Exception as exc:
                if self.monotonic() >= authority.safe_until_monotonic:
                    with self._lock:
                        self._authorities.pop(group_id, None)
                    self.on_fence(group_id, f"Lease renewal failed beyond safety deadline: {exc}")
                continue
            with self._lock:
                current = self._authorities.get(group_id)
                if current and current.lease.lease_id == renewed.lease_id and current.lease.epoch == renewed.epoch:
                    self._authorities[group_id] = LocalAuthority(renewed, started + self.ttl - self.fence_margin)
                    try:
                        self.on_authority(group_id, started + self.ttl - self.fence_margin)
                    except Exception as exc:
                        with self._lock:
                            self._authorities.pop(group_id, None)
                        self.on_fence(group_id, f"Could not refresh protected-process authorization: {exc}")

    def _loop(self) -> None:
        while not self._stop.wait(self.renew_interval):
            self.tick()
