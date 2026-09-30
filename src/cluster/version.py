"""Single source of truth for Runner release compatibility."""

from .models import PROTOCOL_VERSION

# Kept separate from the protocol: maintenance releases may interoperate.
RUNNER_VERSION = "4.1.0"
BUILD_NUMBER = "20260921.1"
AGENT_VERSION = RUNNER_VERSION


def display_version() -> str:
    return f"{RUNNER_VERSION} (build {BUILD_NUMBER})"
