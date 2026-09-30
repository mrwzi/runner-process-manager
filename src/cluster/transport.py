from __future__ import annotations

import json
import secrets
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .security import sign_request


class ClusterTransport(ABC):
    @abstractmethod
    def request(self, node_id: str, method: str, path: str, payload: dict | None = None) -> Any: ...


class TailscaleTransport(ClusterTransport):
    """Application protocol over HTTPS endpoints reachable on a Tailscale tailnet."""

    def __init__(self, local_node_id: str, endpoints: dict[str, str], trust: dict[str, bytes], timeout: float = 5.0, require_https: bool = True) -> None:
        self.local_node_id = local_node_id
        self.endpoints = endpoints
        self.trust = trust
        self.timeout = timeout
        self.require_https = require_https

    def request(self, node_id: str, method: str, path: str, payload: dict | None = None) -> Any:
        endpoint = self.endpoints[node_id].rstrip("/")
        if self.require_https and not endpoint.startswith("https://"):
            raise ValueError("Agent endpoints must use HTTPS; use Tailscale identity plus TLS")
        method = method.upper()
        # The signature must cover the exact bytes placed on the wire.  urllib
        # sends no entity body for GET requests, so signing b"{}" caused every
        # authenticated GET to be rejected by the peer.
        body = b"" if method == "GET" else json.dumps(payload or {}, separators=(",", ":")).encode()
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        signature = sign_request(self.trust[node_id], method, path, timestamp, nonce, body)
        request = urllib.request.Request(
            endpoint + path,
            data=body if method != "GET" else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Runner-Node": self.local_node_id,
                "X-Runner-Time": str(timestamp),
                "X-Runner-Nonce": nonce,
                "X-Runner-Signature": signature,
                "X-Runner-Protocol": "1",
            },
        )
        # Handoffs include graceful dependency shutdown and therefore have a
        # separate bounded operation timeout from heartbeat/status traffic.
        timeout = max(self.timeout, 30.0) if path.startswith("/v1/handoff/") else self.timeout
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
