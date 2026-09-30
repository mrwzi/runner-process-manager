from __future__ import annotations

import time
from dataclasses import dataclass

from .models import Heartbeat, PROTOCOL_VERSION


@dataclass(slots=True)
class PeerObservation:
    heartbeat: Heartbeat
    received_monotonic: float


class HeartbeatMonitor:
    """Uses local monotonic receipt time; remote wall-clock skew cannot trigger failover."""

    def __init__(self, suspect_after_seconds: float = 8.0, monotonic=time.monotonic) -> None:
        self.suspect_after_seconds = suspect_after_seconds
        self.monotonic = monotonic
        self.peers: dict[str, PeerObservation] = {}

    def observe(self, heartbeat: Heartbeat) -> bool:
        if heartbeat.protocol_version != PROTOCOL_VERSION:
            raise ValueError(
                f"Incompatible Runner protocol {heartbeat.protocol_version}; local protocol is {PROTOCOL_VERSION}"
            )
        previous = self.peers.get(heartbeat.node_id)
        if previous and heartbeat.sequence <= previous.heartbeat.sequence:
            return False
        self.peers[heartbeat.node_id] = PeerObservation(heartbeat, self.monotonic())
        return True

    def reachable(self, node_id: str) -> bool:
        observation = self.peers.get(node_id)
        return bool(observation and self.monotonic() - observation.received_monotonic <= self.suspect_after_seconds)

    def age(self, node_id: str) -> float | None:
        observation = self.peers.get(node_id)
        return self.monotonic() - observation.received_monotonic if observation else None
