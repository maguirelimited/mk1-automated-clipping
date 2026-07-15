# Secure SSH Access to Production

This document describes how to reach the production machine safely over SSH using
normal Linux practices. It is **documentation only** — it does not configure the
server for you. Use the validation checklist at the end to confirm access on a
real machine.

---

## Purpose

SSH is the transport for **remote production operations**:

- Checking service and job status
- Running maintenance and updates (`./update.sh`, config validation, smoke tests)
- Emergency recovery (restart services, inspect failed jobs, disk pressure)
- Low-level administration when the Ops UI or automation is not enough
- Secure access to the Operations UI via local port forwarding (the UI stays on
  localhost; see [REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md))

SSH is **not** the normal place for:

- Feature development
- Experimental changes against production data
- Day-to-day editing of application code

Development belongs in the repository and dev environment. SSH is for **operating**
the machine, not replacing the development workflow.

---

## User model

Production access uses a **normal non-root Linux user with sudo access**.

Example username (placeholder — use your actual operator account):

```text
maguireltd
```

Rules:

- **Do not SSH as root.**
- **Do not run the production system as root** unless a specific step explicitly requires it.
- **Use sudo only where needed** (systemd, firewall, package install, reading protected logs).

Typical pattern:

```text
ssh maguireltd@<machine-address>
sudo systemctl status <service>
```

---

## Key-based login

SSH must use **public-key authentication**, not passwords.

### Connect from a trusted client

Default key (if `~/.ssh/config` already points at the right key):

```bash
ssh maguireltd@<machine-address>
```

Explicit key file:

```bash
ssh -i ~/.ssh/<key_name> maguireltd@<machine-address>
```

Replace `<machine-address>` with the host’s IP or DNS name. Do not commit real
addresses in the repository if they are sensitive.

### Add a new trusted client device

On the **client** (laptop, workstation):

```bash
ssh-keygen -t ed25519 -C "<device-name>"
ssh-copy-id maguireltd@<machine-address>
```

Then verify:

```bash
ssh maguireltd@<machine-address>
```

Use a descriptive `-C` comment (e.g. `laptop-2026`) so keys can be audited later.
Remove old keys from `~/.ssh/authorized_keys` on the server when a device is retired.

---

## Required SSH server settings

On the **production server**, `sshd` should enforce key-only, non-root login.

Required settings:

```text
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
```

These are normally configured in:

```text
/etc/ssh/sshd_config
```

After editing, validate and reload (on the server, with an **existing** session still open):

```bash
sudo sshd -t
sudo systemctl reload sshd
```

**Important:** Open a **second** SSH session and confirm login works **before**
closing your current session. A mistake in `sshd_config` can lock you out.

This document does not prescribe a non-default SSH port. If you change the port,
document it out-of-band (password manager or private ops notes), not in the repo
with a real machine-specific value.

---

## Firewall expectations

The host firewall should be **enabled**. SSH should only be exposed as much as
necessary (public internet, specific IP allowlist, or private network — see below).

Ubuntu with UFW:

```bash
sudo ufw status
sudo ufw allow OpenSSH
sudo ufw enable
```

Review rules periodically:

```bash
sudo ufw status numbered
```

If SSH is reachable only over a private network (Tailscale, WireGuard, etc.),
restrict the public interface accordingly instead of opening SSH to the world.

---

## Brute-force protection

Even with password login disabled, SSH endpoints on the public internet see
constant connection attempts. Install and configure **fail2ban** or equivalent
rate limiting in a dedicated hardening step (not covered in this document).

At minimum, plan for:

- fail2ban (or similar) watching SSH auth logs
- Monitoring for repeated failed key attempts
- Removing unused accounts and expired authorized keys

---

## Private-network option (recommended when practical)

To avoid exposing SSH broadly on the public internet, use a private overlay:

- **Tailscale**
- **WireGuard**
- **ZeroTier**

Then connect to the machine’s private address and keep the public firewall tight.
This is **preferable when setup is easy** but **not a blocker** — hardened
public SSH with keys only is still acceptable if documented and checklist-verified.

Example pattern (placeholders):

```bash
# After joining the machine and client to the same tailnet:
ssh maguireltd@<tailscale-hostname>
```

---

## Validation checklist

Run through this on the **real production machine** after setup or any SSH/firewall change:

```text
[ ] SSH works from another device
[ ] Key login works
[ ] Password login fails
[ ] Root login fails
[ ] User can run sudo
[ ] Firewall is enabled
[ ] SSH port is allowed (or SSH is reachable only via private network as intended)
[ ] No private keys are committed to the repository
[ ] Access method is documented (who has keys, which devices, recovery path)
```

Quick manual checks (from a **second** client):

```bash
# Key login should succeed
ssh maguireltd@<machine-address> 'whoami'

# Password login should fail (if prompted, stop — fix sshd_config)
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no maguireltd@<machine-address>

# Root login should fail
ssh root@<machine-address>
```

On the server:

```bash
sudo ufw status
sudo grep -E '^(PasswordAuthentication|PermitRootLogin|PubkeyAuthentication)' /etc/ssh/sshd_config
sudo -l    # as maguireltd — confirm sudo works
```

---

## Safety warnings

- **Never commit private SSH keys** to git or any shared storage.
- **Never paste private keys** into documentation, tickets, or chat.
- **Never print secrets** in logs or operational script output.
- **Do not expose a public Operations UI.** Keep it on `127.0.0.1` and use an
  SSH tunnel ([REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md)).
- **Do not add unauthenticated remote control endpoints** (arbitrary command APIs, browser terminals, etc.).

Public keys in `authorized_keys` on the server are fine; private keys stay on client devices only.

---

## What not to use SSH for

Keep SSH sessions focused on operations:

```text
heavy feature development
manual production state edits without a record
experimental scripts against production data
processing videos interactively
bypassing config/deployment flows (use ./update.sh, config validation, and documented runbooks)
```

Prefer repo changes, dev smoke tests, and `./update.sh` / `./run.sh --check-only` before touching production.

---

## Related documentation

- [Remote UI Access](./REMOTE_UI_ACCESS.md) — SSH tunnel to the localhost-only Operations UI
- [Operations Runbook](./RUNBOOK.md) — updates, startup, smoke validation
- [Configuration README](../configuration/README.md) — config structure and environments
- [deploy/README.md](../../deploy/README.md) — deployment layout and production paths

---

## Deferred (later Remote Operations prompts)

This document does **not** implement:

- Operational scripts (`status.sh`, remote restart helpers, upload kill switch)
- fail2ban installation automation
- Tailscale/WireGuard setup automation
- Remote admin API or browser terminal

Those are separate hardening and Remote Administration tasks.
