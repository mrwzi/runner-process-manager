from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .models import Lease


class LeaseConflict(RuntimeError):
    pass


class LeaseStore:
    """Authoritative witness-side lease store.

    Expiry uses the witness clock. BEGIN IMMEDIATE serializes acquire/renew/release,
    and every new ownership grant increments an epoch that is never reused.
    """

    def __init__(self, path: Path, clock=time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS lease_epochs (
                    group_id TEXT PRIMARY KEY,
                    last_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    group_id TEXT PRIMARY KEY,
                    owner_node_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL UNIQUE,
                    epoch INTEGER NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
        finally:
            db.close()

    def acquire(self, group_id: str, node_id: str, ttl: float) -> Lease:
        if ttl <= 0:
            raise ValueError("Lease TTL must be positive")
        with self._transaction() as db:
            now = float(self.clock())
            row = db.execute(
                "SELECT owner_node_id, lease_id, epoch, expires_at FROM leases WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if row and float(row[3]) > now:
                if row[0] == node_id:
                    raise LeaseConflict("Owner must renew its existing lease instead of reacquiring it")
                raise LeaseConflict(f"Lease is owned by {row[0]} until witness time {row[3]}")
            previous = db.execute(
                "SELECT last_epoch FROM lease_epochs WHERE group_id=?", (group_id,)
            ).fetchone()
            epoch = (int(previous[0]) if previous else 0) + 1
            lease_id = str(uuid.uuid4())
            expires_at = now + ttl
            db.execute(
                "INSERT INTO lease_epochs(group_id,last_epoch) VALUES(?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET last_epoch=excluded.last_epoch",
                (group_id, epoch),
            )
            db.execute(
                "INSERT INTO leases(group_id,owner_node_id,lease_id,epoch,expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET owner_node_id=excluded.owner_node_id, "
                "lease_id=excluded.lease_id, epoch=excluded.epoch, expires_at=excluded.expires_at",
                (group_id, node_id, lease_id, epoch, expires_at),
            )
            return Lease(group_id, node_id, lease_id, epoch, expires_at)

    def renew(self, lease: Lease, ttl: float) -> Lease:
        with self._transaction() as db:
            now = float(self.clock())
            row = db.execute(
                "SELECT owner_node_id,lease_id,epoch,expires_at FROM leases WHERE group_id=?",
                (lease.group_id,),
            ).fetchone()
            if not row or row[0] != lease.owner_node_id or row[1] != lease.lease_id or int(row[2]) != lease.epoch:
                raise LeaseConflict("Lease identity or fencing epoch is stale")
            if float(row[3]) <= now:
                raise LeaseConflict("Lease has expired and must not be renewed")
            expires_at = now + ttl
            db.execute("UPDATE leases SET expires_at=? WHERE group_id=?", (expires_at, lease.group_id))
            return Lease(lease.group_id, lease.owner_node_id, lease.lease_id, lease.epoch, expires_at)

    def release(self, lease: Lease) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM leases WHERE group_id=? AND owner_node_id=? AND lease_id=? AND epoch=?",
                (lease.group_id, lease.owner_node_id, lease.lease_id, lease.epoch),
            )
            return cursor.rowcount == 1

    def get(self, group_id: str) -> Lease | None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT owner_node_id,lease_id,epoch,expires_at FROM leases WHERE group_id=?",
                (group_id,),
            ).fetchone()
        finally:
            db.close()
        return Lease(group_id, row[0], row[1], int(row[2]), float(row[3])) if row else None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _transaction(self):
        store = self

        class Transaction:
            def __enter__(self):
                store._lock.acquire()
                self.db = store._connect()
                self.db.execute("BEGIN IMMEDIATE")
                return self.db

            def __exit__(self, exc_type, exc, traceback):
                try:
                    self.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    self.db.close()
                    store._lock.release()
                return False

        return Transaction()
