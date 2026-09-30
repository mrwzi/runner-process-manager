from __future__ import annotations

import json
import logging
import logging.handlers
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .agent import RunnerAgent
from .config import atomic_json_write
from .models import PROTOCOL_VERSION
from .version import AGENT_VERSION
from .bounded_http import BoundedThreadingHTTPServer


class _AgentHTTPServer(BoundedThreadingHTTPServer):
    # A local Agent is a singleton. Do not allow two --agent launches to share
    # the authenticated API listener through SO_REUSEADDR.
    allow_reuse_address = False


class AgentApiServer:
    """Versioned localhost control API. Remote exposure requires TLS + paired HMAC auth."""

    def __init__(self, agent: RunnerAgent, host: str = "127.0.0.1", port: int = 47471) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.started_at = time.time()
        self.process_record = agent.runtime_root / "agent-process.json"
        self.logger = self._configure_diagnostics(agent.runtime_root)
        self._status_lock = threading.Lock()
        self._status_cache: dict[str, Any] | None = None
        self._status_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self.token = self._load_token(agent.runtime_root)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RunnerAgent/2"

            def handle_one_request(self):
                started = time.monotonic()
                try:
                    return super().handle_one_request()
                finally:
                    elapsed_ms = (time.monotonic() - started) * 1000
                    if elapsed_ms >= 500:
                        outer.logger.warning(
                            "slow local API request path=%s elapsed_ms=%.0f pid=%s bind=%s:%s",
                            str(getattr(self, "path", "<request-parse>" )).split("?", 1)[0], elapsed_ms, os.getpid(), outer.host, outer.port,
                        )

            def do_GET(self):
                if not self._authenticated():
                    return
                try:
                    if self.path == "/v1/health":
                        self._send(HTTPStatus.OK, {
                            "ok": True,
                            "node_id": outer.agent.cluster.node_id,
                            "agent_version": AGENT_VERSION,
                            "protocol_version": PROTOCOL_VERSION,
                            "pid": os.getpid(),
                            "started_at": outer.started_at,
                        })
                    elif self.path == "/v1/status":
                        status = outer._cached_status()
                        if status is None:
                            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Agent status is initializing"})
                        else:
                            self._send(HTTPStatus.OK, status)
                    elif self.path == "/v1/apps":
                        # Readiness may invoke Docker/Compose and filesystem
                        # checks. Serve a conservative cached view so API polls
                        # never wait on those operations.
                        status = outer._cached_status()
                        if status is None:
                            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Agent status is initializing"})
                        else:
                            self._send(HTTPStatus.OK, {"applications": status["applications"], "cluster": status})
                    elif self.path == "/v1/cluster/config":
                        self._send(HTTPStatus.OK, outer.agent.cluster.to_dict())
                    elif self.path.startswith("/v1/logs/"):
                        app_id = self.path.removeprefix("/v1/logs/")
                        try:
                            self._send(HTTPStatus.OK, outer.agent.remote_logs(app_id))
                        except KeyError:
                            self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown application"})
                    else:
                        self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
                except Exception as exc:
                    outer.logger.error("local API request failed path=%s error_type=%s", self.path.split("?", 1)[0], type(exc).__name__)
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Agent request failed; see Agent diagnostics"})

            def do_POST(self):
                if not self._authenticated():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    app_ids = [str(value) for value in payload.get("app_ids", [])]
                    if self.path == "/v1/ownership/acquire-and-start":
                        success, reason = outer.agent.acquire_and_start(app_ids)
                    elif self.path == "/v1/cluster/config":
                        allowed = {"automatic_failover", "automatic_failback", "preferred_primary_node_id", "heartbeat_interval_seconds", "suspect_after_seconds"}
                        with outer.agent.config_lock:
                            for key in allowed:
                                if key in payload:
                                    setattr(outer.agent.cluster, key, payload[key])
                            outer.agent.store.save_cluster(outer.agent.cluster)
                        success, reason = True, "Cluster configuration updated"
                    elif self.path == "/v1/ownership/transfer-here":
                        success, reason = outer.agent.transfer_here(app_ids, payload.get("source_node_id"))
                    elif self.path == "/v1/pairing/offer":
                        self._send(HTTPStatus.OK, outer.agent.pairing_offer())
                        return
                    elif self.path == "/v1/pairing/connect":
                        self._send(HTTPStatus.OK, outer.agent.pair_remote(str(payload["endpoint"]), str(payload["code"])))
                        return
                    elif self.path == "/v1/ownership/transfer-out":
                        success, reason = outer.agent.transfer_out(app_ids)
                    elif self.path == "/v1/apps/start":
                        for app_id in app_ids:
                            outer.agent.manager.start_app(app_id)
                        success, reason = True, "Start accepted"
                    elif self.path == "/v1/apps/stop":
                        success, reason = outer.agent.cluster_stop(app_ids)
                    elif self.path == "/v1/apps/restart":
                        success, reason = outer.agent.cluster_restart(app_ids)
                    elif self.path == "/v1/pairing/remove":
                        success, reason = outer.agent.remove_peer(str(payload["node_id"]))
                    elif self.path == "/v1/witness/configure":
                        success, reason = outer.agent.configure_witness(str(payload["url"]), str(payload["shared_secret"]))
                    elif self.path == "/v1/apps/force-stop":
                        success, reason = outer.agent.cluster_force_stop(app_ids)
                    elif self.path == "/v1/apps/input":
                        outer.agent.manager.send_input(str(payload["app_id"]), str(payload.get("text", "")))
                        success, reason = True, "Input accepted"
                    elif self.path == "/v1/apps/add":
                        with outer.agent.config_lock:
                            outer.agent.manager.add_app(dict(payload["app"]))
                            outer.agent._prepare_existing_local_deployments()
                            atomic_json_write(outer.agent.store.apps_path, {"schema_version": 2, "apps": outer.agent.manager.export_apps()})
                        success, reason = True, "Application added"
                    elif self.path == "/v1/apps/update":
                        with outer.agent.config_lock:
                            outer.agent.manager.update_app(str(payload["app_id"]), dict(payload["app"]))
                            outer.agent._prepare_existing_local_deployments()
                            atomic_json_write(outer.agent.store.apps_path, {"schema_version": 2, "apps": outer.agent.manager.export_apps()})
                        success, reason = True, "Application updated"
                    elif self.path == "/v1/apps/remove":
                        with outer.agent.config_lock:
                            outer.agent.manager.remove_app(str(payload["app_id"]))
                            atomic_json_write(outer.agent.store.apps_path, {"schema_version": 2, "apps": outer.agent.manager.export_apps()})
                        success, reason = True, "Application removed"
                    else:
                        self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
                        return
                    self._send(HTTPStatus.OK if success else HTTPStatus.CONFLICT, {"success": success, "reason": reason})
                except (ValueError, KeyError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:
                    # An unexpected handler exception otherwise closes the
                    # socket without an HTTP response, making the GUI report a
                    # misleading read timeout. Keep diagnostics secret-safe.
                    outer.logger.error("local API request failed path=%s error_type=%s", self.path.split("?", 1)[0], type(exc).__name__)
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Agent request failed; see Agent diagnostics"})

            def _authenticated(self) -> bool:
                supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if secrets.compare_digest(supplied, outer.token):
                    return True
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
                return False

            def _send(self, status: HTTPStatus, value: Any) -> None:
                body = json.dumps(value, separators=(",", ":"), default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                outer.logger.info("agent-api " + format, *args)

        self.server = _AgentHTTPServer((host, port), Handler)
        self.thread: threading.Thread | None = None

    @staticmethod
    def _configure_diagnostics(runtime_root: Path) -> logging.Logger:
        logger = logging.getLogger(f"runner.agent.api.{abs(hash(str(runtime_root.resolve())))}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if logger.handlers:
            return logger
        try:
            directory = runtime_root / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                directory / "agent-api.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
        return logger

    def start(self) -> None:
        self._status_stop.clear()
        self._status_thread = threading.Thread(target=self._refresh_status_cache, name="runner-api-status-cache", daemon=True)
        self._status_thread.start()
        self.thread = threading.Thread(target=self.server.serve_forever, name="runner-agent-api", daemon=True)
        self.thread.start()
        try:
            import psutil
            process_started_at = psutil.Process(os.getpid()).create_time()
        except Exception:
            process_started_at = self.started_at
        atomic_json_write(self.process_record, {
            "pid": os.getpid(), "started_at": self.started_at,
            "process_started_at": process_started_at,
            "host": self.host, "port": self.port,
        })

    def stop(self) -> None:
        self._status_stop.set()
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)
        if self._status_thread:
            self._status_thread.join(timeout=2)
        for handler in list(self.logger.handlers):
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)
        try:
            current = json.loads(self.process_record.read_text(encoding="utf-8"))
            if int(current.get("pid", -1)) == os.getpid():
                self.process_record.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _cached_status(self) -> dict[str, Any] | None:
        with self._status_lock:
            return self._status_cache

    def _refresh_status_cache(self) -> None:
        while not self._status_stop.is_set():
            started = time.monotonic()
            try:
                status = self.agent.status(refresh_readiness=False)
                with self._status_lock:
                    self._status_cache = status
                elapsed_ms = (time.monotonic() - started) * 1000
                if elapsed_ms >= 500:
                    self.logger.warning("slow status snapshot refresh elapsed_ms=%.0f pid=%s", elapsed_ms, os.getpid())
            except Exception as exc:
                self.logger.error("status snapshot refresh failed error_type=%s", type(exc).__name__)
            self._status_stop.wait(1.0)

    @staticmethod
    def _load_token(runtime_root: Path) -> str:
        path = runtime_root / "agent-api.json"
        if path.exists():
            return str(json.loads(path.read_text(encoding="utf-8"))["token"])
        token = secrets.token_urlsafe(32)
        atomic_json_write(path, {"token": token, "listen": "127.0.0.1:47471"})
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
