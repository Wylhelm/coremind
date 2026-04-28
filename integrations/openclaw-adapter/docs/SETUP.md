# OpenClaw Adapter — Setup Guide

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Users running both CoreMind and OpenClaw who want them to cooperate.

---

## 1. Prerequisites

- CoreMind daemon ≥ 0.0.1 (Phase 1 complete)
- OpenClaw gateway ≥ 0.5.0
- Both daemons reachable from the same host (Unix socket) or on the same VLAN (TLS TCP)

## 2. Install the OpenClaw half

```bash
cd $OPENCLAW_HOME
openclaw plugins install @coremind/openclaw-adapter
```

This drops the extension into `OPENCLAW_HOME/plugins/coremind-adapter/` and
adds its manifest to OpenClaw's plugin index. Approve the prompted
permissions; narrow them if you do not want the adapter to reach every
channel.

## 3. Install the CoreMind half

```bash
pip install -e integrations/openclaw-adapter/coremind_side
coremind plugin register coremind-plugin-openclaw-adapter
```

Registration creates the plugin's ed25519 keypair under
`~/.coremind/keys/plugins/coremind_plugin_openclaw_adapter.ed25519` (mode
600) and records the public key in the plugin index.

## 3.5 Provision the shared signing key on the OpenClaw side

**This is the single trust boundary of the adapter.** The CoreMind half
verifies every inbound `WorldEvent` against the ed25519 **public** key of
the plugin's keypair. The OpenClaw half needs the corresponding **private**
key to sign events before forwarding them. The two halves MUST hold
material derived from the same seed, or every ingest will be rejected as
`UNAUTHENTICATED`.

Supported provisioning schemes (pick one):

### 3.5.a Same host, shared filesystem (default)

Both daemons run as the same Unix user. The OpenClaw extension reads the
raw seed directly from the CoreMind key file:

```text
~/.coremind/keys/plugins/coremind_plugin_openclaw_adapter.ed25519
```

The TS `signer.ts` helper `loadPrivateKeyFromSeed(buffer)` accepts the raw
32-byte seed extracted from this PEM. This is the simplest scheme and the
one the adapter assumes when no other is configured.

### 3.5.b Explicit export (cross-host installs)

Run once on the CoreMind host:

```bash
coremind plugin export-key coremind.plugin.openclaw_adapter \
    --out /secure/transfer/openclaw-adapter.seed.bin
```

Ship the 32-byte seed file to the OpenClaw host via your existing secrets
channel (OpenClaw secrets store, SOPS, Vault, etc.) and reference it from
the OpenClaw plugin config:

```json
{ "signerSeedPath": "/run/secrets/openclaw-adapter.seed.bin" }
```

### 3.5.c OpenClaw-side generation + public-key import

If you prefer the private key to live exclusively on the OpenClaw host,
generate it there and import the **public** key into CoreMind:

```bash
openclaw plugins exec @coremind/openclaw-adapter generate-key \
    --out ~/openclaw-adapter.pub
coremind plugin import-pubkey coremind.plugin.openclaw_adapter \
    --from ~/openclaw-adapter.pub
```

In this scheme the CoreMind key file is a **public-only** stub. Rotation
requires re-running both commands.

Rotate keys by re-running the chosen scheme. The adapter detects a key
mismatch at the first ingested event and logs
`daemon_forwarder.signature_mismatch` before rejecting with
`UNAUTHENTICATED`.

## 4. Configure

Create `~/.coremind/plugins/openclaw_adapter.toml`:

```toml
# Location of the CoreMind daemon's plugin-host Unix socket.
daemon_socket = "~/.coremind/run/plugin_host.sock"

# Address this plugin listens on for the OpenClaw extension to push events to.
# Defaults live under ~/.coremind/run (chmod 700) — avoid /tmp.
coremind_half_address = "unix://~/.coremind/run/openclaw-half.sock"

# Address this plugin's CoreMindPlugin gRPC server listens on (daemon → plugin).
plugin_grpc_address  = "unix://~/.coremind/run/openclaw-plugin.sock"

# Address of the OpenClaw extension's OpenClawHalf gRPC server.
openclaw_address = "unix://~/.coremind/run/openclaw-adapter.sock"

# Permission narrowing (globs). Use ["*"] for no restriction.
allowed_channels = ["telegram"]
allowed_skills   = ["weather.*", "briefing.*"]
cron_manage      = true
```

On the OpenClaw side, the extension by default binds sockets under
`$XDG_RUNTIME_DIR/openclaw/` (or `~/.openclaw/run/` if XDG_RUNTIME_DIR is
unset). The offline event queue lives at
`~/.openclaw/state/coremind-adapter/events.jsonl` and is **the single
source of offline buffering** — the CoreMind side does not double-buffer.

## 5. Verify

```bash
coremind plugin status coremind.plugin.openclaw_adapter
# Expected: state=OK, events_processed>0 once OpenClaw sends anything
```

Send yourself a test message on a configured channel and confirm the event
shows up in `coremind logs tail | grep message_received`.

## 6. Troubleshooting

**Unix socket permission denied.** Both daemons must run as the same user,
or the socket's parent directory must be group-readable.

**Adapter in degraded mode.** The CoreMind side is running but cannot reach
OpenClaw. Events are still ingested from other plugins; outbound actions
queue in memory and fail fast after the configured timeout. Check
`openclaw_adapter.tools.degraded` log events.

**Signature verification failed.** The plugin's keypair was rotated without
re-registering. Re-run `coremind plugin register` to re-bind the new public
key.

**Skill rejected with PERMISSION_DENIED.** Your `allowed_skills` scope does
not cover the requested skill name. Widen the glob or route the action to
an allowed skill.

## 7. Stopping

```bash
coremind plugin disable coremind.plugin.openclaw_adapter
openclaw plugins disable @coremind/openclaw-adapter
```

Disabling is reversible; both halves remember their registration state.
