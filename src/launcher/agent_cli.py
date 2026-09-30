"""Headless Runner Agent command line for Debian/systemd deployments."""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cluster.agent import RunnerAgent
from cluster.api import AgentApiServer
from cluster.config import ConfigStore
from cluster.models import AgentMode
from cluster.onboarding import OnboardingController
from cluster.secrets_store import SecretStore
from cluster.tailscale import available_peers, ensure_certificate, local_identity
from cluster.transport import TailscaleTransport
from cluster.version import AGENT_VERSION, BUILD_NUMBER, RUNNER_VERSION
from cluster.models import PROTOCOL_VERSION

DEFAULT_RUNTIME = Path("/var/lib/runner")


def _runtime(value: str | None) -> Path:
    return Path(value or os.environ.get("RUNNER_RUNTIME_ROOT", DEFAULT_RUNTIME)).expanduser()


def tailscale_status() -> dict[str, object]:
    try:
        identity = local_identity()
        return {"installed": True, "connected": identity.online, "hostname": identity.hostname,
                "ipv4": identity.ipv4, "ipv6": identity.ipv6,
                "peers": [peer.hostname for peer in available_peers() if peer.online]}
    except Exception as exc:
        return {"installed": False, "connected": False, "reason": str(exc), "peers": []}


def _remote_components(agent: RunnerAgent):
    """Start paired HTTPS service only after Tailscale certificate validation."""
    remote_api = peer_service = None
    if not agent.cluster.enabled:
        return remote_api, peer_service
    from cluster.peer_service import PeerService
    from cluster.remote_api import RemoteAgentServer
    trusted_records = SecretStore(agent.runtime_root).get("trusted_nodes", {})
    trusted = {node_id: base64.urlsafe_b64decode(value["shared_secret"])
               for node_id, value in trusted_records.items()}
    endpoints = {str(node["node_id"]): str(node["endpoint"]) for node in agent.cluster.nodes}
    try:
        if not agent.cluster.tls_certificate or not agent.cluster.tls_private_key:
            identity = local_identity()
            certificate, private_key = ensure_certificate(agent.runtime_root, identity.endpoint_host)
            agent.cluster.tls_certificate, agent.cluster.tls_private_key = str(certificate), str(private_key)
            agent.store.save_cluster(agent.cluster)
        remote_api = RemoteAgentServer(agent, agent.cluster.remote_host, agent.cluster.remote_port, trusted,
                                       Path(agent.cluster.tls_certificate), Path(agent.cluster.tls_private_key))
        remote_api.start()
        peer_service = PeerService(agent, TailscaleTransport(agent.cluster.node_id, endpoints, trusted))
        peer_service.start()
    except Exception as exc:
        agent.last_transition_reason = f"Remote Agent API unavailable: {exc}"
        agent.audit.write("remote_api_unavailable", reason=str(exc))
    return remote_api, peer_service


def serve(runtime_root: Path) -> int:
    runtime_root.mkdir(parents=True, exist_ok=True)
    agent = RunnerAgent(runtime_root)
    api = AgentApiServer(agent)
    api.start()
    remote_api, peer_service = _remote_components(agent)
    agent.start_configured()
    stopping = False
    def stop(*_args):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        time.sleep(0.5)
    if peer_service: peer_service.stop()
    if remote_api: remote_api.stop()
    api.stop()
    # systemd restart replaces only the Agent. Applications are reconciled by
    # the replacement Agent; fencing still stops protected work if authority
    # cannot be restored before the guard deadline.
    agent.shutdown(stop_applications=False)
    return 0


def setup(runtime_root: Path, role: str) -> int:
    runtime_root.mkdir(parents=True, exist_ok=True)
    controller = OnboardingController(runtime_root)
    cluster = controller.select_mode(role)
    try:
        tailscale = local_identity()
        # Debian exposes the remote HTTPS service only on the Tailscale
        # interface. No public/LAN listener is needed for normal operation.
        if tailscale.ipv4:
            cluster.remote_host = tailscale.ipv4
            ConfigStore(runtime_root).save_cluster(cluster)
    except Exception:
        pass
    identity = RunnerAgent(runtime_root).identity
    restarted = False
    if os.name != "nt" and os.geteuid() == 0:
        restarted = subprocess.run(["systemctl", "restart", "runner-agent.service"], capture_output=True).returncode == 0
    print(json.dumps({"setup": "complete", "role": role, "node_id": identity["node_id"],
                      "tailscale": tailscale_status(), "agent_restarted": restarted}, indent=2))
    return 0


def status(runtime_root: Path) -> int:
    agent = RunnerAgent(runtime_root)
    result = agent.status()
    result["tailscale"] = tailscale_status()
    print(json.dumps(result, indent=2, default=str))
    agent.shutdown(stop_applications=False)
    return 0


def pair(runtime_root: Path, endpoint: str, code: str) -> int:
    agent = RunnerAgent(runtime_root)
    if not agent.cluster.enabled:
        raise RuntimeError("Run 'runner-agent setup --role backup' before pairing.")
    result = agent.pair_remote(endpoint, code)
    print(json.dumps({"paired": result, "tailscale": tailscale_status()}, indent=2))
    agent.shutdown(stop_applications=False)
    return 0


def test_connection(runtime_root: Path) -> int:
    agent = RunnerAgent(runtime_root)
    state = agent.status()
    peers = state.get("peers", {})
    connected = [node for node, value in peers.items() if value.get("connection") == "connected"]
    print(json.dumps({"tailscale": tailscale_status(), "trusted_peer_heartbeats": connected,
                      "protocol_version": PROTOCOL_VERSION, "agent_version": AGENT_VERSION}, indent=2))
    agent.shutdown(stop_applications=False)
    return 0 if connected else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner-agent", description="Runner headless Agent for Debian")
    parser.add_argument("--runtime-root", default=None, help="default: /var/lib/runner")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the systemd Agent service")
    setup_parser = commands.add_parser("setup", help="initialize standalone, primary, or backup role")
    setup_parser.add_argument("--role", choices=("standalone", "primary", "backup"), default="backup")
    pair_parser = commands.add_parser("pair", help="pair using code/endpoint shown by the Windows Runner")
    pair_parser.add_argument("--endpoint", required=True)
    pair_parser.add_argument("--code", required=True)
    commands.add_parser("status", help="show Agent and Tailscale status")
    commands.add_parser("test-connection", help="verify authenticated paired heartbeat")
    commands.add_parser("repair", help="restart the systemd Agent")
    commands.add_parser("version", help="show version information")
    args = parser.parse_args(argv)
    runtime_root = _runtime(args.runtime_root)
    try:
        if args.command == "serve": return serve(runtime_root)
        if args.command == "setup": return setup(runtime_root, args.role)
        if args.command == "status": return status(runtime_root)
        if args.command == "pair": return pair(runtime_root, args.endpoint, args.code)
        if args.command == "test-connection": return test_connection(runtime_root)
        if args.command == "repair":
            os.execvp("systemctl", ["systemctl", "restart", "runner-agent.service"])
        print(json.dumps({"runner_version": RUNNER_VERSION, "agent_version": AGENT_VERSION,
                          "protocol_version": PROTOCOL_VERSION, "build": BUILD_NUMBER}, indent=2))
        return 0
    except Exception as exc:
        print(f"runner-agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
