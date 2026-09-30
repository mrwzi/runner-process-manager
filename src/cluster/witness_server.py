from __future__ import annotations

import argparse
import base64
import json
import ssl
import secrets
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .lease import LeaseConflict, LeaseStore
from .models import Lease
from .security import AuthenticationError, RequestVerifier
from .secrets_store import SecretStore


def serve(runtime_root: Path, host: str, port: int, certificate: Path, private_key: Path) -> None:
    secrets_store = SecretStore(runtime_root)
    trusted = {
        node_id: base64.urlsafe_b64decode(record["shared_secret"])
        for node_id, record in secrets_store.get("trusted_nodes", {}).items()
    }
    verifier = RequestVerifier(trusted)
    leases = LeaseStore(runtime_root / "witness.db")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self._verify(b""):
                return
            prefix = "/v1/leases/"
            if not self.path.startswith(prefix):
                return self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
            lease = leases.get(self.path.removeprefix(prefix))
            self._send(HTTPStatus.OK, asdict(lease) if lease else None)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if not self._verify(body):
                return
            try:
                payload = json.loads(body)
                if self.path == "/v1/leases/acquire":
                    result = asdict(leases.acquire(str(payload["group_id"]), str(payload["node_id"]), float(payload["ttl"])))
                elif self.path == "/v1/leases/renew":
                    ttl = float(payload.pop("ttl"))
                    result = asdict(leases.renew(Lease(**payload), ttl))
                elif self.path == "/v1/leases/release":
                    result = {"released": leases.release(Lease(**payload))}
                else:
                    return self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
                self._send(HTTPStatus.OK, result)
            except LeaseConflict as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (KeyError, TypeError, ValueError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _verify(self, body: bytes) -> bool:
            try:
                verifier.verify(
                    self.headers.get("X-Runner-Node", ""), self.command, self.path,
                    int(self.headers.get("X-Runner-Time", "0")), self.headers.get("X-Runner-Nonce", ""),
                    body, self.headers.get("X-Runner-Signature", ""),
                )
                return True
            except (AuthenticationError, ValueError) as exc:
                self._send(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return False

        def _send(self, status: HTTPStatus, value) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runner authoritative lease witness")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=47472)
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--initialize-node", metavar="NODE_ID", help="Register a node and print its one-time shared secret")
    args = parser.parse_args(argv)
    if args.initialize_node:
        secret = secrets.token_bytes(32)
        encoded = base64.urlsafe_b64encode(secret).decode("ascii")
        store = SecretStore(args.runtime_root)
        trusted = dict(store.get("trusted_nodes", {}))
        trusted[str(args.initialize_node)] = {"shared_secret": encoded}
        store.set("trusted_nodes", trusted)
        print(encoded)
        return 0
    if not args.certificate or not args.private_key:
        parser.error("--certificate and --private-key are required when serving")
    serve(args.runtime_root, args.host, args.port, args.certificate, args.private_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
