from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import atomic_json_write


class SecretStore:
    """Encrypted-at-rest secret storage with a replaceable key-provider boundary."""

    def __init__(self, root: Path, key: bytes | None = None) -> None:
        self.root = root
        self.path = root / "secrets.enc"
        self.key_path = root / "secrets.key"
        self._key = key or self._load_or_create_key()
        self._fernet = Fernet(self._key)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            plaintext = self._fernet.decrypt(self.path.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError("Runner secret store cannot be decrypted with this node key") from exc
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Secret store root must be an object")
        return value

    def save(self, values: dict[str, Any]) -> None:
        encrypted = self._fernet.encrypt(json.dumps(values, separators=(",", ":"), sort_keys=True).encode())
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)
        self._restrict(self.path)

    def get(self, name: str, default: Any = None) -> Any:
        return self.load().get(name, default)

    def set(self, name: str, value: Any) -> None:
        values = self.load()
        values[name] = value
        self.save(values)

    def _load_or_create_key(self) -> bytes:
        environment_key = os.environ.get("RUNNER_SECRETS_KEY")
        if environment_key:
            return environment_key.encode("ascii")
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        self._restrict(self.key_path)
        return key

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
