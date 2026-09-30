#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
install -d -m 0755 /opt/runner
install -m 0755 "${1:-./Runner}" /opt/runner/Runner
id runner >/dev/null 2>&1 || useradd --system --home /var/lib/runner --shell /usr/sbin/nologin runner
install -d -o runner -g runner -m 0700 /var/lib/runner
install -m 0644 "$(dirname "$0")/runner-agent.service" /etc/systemd/system/runner-agent.service
systemctl daemon-reload
systemctl enable --now runner-agent.service
