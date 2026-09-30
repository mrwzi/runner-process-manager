from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .security import redact_secrets


class AuditLog:
    def __init__(self, path: Path, node_id: str) -> None:
        self.path = path
        self.node_id = node_id
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = redact_secrets({
            "timestamp": time.time(),
            "node_id": self.node_id,
            "event": event,
            **fields,
        })
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
