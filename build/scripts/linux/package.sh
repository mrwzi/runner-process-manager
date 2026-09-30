#!/usr/bin/env bash
# Build a Debian package from the headless Agent source. Run on Debian 13 or
# in the supplied Docker command; no PySide6 is included in the package.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
version="4.1.0"
arch="${RUNNER_DEB_ARCH:-amd64}"
package="runner-agent_${version}_${arch}"
# Do not stage beneath a Windows-mounted source checkout.  DrvFS commonly
# reports every file as 0777, and dpkg-deb refuses a DEBIAN directory with
# those unsafe permissions.  A native Linux staging directory also makes the
# result reproducible when this script is run from WSL.
stage="$(mktemp -d "/tmp/${package}.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

install -d "$stage/DEBIAN" "$stage/opt/runner" "$stage/usr/bin" "$stage/lib/systemd/system" "$stage/usr/share/doc/runner-agent" "$root/dist/debian" "$root/release"
cp -a "$root/src/cluster" "$root/src/manager" "$root/src/launcher" "$stage/opt/runner/"
find "$stage/opt/runner" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$stage/opt/runner" -type f -name '*.pyc' -delete
# Source checkouts on Windows/DrvFS can report every copied file as 0777.
# Normalize package payload permissions so the unprivileged service cannot
# execute or modify arbitrary Python source merely because of host mounts.
find "$stage/opt/runner" -type d -exec chmod 0755 {} +
find "$stage/opt/runner" -type f -exec chmod 0644 {} +
install -m 0755 "$root/build/scripts/linux/runner-agent" "$stage/usr/bin/runner-agent"
install -m 0644 "$root/build/scripts/linux/runner-agent.service" "$stage/lib/systemd/system/runner-agent.service"
install -m 0644 "$root/docs/OPERATIONS.md" "$stage/usr/share/doc/runner-agent/OPERATIONS.md"
install -m 0644 "$root/docs/SECURITY.md" "$stage/usr/share/doc/runner-agent/SECURITY.md"

cat > "$stage/DEBIAN/control" <<EOF
Package: runner-agent
Version: $version
Section: admin
Priority: optional
Architecture: $arch
Depends: python3 (>= 3.11), python3-cryptography, python3-psutil, adduser, systemd
Recommends: tailscale
Maintainer: Runner <support@runner.local>
Description: Runner high-availability headless Agent
 Authenticated Runner Agent for Debian minimal servers. No graphical desktop
 or PySide6 runtime is installed.
EOF
install -m 0755 "$root/build/scripts/linux/debian/postinst" "$stage/DEBIAN/postinst"
install -m 0755 "$root/build/scripts/linux/debian/prerm" "$stage/DEBIAN/prerm"
install -m 0755 "$root/build/scripts/linux/debian/postrm" "$stage/DEBIAN/postrm"
dpkg-deb --root-owner-group --build "$stage" "$root/dist/debian/${package}.deb"
cp -f "$root/dist/debian/${package}.deb" "$root/release/${package}.deb"
echo "Debian package: $root/dist/debian/${package}.deb"
