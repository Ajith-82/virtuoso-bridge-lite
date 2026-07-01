# Connecting Claude Code to a Virtuoso session on HPC (from your laptop)

Drive a Virtuoso session running on a remote compute node — through a login /
gateway host — from Claude Code on your laptop. Everything you type runs
locally; the bridge tunnels SKILL to the remote CIW.

```
   Laptop (you + Claude Code + this repo)
        │  virtuoso-bridge start   →  ssh -J <jump> <remote>, forward localPort → 127.0.0.1:daemonPort
        ▼
   Login / gateway host              ← VB_JUMP_HOST   (ProxyJump, built automatically)
        ▼
   Compute node running Virtuoso     ← VB_REMOTE_HOST
        ├─ ramic_bridge daemon  (deployed by `start`)
        └─ Virtuoso CIW  ── paste one load("…virtuoso_setup.il") line once
```

See `AGENTS.md` for the full CLI/API reference; this file is the HPC-specific
quickstart.

## Two rules that trip people up on HPC

1. **Use your SSH *aliases*, not raw IPs/ports.** The bridge does **not** pass
   `-p`; it relies on `~/.ssh/config` for `HostName`, `User`, and `Port`. If your
   hosts use a non-standard SSH port, put the **alias** in the bridge config so
   that port is inherited. A raw IP would default to port 22 and fail.
2. **`VB_REMOTE_PORT` / `VB_LOCAL_PORT` are the *bridge daemon* port — not the SSH
   port.** They're auto-derived by hashing your remote username; leave them
   alone. Don't set them to your SSH port.

## Prerequisites

- **Passwordless SSH all the way to the compute node.** The tool runs `ssh` with
  `BatchMode=yes` and will never prompt. Prove it first:
  ```bash
  ssh -J <jump-alias> <remote-alias> echo ok      # must print ok, no prompt
  ```
- **A running Virtuoso with a CIW** on the compute node (typically via VNC/X). The
  bridge attaches to an existing session; it does not launch Virtuoso.
- **This repo installed locally:**
  ```bash
  uv venv .venv && source .venv/bin/activate
  uv pip install -e .
  ```

## Worked example — tailored to this machine's `~/.ssh/config`

Your `~/.ssh/config` defines (user `ajithkv`, port `5148`, `ForwardAgent yes`):

| Alias          | HostName        | Reachable from | Role in this setup            |
|----------------|-----------------|----------------|-------------------------------|
| `server01_ext` | 90.213.214.193  | laptop (public)| gateway / jump host           |
| `server01`     | 192.168.0.113   | internal LAN   | internal node                 |
| `server02`     | 192.168.0.186   | internal LAN   | internal node                 |

Pick the scenario that matches **where Virtuoso actually runs.**

### Scenario A — Virtuoso on an internal node, reached via the gateway

Virtuoso on `server02` (internal), tunnelled through `server01_ext` (public):

```bash
virtuoso-bridge init ajithkv@server02 -J ajithkv@server01_ext
```
`~/.virtuoso-bridge/.env`:
```dotenv
VB_REMOTE_HOST=server02              # alias → 192.168.0.186:5148, user ajithkv
VB_REMOTE_USER=ajithkv
VB_JUMP_HOST=server01_ext            # alias → 90.213.214.193:5148 (public gateway)
# VB_REMOTE_PORT / VB_LOCAL_PORT: leave unset (auto — bridge daemon port, not SSH)
```
The bridge issues `ssh -J ajithkv@server01_ext ajithkv@server02`; both aliases are
resolved from `~/.ssh/config`, so port `5148` is applied to each hop
automatically.

### Scenario B — Virtuoso on the gateway host itself (no jump)

If Virtuoso runs on the directly-reachable host (`server01_ext`), skip the jump:

```bash
virtuoso-bridge init ajithkv@server01_ext
```
```dotenv
VB_REMOTE_HOST=server01_ext          # the machine running Virtuoso
VB_REMOTE_USER=ajithkv
# no VB_JUMP_HOST
```

## Start, load, verify

```bash
virtuoso-bridge start          # opens the tunnel + deploys the daemon
# → prints:  load("/tmp/virtuoso_bridge_<user>/<client>/virtuoso_bridge/virtuoso_setup.il")
```
Paste that `load("…")` line into the **Virtuoso CIW** once (add it to the remote
`~/.cdsinit` to auto-load on every start). Then:
```bash
virtuoso-bridge status         # [tunnel] running  [daemon] OK  [spectre] OK/NOT FOUND
virtuoso-bridge eval "1+2"     # → 3
```

## Scheduler-allocated compute nodes (SLURM / LSF)

If the compute node is handed out by a scheduler and its hostname changes per
job, either:

- **Update the target each allocation:** start Virtuoso on the allocated node,
  note `$HOSTNAME`, then `virtuoso-bridge init --force ajithkv@<node> -J ajithkv@<login>`
  and `virtuoso-bridge restart`; or
- **Keep a stable alias** in `~/.ssh/config` (e.g. `Host vnode` with
  `ProxyJump server01_ext`) and only edit its `HostName` when the node changes —
  then `VB_REMOTE_HOST=vnode` never moves.

(If your site allows Virtuoso on the login node, Scenario B is simplest — but many
sites forbid EDA on login nodes.)

## Using it from Claude Code

Run Claude Code on your laptop **inside this repo**. Once `status` is green, the
bridge is Claude Code's hands into the remote Virtuoso — it drives it through the
same API/CLI, plus the bundled `skills/virtuoso`, `skills/spectre`,
`skills/optimizer`:

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()     # reads .env, uses the tunnel
client.execute_skill("1+2")            # runs on the HPC CIW
with client.schematic.edit() as s:
    ...                                # schematic / layout / maestro helpers
```

SKILL always goes **through** the bridge (`client.execute_skill` /
`virtuoso-bridge eval`) — never SSH in and run SKILL by hand.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[daemon] NO RESPONSE` | `load("…")` not pasted into the CIW, or Virtuoso not running on that node. Re-run `status` to reprint the line. |
| Tunnel won't start / auth fails | `ssh -J <jump> <remote> echo ok` must work with **no prompt** — `BatchMode=yes` gives no password fallback. Fix keys/agent first. |
| Connects to wrong host | `VB_REMOTE_HOST` = the node running Virtuoso; `VB_JUMP_HOST` = the gateway. Don't set remote host to the gateway. |
| "Connection refused" on the SSH hop | You used a raw IP instead of the alias, so it tried port 22 — use the `~/.ssh/config` alias so port `5148` is applied. |
| 15–30 s stalls on connect | Usually GSSAPI/Kerberos or a slow gateway; the bridge already disables GSSAPI and allows longer jump-host settle time, so first connects are just slow, not broken. |
| Spectre `NOT FOUND` | Independent from the SKILL bridge. Set `VB_CADENCE_CSHRC` if `spectre` isn't on the remote `PATH` (see `AGENTS.md` → "How Spectre is located"). |
