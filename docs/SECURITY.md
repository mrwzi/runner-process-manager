# Security

Runner uses persistent UUID node identities, one-time pairing codes, stored peer fingerprints, per-peer random shared secrets, signed requests, timestamps, and replay nonces. Network location is never identity.

Remote control and witness requests require HTTPS plus application-layer authentication. The initial transport is Tailscale, but cluster code depends on `ClusterTransport`, not on Tailscale APIs.

Secrets are encrypted at rest with Fernet. The encryption key may come from `RUNNER_SECRETS_KEY`; otherwise it is stored in a permission-restricted node-local key file. Production operators should inject the key from the OS service secret facility or replace the key provider with an OS keyring/HSM. Secret fields are redacted from structured logs.

Release metadata is accepted only over HTTPS after Ed25519 signature
verification. Release packages are SHA-256 checked before the updater is
started. Cluster keys, identities, secrets, logs, and node runtime state are
not part of installers, portable archives, or update packages.

The synchronization layer accepts only manifest-listed relative paths beneath the approved deployment root, rejects traversal, verifies SHA-256, and activates only complete staged versions. It does not execute arbitrary synchronization metadata.

The localhost Agent API uses a random bearer token stored with restricted permissions. It is not safe to bind this API publicly. Remote Agent endpoints must use the paired HMAC protocol and TLS.

Pairing is the only path that adds a remote node to the trust map. The remote endpoint still requires TLS, a one-time pairing code, a persistent node UUID/fingerprint, and a per-node HMAC secret. Heartbeats do not grant control authority. Every protected start, restart, handoff, and ingress action is checked against the current witness lease and fencing epoch.

The process guard is intentionally independent of the Agent process. It reads only a local monotonic deadline file written atomically by the lease controller; deletion or expiry causes the child tree to be fenced. This prevents an Agent crash from leaving an indefinitely authorized protected process while another node takes a later epoch.

Docker Engine is a privileged local facility. Runner uses only the local
Docker CLI beneath an approved Compose deployment root and does not expose the
Docker socket/API through its local or remote HTTP APIs. Remote peers can ask
the Agent to perform a lease-authorized deployment transition, not provide an
arbitrary host path or Docker command. Compose logs are redacted before being
returned through Runner. Registry credentials and `.env` files are never
included in file synchronization or ordinary logs.
