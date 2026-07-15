# Remote Operations UI Access (SSH Tunnel)

This document describes how to open the production Operations UI from a trusted
client (for example a Mac laptop) **without exposing the UI on the public
internet**.

It is **documentation only**. It does not change server networking, add a reverse
proxy, or open additional public ports. SSH remains the authentication and
encryption layer.

---

## Why the UI stays private

The Operations UI binds to **localhost only** (`127.0.0.1`) in production:

| Setting | Default | Source |
| --- | --- | --- |
| `OPS_UI_HOST` | `127.0.0.1` | `deploy/scripts/run-ops-ui.sh`, `ops-ui` settings |
| `OPS_UI_PORT` | `5070` (prod), `5170` (dev) | `/etc/mk04/<env>/env` or defaults |

Because the process listens only on the loopback interface, it is **not**
reachable from other hosts on the network or from the public internet. That is
intentional: private-by-default, minimal operational complexity, no extra
infrastructure.

Remote operators reach the UI by **SSH local port forwarding**. The SSH session
forwards a port on the client to the UI port on the server’s loopback. Traffic
never leaves the encrypted SSH channel, and no public UI port is required.

Do **not**:

- Bind the UI to `0.0.0.0` for remote access
- Open the Ops UI port in the host firewall
- Put the UI behind a public reverse proxy or cloud tunnel for this workflow
- Disable Ops UI authentication to make remote access “easier”

---

## Prerequisites

On the **client** (Mac or other workstation):

- OpenSSH client (`ssh` is available by default on macOS)
- An SSH private key authorized on the production host (see [SSH_ACCESS.md](./SSH_ACCESS.md))
- Network path to the production host’s SSH port

On the **server**:

- SSH key authentication configured (password login disabled)
- `mk04-ops-ui.service` running (or the UI started via the supported deploy path)
- Ops UI listening on `127.0.0.1:<ops-ui-port>` (default prod port `5070`)

Confirm on the server (over an existing SSH session):

```bash
ss -ltnp | grep -E '127\.0\.0\.1:(5070|5170)' || true
# or:
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<ops-ui-port>/health
```

Replace `<ops-ui-port>` with the value of `OPS_UI_PORT` for that environment
(prod default `5070`).

---

## SSH key authentication

Use public-key authentication only. Full hardening checklist:

[SSH_ACCESS.md](./SSH_ACCESS.md)

Minimal client setup:

```bash
# Generate a key on the client if you do not already have one
ssh-keygen -t ed25519 -C "<device-name>"

# Install the public key on the server (once)
ssh-copy-id -i ~/.ssh/<key_name>.pub <ssh-user>@<machine-address>
```

Placeholders:

| Placeholder | Meaning |
| --- | --- |
| `<device-name>` | Label for this client (for key audit) |
| `<key_name>` | Private key filename under `~/.ssh/` |
| `<ssh-user>` | Non-root operator account on the server |
| `<machine-address>` | Server IP, DNS name, or private-network hostname |

Never commit private keys or real host addresses to the repository.

---

## Recommended SSH alias (automatic local port forwarding)

Add a host block to the **client** SSH config:

```text
~/.ssh/config
```

Example (placeholders only):

```sshconfig
Host mk1
    HostName <machine-address>
    User <ssh-user>
    IdentityFile ~/.ssh/<key_name>
    LocalForward <local-port> 127.0.0.1:<ops-ui-port>
    # Optional hardening / convenience:
    # IdentitiesOnly yes
    # ServerAliveInterval 60
    # ServerAliveCountMax 3
```

| Placeholder | Typical prod value | Notes |
| --- | --- | --- |
| `Host mk1` | `mk1` | Short alias used as `ssh mk1` |
| `<machine-address>` | *(your host)* | IP, DNS, or private overlay hostname |
| `<ssh-user>` | *(operator account)* | Non-root user with sudo as needed |
| `<key_name>` | *(your key file)* | Path to the private key |
| `<local-port>` | `5070` | Port on **your Mac**; may match the remote port |
| `<ops-ui-port>` | `5070` | Must match `OPS_UI_PORT` on the server |

`LocalForward` maps:

```text
client localhost:<local-port>  →  server 127.0.0.1:<ops-ui-port>
```

Connect:

```bash
ssh mk1
```

Leave that session open. While it is connected, open a browser on the client:

```text
http://localhost:<local-port>
```

Example with defaults (`<local-port>` and `<ops-ui-port>` both `5070`):

```text
http://localhost:5070
```

