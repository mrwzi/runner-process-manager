from __future__ import annotations

import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import psutil

from .models import HealthState


@dataclass(slots=True)
class HealthResult:
    state: HealthState
    reason: str
    latency_ms: float | None = None


class HealthChecker:
    def check(self, config: dict[str, Any], pid: int | None = None) -> HealthResult:
        kind = str(config.get("type", "process"))
        timeout = float(config.get("timeout_seconds", 5))
        start = time.monotonic()
        try:
            if kind == "process":
                healthy = bool(pid and psutil.pid_exists(pid) and psutil.Process(pid).is_running())
                reason = "Process is running" if healthy else "Process is not running"
            elif kind == "tcp":
                with socket.create_connection((str(config["host"]), int(config["port"])), timeout=timeout):
                    pass
                healthy, reason = True, "TCP connection succeeded"
            elif kind in {"http", "https"}:
                request = urllib.request.Request(str(config["url"]), method="GET")
                context = ssl.create_default_context()
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    actual = response.status
                expected = int(config.get("expected_status", 200))
                healthy = actual == expected
                reason = f"HTTP status {actual}; expected {expected}"
            else:
                return HealthResult(HealthState.UNKNOWN, f"Unsupported health check type: {kind}")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return HealthResult(HealthState.UNHEALTHY, str(exc), (time.monotonic() - start) * 1000)
        return HealthResult(HealthState.HEALTHY if healthy else HealthState.UNHEALTHY, reason, (time.monotonic() - start) * 1000)
