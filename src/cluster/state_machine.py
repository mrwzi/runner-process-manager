from __future__ import annotations

from dataclasses import dataclass, field

from .models import FailoverState


class InvalidTransition(RuntimeError):
    pass


_ALLOWED = {
    FailoverState.PRIMARY_HEALTHY: {FailoverState.PRIMARY_SUSPECT},
    FailoverState.PRIMARY_SUSPECT: {FailoverState.PRIMARY_HEALTHY, FailoverState.WAIT_FOR_LEASE, FailoverState.ERROR},
    FailoverState.WAIT_FOR_LEASE: {FailoverState.PRIMARY_HEALTHY, FailoverState.VERIFY_BACKUP_READY, FailoverState.ERROR},
    FailoverState.VERIFY_BACKUP_READY: {FailoverState.START_DEPENDENCIES, FailoverState.ERROR},
    FailoverState.START_DEPENDENCIES: {FailoverState.START_APPLICATIONS, FailoverState.ERROR},
    FailoverState.START_APPLICATIONS: {FailoverState.VERIFY_HEALTH, FailoverState.ERROR},
    FailoverState.VERIFY_HEALTH: {FailoverState.BACKUP_ACTIVE, FailoverState.ERROR},
    FailoverState.BACKUP_ACTIVE: {FailoverState.RESYNC_PRIMARY, FailoverState.ERROR},
    FailoverState.RESYNC_PRIMARY: {FailoverState.READY_FOR_HANDOFF, FailoverState.BACKUP_ACTIVE, FailoverState.ERROR},
    FailoverState.READY_FOR_HANDOFF: {FailoverState.HANDING_BACK, FailoverState.BACKUP_ACTIVE, FailoverState.ERROR},
    FailoverState.HANDING_BACK: {FailoverState.PRIMARY_HEALTHY, FailoverState.BACKUP_ACTIVE, FailoverState.ERROR},
    FailoverState.ERROR: {FailoverState.PRIMARY_HEALTHY, FailoverState.PRIMARY_SUSPECT, FailoverState.BACKUP_ACTIVE},
}


@dataclass(slots=True)
class FailoverMachine:
    state: FailoverState = FailoverState.PRIMARY_HEALTHY
    reason: str = ""
    history: list[tuple[FailoverState, str]] = field(default_factory=list)

    def transition(self, target: FailoverState, reason: str) -> None:
        if target not in _ALLOWED[self.state]:
            raise InvalidTransition(f"Invalid failover transition {self.state} -> {target}")
        self.state = target
        self.reason = reason
        self.history.append((target, reason))
