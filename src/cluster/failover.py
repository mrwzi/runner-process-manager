from __future__ import annotations

from dataclasses import dataclass
import time

from .agent import RunnerAgent
from .heartbeat import HeartbeatMonitor
from .models import FailoverState
from .state_machine import FailoverMachine


@dataclass(slots=True)
class FailoverDecision:
    acted: bool
    state: FailoverState
    reason: str


class FailoverController:
    """Deterministic failover coordinator; the witness remains authoritative."""

    def __init__(self, agent: RunnerAgent, monitor: HeartbeatMonitor) -> None:
        self.agent = agent
        self.monitor = monitor
        self.machine = FailoverMachine()
        self._retry_after = 0.0

    def evaluate(self, primary_node_id: str, protected_app_ids: list[str]) -> FailoverDecision:
        if self.monitor.reachable(primary_node_id):
            if self.machine.state != FailoverState.PRIMARY_HEALTHY:
                # A returning preferred primary stays standby if backup is already active.
                if self.machine.state == FailoverState.BACKUP_ACTIVE:
                    self.machine.transition(FailoverState.RESYNC_PRIMARY, "Preferred primary returned; active ownership is unchanged")
                else:
                    self.machine = FailoverMachine(reason="Primary heartbeat is healthy")
            return FailoverDecision(False, self.machine.state, self.machine.reason or "Primary heartbeat is healthy")

        if self.machine.state == FailoverState.ERROR:
            if time.monotonic() < self._retry_after:
                return FailoverDecision(False, self.machine.state, self.machine.reason)
            self.machine.transition(FailoverState.PRIMARY_SUSPECT, "Retrying takeover after bounded backoff")
            return FailoverDecision(False, self.machine.state, self.machine.reason)

        if self.machine.state == FailoverState.PRIMARY_HEALTHY:
            self.machine.transition(FailoverState.PRIMARY_SUSPECT, "Heartbeat threshold exceeded")
            return FailoverDecision(False, self.machine.state, self.machine.reason)
        if self.machine.state == FailoverState.PRIMARY_SUSPECT:
            self.machine.transition(FailoverState.WAIT_FOR_LEASE, "Requesting authoritative ownership from witness")
        if self.machine.state == FailoverState.WAIT_FOR_LEASE:
            self.machine.transition(FailoverState.VERIFY_BACKUP_READY, "Witness acquisition will fence any stale owner")
            readiness = self.agent.readiness(protected_app_ids)
            if not readiness.ready:
                self.machine.transition(FailoverState.ERROR, "; ".join(readiness.reasons))
                self._retry_after = time.monotonic() + max(1.0, self.agent.cluster.heartbeat_interval_seconds * 2)
                return FailoverDecision(False, self.machine.state, self.machine.reason)
            self.machine.transition(FailoverState.START_DEPENDENCIES, "Backup readiness verified")
            self.machine.transition(FailoverState.START_APPLICATIONS, "Starting in dependency order")
            success, reason = self.agent.acquire_and_start(protected_app_ids)
            if not success:
                self.machine.transition(FailoverState.ERROR, reason)
                self._retry_after = time.monotonic() + max(1.0, self.agent.cluster.heartbeat_interval_seconds * 2)
                return FailoverDecision(False, self.machine.state, reason)
            self.machine.transition(FailoverState.VERIFY_HEALTH, "Applications started; verifying health")
            self.machine.transition(FailoverState.BACKUP_ACTIVE, reason)
            return FailoverDecision(True, self.machine.state, reason)
        return FailoverDecision(False, self.machine.state, self.machine.reason)
