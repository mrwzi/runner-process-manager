from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class UpdateDecision:
    allowed: bool
    reason: str


def can_update_node(node_id: str, active_owners: dict[str, str], healthy_standbys: dict[str, list[str]]) -> UpdateDecision:
    owned = [group for group, owner in active_owners.items() if owner == node_id]
    missing = [group for group in owned if not healthy_standbys.get(group)]
    if missing:
        return UpdateDecision(False, "Node owns protected groups without a healthy standby: " + ", ".join(missing))
    return UpdateDecision(True, "No unprotected active ownership would be interrupted")
