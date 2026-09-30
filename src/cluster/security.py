from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .config import atomic_json_write


class AuthenticationError(RuntimeError):
    pass


class IdentityStore:
    """Persistent node identity and symmetric request signing material.

    The private key never travels in heartbeats or logs. A deployment may replace
    this backend with an OS keyring/HSM without changing the protocol layer.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / "identity.json"

    def load_or_create(self) -> dict[str, str]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        identity = {
            "node_id": str(uuid.uuid4()),
            "private_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        }
        identity["public_fingerprint"] = hashlib.sha256(identity["private_key"].encode()).hexdigest()
        atomic_json_write(self.path, identity)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return identity


def sign_request(secret: bytes, method: str, path: str, timestamp: int, nonce: str, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    message = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{digest}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class RequestVerifier:
    def __init__(self, trusted_secrets: dict[str, bytes], max_skew_seconds: int = 60) -> None:
        self.trusted_secrets = trusted_secrets
        self.max_skew_seconds = max_skew_seconds
        self._nonces: dict[tuple[str, str], float] = {}

    def verify(self, node_id: str, method: str, path: str, timestamp: int, nonce: str, body: bytes, signature: str) -> None:
        now = time.time()
        if abs(now - timestamp) > self.max_skew_seconds:
            raise AuthenticationError("Request timestamp is outside the accepted window")
        key = (node_id, nonce)
        self._nonces = {item: expiry for item, expiry in self._nonces.items() if expiry > now}
        if key in self._nonces:
            raise AuthenticationError("Replay nonce was already used")
        secret = self.trusted_secrets.get(node_id)
        if secret is None:
            raise AuthenticationError("Node is not paired")
        expected = sign_request(secret, method, path, timestamp, nonce, body)
        if not hmac.compare_digest(expected, signature):
            raise AuthenticationError("Invalid request signature")
        self._nonces[key] = now + self.max_skew_seconds


def redact_secrets(value: Any) -> Any:
    secret_words = ("secret", "token", "password", "api_key", "private_key", "credential")
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(word in key.lower() for word in secret_words) else redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
