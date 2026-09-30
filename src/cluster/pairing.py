from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from .secrets_store import SecretStore


@dataclass(slots=True)
class PairingOffer:
    code: str
    expires_at: float
    node_id: str
    fingerprint: str


class PairingManager:
    """Single-use human pairing codes; reaching the agent port alone grants no trust."""

    def __init__(self, node_id: str, fingerprint: str, store: SecretStore, clock=time.time) -> None:
        self.node_id = node_id
        self.fingerprint = fingerprint
        self.store = store
        self.clock = clock
        self._offers: dict[str, PairingOffer] = {}
        self._lock = threading.Lock()

    def create_offer(self, ttl_seconds: int = 300) -> PairingOffer:
        code = "-".join(f"{secrets.randbelow(1000):03d}" for _ in range(3))
        offer = PairingOffer(code, self.clock() + ttl_seconds, self.node_id, self.fingerprint)
        with self._lock:
            self._offers[hashlib.sha256(code.encode()).hexdigest()] = offer
        return offer

    def accept(self, code: str, remote_node_id: str, remote_fingerprint: str) -> str:
        key = hashlib.sha256(code.encode()).hexdigest()
        with self._lock:
            offer = self._offers.pop(key, None)
        if offer is None or offer.expires_at <= self.clock():
            raise PermissionError("Pairing code is invalid, expired, or already used")
        shared = secrets.token_bytes(32)
        encoded = base64.urlsafe_b64encode(shared).decode("ascii")
        trust = dict(self.store.get("trusted_nodes", {}))
        trust[remote_node_id] = {"fingerprint": remote_fingerprint, "shared_secret": encoded}
        self.store.set("trusted_nodes", trust)
        return encoded
