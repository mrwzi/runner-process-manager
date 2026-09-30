from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


PROTOCOL_VERSION = 1
CONFIG_SCHEMA_VERSION = 3


class AgentMode(StrEnum):
    STANDALONE = "standalone"
    CLUSTER = "cluster"


class NodeRole(StrEnum):
    ACTIVE = "active"
    STANDBY = "standby"
    TAKING_OVER = "taking_over"
    HANDING_BACK = "handing_back"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ConnectivityState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class SyncStatus(StrEnum):
    SYNCED = "synced"
    SYNCING = "syncing"
    OUT_OF_DATE = "out_of_date"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"


class FailoverState(StrEnum):
    PRIMARY_HEALTHY = "primary_healthy"
    PRIMARY_SUSPECT = "primary_suspect"
    WAIT_FOR_LEASE = "wait_for_lease"
    VERIFY_BACKUP_READY = "verify_backup_ready"
    START_DEPENDENCIES = "start_dependencies"
    START_APPLICATIONS = "start_applications"
    VERIFY_HEALTH = "verify_health"
    BACKUP_ACTIVE = "backup_active"
    RESYNC_PRIMARY = "resync_primary"
    READY_FOR_HANDOFF = "ready_for_handoff"
    HANDING_BACK = "handing_back"
    ERROR = "error"


class PersistenceStrategy(StrEnum):
    STATELESS = "stateless"
    EXTERNAL = "external"
    SQLITE = "sqlite"
    REPLICATED = "replicated"
    CUSTOM = "custom"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class Lease:
    group_id: str
    owner_node_id: str
    lease_id: str
    epoch: int
    expires_at: float

    def valid_for(self, node_id: str, now: float, safety_margin: float = 0.0) -> bool:
        return self.owner_node_id == node_id and now + safety_margin < self.expires_at


@dataclass(slots=True)
class NodeRecord:
    node_id: str
    name: str
    hostname: str
    os_name: str
    agent_version: str
    protocol_version: int = PROTOCOL_VERSION
    preferred_primary: bool = False
    endpoint: str = ""
    public_key: str = ""
    tailscale_ipv4: str = ""
    tailscale_ipv6: str = ""


@dataclass(slots=True)
class ClusterConfig:
    enabled: bool = False
    cluster_id: str = ""
    cluster_name: str = "Production"
    node_id: str = ""
    preferred_primary_node_id: str = ""
    witness_url: str = ""
    transport: str = "tailscale"
    heartbeat_interval_seconds: float = 2.0
    suspect_after_seconds: float = 8.0
    lease_ttl_seconds: float = 15.0
    lease_renew_seconds: float = 5.0
    fence_margin_seconds: float = 2.0
    automatic_failover: bool = False
    automatic_failback: bool = False
    remote_host: str = "0.0.0.0"
    remote_port: int = 47473
    tls_certificate: str = ""
    tls_private_key: str = ""
    node_name: str = ""
    nodes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mode(self) -> AgentMode:
        return AgentMode.CLUSTER if self.enabled else AgentMode.STANDALONE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Readiness:
    ready: bool
    reasons: list[str] = field(default_factory=list)
    sync_status: SyncStatus = SyncStatus.NOT_CONFIGURED
    checked_at: float = 0.0


@dataclass(slots=True)
class Heartbeat:
    node_id: str
    sequence: int
    sent_at: float
    agent_version: str
    protocol_version: int
    os_name: str
    role: NodeRole
    connectivity: ConnectivityState
    health: HealthState
    sync_status: SyncStatus
    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    applications: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("role", "connectivity", "health", "sync_status"):
            value[key] = str(value[key])
        return value