Sign in with the Ops UI operator password (`OPS_UI_OPERATOR_PASSWORD` on the
server). SSH authenticates the tunnel; the UI still requires its own login.

### One-off tunnel (without editing `~/.ssh/config`)

```bash
ssh -L <local-port>:127.0.0.1:<ops-ui-port> <ssh-user>@<machine-address>
```

Or with an existing alias that does not yet include `LocalForward`:

```bash
ssh -L <local-port>:127.0.0.1:<ops-ui-port> mk1
```

### Background tunnel (optional)

If you want the tunnel without an interactive shell:

```bash
ssh -fN mk1
```

(`-f` backgrounds after auth, `-N` does not run a remote command.) Stop it later
with `pkill -f 'ssh.*mk1'` or by killing the specific `ssh` PID — only on the
client, and only when you intend to close the tunnel.

---

## Verification steps

Run these after configuring the alias (or a one-off `-L` forward).

### 1. Server: UI is localhost-only

On the production host:

```bash
# Should show 127.0.0.1:<ops-ui-port>, not 0.0.0.0
ss -ltnp | grep <ops-ui-port>

# Local health probe should succeed on the server
curl -fsS http://127.0.0.1:<ops-ui-port>/health
```

Confirm the host firewall does **not** allow inbound traffic to `<ops-ui-port>`
from the public internet (SSH port only, as documented in [SSH_ACCESS.md](./SSH_ACCESS.md)).

### 2. Client: tunnel is active

On the client, with `ssh mk1` (or an equivalent `-L` session) connected:

```bash
# From the Mac — should reach the remote UI through the tunnel
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:<local-port>/health
```

A successful response (for example `200` or a redirect to `/login`) means the
forward is working.

### 3. Browser

Open:

```text
http://localhost:<local-port>
```

You should see the Operations UI login (or the shell if already authenticated).

### 4. Negative check (optional)

From another machine **without** an SSH tunnel, `http://<machine-address>:<ops-ui-port>`
must **not** load. That confirms the UI is not publicly exposed.

---

## Troubleshooting

| Symptom | Likely cause | What to check |
| --- | --- | --- |
| Browser: connection refused on `localhost:<local-port>` | No active tunnel | `ssh mk1` session open? `LocalForward` present? |
| Browser: connection refused, tunnel is up | Wrong local port | Match `<local-port>` in config and URL |
| Tunnel connects but UI fails | Ops UI not running | `systemctl status mk04-ops-ui` on the server |
| `curl` on server fails to `127.0.0.1:<ops-ui-port>` | Wrong port or bind | `OPS_UI_PORT` / `OPS_UI_HOST` in `/etc/mk04/prod/env` |
| UI bound to `0.0.0.0` | Misconfiguration | Set `OPS_UI_HOST=127.0.0.1` and restart `mk04-ops-ui` |
| `Address already in use` on client | Local port taken | Choose another `<local-port>` or stop the conflicting process |
| SSH auth fails | Key / user / host | [SSH_ACCESS.md](./SSH_ACCESS.md) checklist |
| Login page loads but credentials fail | Ops UI auth, not SSH | `OPS_UI_OPERATOR_PASSWORD` / session config on server |

Inspect the forward while connected:

```bash
# On the client — shows listening local forward (OpenSSH)
# macOS/Linux:
lsof -iTCP:<local-port> -sTCP:LISTEN
```

Server logs:

```bash
sudo journalctl -u mk04-ops-ui -n 50 --no-pager
```

---

## Security model (unchanged)

| Layer | Role |
| --- | --- |
| Host firewall | Exposes SSH only (as configured); **not** the Ops UI port |
| SSH | Authenticates the operator and encrypts the tunnel |
| `OPS_UI_HOST=127.0.0.1` | Keeps the UI off the network |
| Ops UI password / session | Application login after the tunnel is up |

This workflow does **not** replace SSH hardening, fail2ban, or private-network
options described in [SSH_ACCESS.md](./SSH_ACCESS.md). It only standardizes how
operators reach an already-private UI.

---

## Related documentation

- [SSH Access](./SSH_ACCESS.md) — keys, firewall, validation checklist
- [Operations Runbook](./RUNBOOK.md) — day-to-day SSH operations
- [Production Services](./PRODUCTION_SERVICES.md) — `mk04-ops-ui.service` inventory
- [deploy/README.md](../../deploy/README.md) — deployment layout and localhost binding
- [ops-ui/README.md](../../ops-ui/README.md) — Ops UI application notes
