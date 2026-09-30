from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict

from .lease import LeaseStore
from .models import Lease
from .security import sign_request


class Coordinator(ABC):
    @abstractmethod
    def acquire(self, group_id: str, node_id: str, ttl: float) -> Lease: ...

    @abstractmethod
    def renew(self, lease: Lease, ttl: float) -> Lease: ...

    @abstractmethod
    def release(self, lease: Lease) -> bool: ...

    @abstractmethod
    def current(self, group_id: str) -> Lease | None: ...


class LocalCoordinator(Coordinator):
    """Test/single-witness adapter. Cluster deployments use HttpCoordinator."""

    def __init__(self, store: LeaseStore) -> None:
        self.store = store

    def acquire(self, group_id: str, node_id: str, ttl: float) -> Lease:
        return self.store.acquire(group_id, node_id, ttl)

    def renew(self, lease: Lease, ttl: float) -> Lease:
        return self.store.renew(lease, ttl)

    def release(self, lease: Lease) -> bool:
        return self.store.release(lease)

    def current(self, group_id: str) -> Lease | None:
        return self.store.get(group_id)


class HttpCoordinator(Coordinator):
    """Authenticated witness client intended for a Tailscale-reachable HTTPS URL."""

    def __init__(self, base_url: str, node_id: str, shared_secret: bytes, timeout: float = 5.0) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Remote witness URL must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.shared_secret = shared_secret
        self.timeout = timeout

    def acquire(self, group_id: str, node_id: str, ttl: float) -> Lease:
        return Lease(**self._request("POST", "/v1/leases/acquire", {"group_id": group_id, "node_id": node_id, "ttl": ttl}))

    def renew(self, lease: Lease, ttl: float) -> Lease:
        return Lease(**self._request("POST", "/v1/leases/renew", {**asdict(lease), "ttl": ttl}))

    def release(self, lease: Lease) -> bool:
        return bool(self._request("POST", "/v1/leases/release", asdict(lease))["released"])

    def current(self, group_id: str) -> Lease | None:
        result = self._request("GET", f"/v1/leases/{group_id}", None)
        return Lease(**result) if result else None

    def _request(self, method: str, path: str, payload: dict | None) -> dict | None:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        signature = sign_request(self.shared_secret, method, path, timestamp, nonce, body)
        request = urllib.request.Request(
            self.base_url + path,
            data=body if method != "GET" else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Runner-Node": self.node_id,
                "X-Runner-Time": str(timestamp),
                "X-Runner-Nonce": nonce,
                "X-Runner-Signature": signature,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
