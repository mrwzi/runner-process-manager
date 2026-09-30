from __future__ import annotations

import json
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .security import AuthenticationError, RequestVerifier, redact_secrets
from .bounded_http import BoundedThreadingHTTPServer


MAX_BODY = 64 * 1024 * 1024


class RemoteAgentServer:
    """Paired-node API. Public binding is refused unless TLS is configured."""

    def __init__(self, agent, host: str, port: int, trusted_secrets: dict[str, bytes], certificate: Path | None, private_key: Path | None, allow_insecure_test: bool = False) -> None:
        if not allow_insecure_test and (not certificate or not private_key):
            raise ValueError("Remote Agent serving requires a TLS certificate and private key")
        self.agent = agent
        self.verifier = RequestVerifier(trusted_secrets)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self._dispatch(b"")
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_BODY:
                    return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request is too large"})
                self._dispatch(self.rfile.read(length))

            def _dispatch(self, body: bytes):
                if self.command == "POST" and self.path == "/v1/pairing/accept":
                    try:
                        import base64
                        payload = json.loads(body)
                        result = outer.agent.accept_pairing(payload)
                        outer.verifier.trusted_secrets[str(payload["node_id"])] = base64.urlsafe_b64decode(result["shared_secret"])
                        return self._send(HTTPStatus.OK, result)
                    except Exception as exc:
                        return self._send(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                try:
                    node_id = self.headers.get("X-Runner-Node", "")
                    outer.verifier.verify(node_id, self.command, self.path, int(self.headers.get("X-Runner-Time", "0")), self.headers.get("X-Runner-Nonce", ""), body, self.headers.get("X-Runner-Signature", ""))
                    if self.headers.get("X-Runner-Protocol") != "1":
                        raise AuthenticationError("Incompatible or missing protocol version")
                except (AuthenticationError, ValueError) as exc:
                    return self._send(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                try:
                    payload = json.loads(body or b"{}")
                    if self.command == "GET" and self.path == "/v1/status":
                        try:
                            result = outer.agent.status(refresh_readiness=False)
                        except TypeError:
                            # Small protocol fakes/older Agent implementations
                            # may expose only status().
                            result = outer.agent.status()
                    elif self.command == "GET" and self.path.startswith("/v1/logs/"):
                        app_id = self.path.removeprefix("/v1/logs/")
                        result = {"node_id": outer.agent.cluster.node_id, "app_id": app_id, "text": outer.agent.manager.get_log_cache_text(app_id)}
                    elif self.path == "/v1/heartbeat":
                        result = outer.agent.receive_heartbeat(payload)
                    elif self.path == "/v1/handoff/prepare":
                        result = outer.agent.prepare_handoff([str(v) for v in payload["app_ids"]], str(payload["target_node_id"]))
                    elif self.path == "/v1/handoff/release":
                        ok, reason = outer.agent.transfer_out([str(v) for v in payload["app_ids"]])
                        result = {"success": ok, "reason": reason}
                    elif self.path == "/v1/apps/stop":
                        ok, reason = outer.agent.cluster_stop([str(v) for v in payload["app_ids"]])
                        result = {"success": ok, "reason": reason}
                    elif self.path == "/v1/apps/restart":
                        ok, reason = outer.agent.cluster_restart([str(v) for v in payload["app_ids"]])
                        result = {"success": ok, "reason": reason}
                    elif self.path == "/v1/apps/force-stop":
                        ok, reason = outer.agent.cluster_force_stop([str(v) for v in payload["app_ids"]])
                        result = {"success": ok, "reason": reason}
                    elif self.path == "/v1/sync/manifest":
                        result = outer.agent.sync_manifest(str(payload["app_id"]))
                    elif self.path == "/v1/sync/push":
                        result = outer.agent.receive_sync(payload)
                    else:
                        return self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
                    self._send(HTTPStatus.OK, redact_secrets(result))
                except Exception as exc:
                    self._send(HTTPStatus.CONFLICT, {"error": str(exc)})

            def _send(self, status, value):
                encoded = json.dumps(value, separators=(",", ":"), default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args): return

        self.server = BoundedThreadingHTTPServer((host, port), Handler)
        if certificate and private_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate, private_key)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = None

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, name="runner-remote-api", daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)
