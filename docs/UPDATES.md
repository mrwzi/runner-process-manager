# Runner updates

Runner uses a signed HTTPS release manifest. The manifest declares the semantic
version, release notes, platform artifacts and SHA-256 for every artifact. Its
Ed25519 signature is verified before release notes or package metadata are
trusted; each downloaded package is SHA-256 verified before `UpdateRunner`
receives it.

This source build intentionally has no release manifest URL or public signing
key configured. Consequently, it does **not** claim that online updates are
operational. A release publisher must distribute a build with its public key
and configure an HTTPS manifest URL under **Updates** (or deploy the managed
release configuration). Never use HTTP or an unsigned manifest.

Updates back up `apps.json`, cluster settings, identity, encrypted secrets and
user settings under `config-backups` before invoking the installed package.
Those files are deliberately excluded from installers and portable archives.
On Windows, the verified Inno Setup package performs the in-place binary
upgrade, `UpdateRunner.exe` restarts the Runner Agent task, and the next Agent
startup runs the existing versioned configuration migrations.

## HA update policy

Runner never self-updates a node reported ACTIVE. If a connected ready standby
exists, update that standby first, verify it, perform the normal fenced
ownership transfer, then update the former active node. If no ready standby is
available Runner blocks the update and explains why. Updating a standby is
allowed; it does not acquire application ownership.

## Release manifest format

```json
{
  "version": "4.2.0",
  "release_notes": "…",
  "min_protocol_version": 1,
  "artifacts": {
    "windows": {"url": "https://releases.example/RunnerSetup.exe", "sha256": "<64 lowercase hex>"},
    "linux": {"url": "https://releases.example/runner-linux.tar.gz", "sha256": "<64 lowercase hex>"}
  },
  "signature": "base64 Ed25519 signature of canonical JSON without signature"
}
```

The signing key is a release-publisher trust root, not cluster pairing data.
