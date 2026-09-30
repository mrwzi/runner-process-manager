# Runner Agent on Debian 13 minimal

Install the matching architecture package:

```sh
sudo apt install ./runner-agent_4.1.0_amd64.deb
sudo runner-agent setup --role backup
```

The package installs no GUI and has no PySide6 dependency. Debian supplies
`python3`, `python3-cryptography`, and `python3-psutil`; Tailscale is a
recommended dependency because it is needed for normal remote pairing.

`runner-agent.service` runs as the locked-down `runner` system user, starts at
boot, restarts after failures, and stores all mutable state in
`/var/lib/runner`. It binds the local controller API only to loopback. After
`setup` finds Tailscale, it configures the remote TLS Agent listener on the
node's Tailscale IPv4 address rather than a public/LAN address.

Use the Windows GUI's **Servers → Add Backup Server** to generate a pairing
offer. On Debian, if confirmation is needed:

```sh
sudo runner-agent pair --endpoint https://PRIMARY.tailnet.ts.net:47473 --code CODE
```

Useful commands are `runner-agent status`, `runner-agent test-connection`,
`runner-agent repair`, and `runner-agent version`.

Upgrades preserve `/var/lib/runner`, including `identity.json`, encrypted
secrets, pairing state, configuration, logs, and deployments. Removing or
purging the package intentionally does the same; explicitly transfer
ownership, stop the Agent, and remove that directory only when retiring a
node. Windows `.bat`, `.cmd`, and `.exe` deployments remain unsupported on
Debian through the shared readiness/platform checks.

## Validation boundary

The package was built and fresh-installed in a clean Debian 13 (`trixie`)
container, including identity generation, CLI status, upgrade preservation,
and uninstall retention. The container did not use systemd as PID 1 and had
no Tailscale tailnet, so actual boot/restart, Tailscale pairing, and a physical
Windows-to-Debian failover require verification on a real Debian host.
