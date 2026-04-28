# Troubleshooting

**Version:** 0.1
**Status:** Stable
**Audience:** Operators running a CoreMind daemon who hit a problem.

For conceptual questions, see [`FAQ.md`](FAQ.md). For installing and updating, see [`RELEASE.md`](RELEASE.md) and [`DEVELOPMENT.md`](DEVELOPMENT.md).

---

## Diagnostic first steps

Before diving into specifics, gather these:

```bash
coremind --version
coremind doctor               # config, paths, key permissions, service reachability
journalctl --user -u coremind -n 200 --no-pager   # if running under systemd
docker compose ps             # SurrealDB + Qdrant status
```

`coremind doctor` prints a single-screen report: config file location, key fingerprints, socket path, database reachability, configured LLM provider, and quiet-hours state. Most issues below correspond to a specific failed check.

---

## Daemon won't start

### Symptom: `Address already in use` on the plugin socket

Another daemon (or a stale one) is bound to `~/.coremind/run/plugin_host.sock`.

```bash
lsof ~/.coremind/run/plugin_host.sock
# kill the offending PID, or:
rm ~/.coremind/run/plugin_host.sock   # only if no daemon is running
```

The daemon refuses to clobber an existing socket on purpose — it cannot tell whether a previous instance is still draining events.

### Symptom: `KeyringError` or `PermissionError` reading `~/.coremind/keys/daemon.ed25519`

The daemon enforces `chmod 600` on its private key. If the file is group- or world-readable it refuses to load it:

```bash
chmod 600 ~/.coremind/keys/daemon.ed25519
chmod 700 ~/.coremind/keys ~/.coremind/secrets
```

If the key file is missing, deleting `~/.coremind/keys/daemon.ed25519.pub` and restarting the daemon regenerates the pair. **Do this only on a fresh instance** — a new keypair invalidates previously-signed audit journal entries.

### Symptom: `ConfigError: unknown key '<x>'`

Pydantic strict mode is on for config. A typo in `~/.coremind/config.toml` is a hard error, not a warning. Check the exact key against [`DEVELOPMENT.md`](DEVELOPMENT.md) and the `coremind.config` module.

---

## Backing services

### SurrealDB unreachable

```bash
docker compose ps
docker compose logs surrealdb --tail 50
```

The daemon's default URL is `ws://127.0.0.1:8000/rpc` with `root`/`root`. If you changed the docker-compose credentials, update `world_db_username` / `world_db_password` in `~/.coremind/config.toml` to match.

If the schema apply step fails with a SurrealQL parse error, you're running a SurrealDB version that isn't compatible with `src/coremind/world/schema.surql`. Pin the image tag in `docker-compose.yml` to the version recorded in the schema header.

### Qdrant unreachable or "collection not found"

The semantic memory collection is created lazily on first write. If you see `collection not found` on read, no plugin has produced an embedded event yet — start a plugin and wait for one cycle. If Qdrant itself is down:

```bash
docker compose restart qdrant
docker compose logs qdrant --tail 50
```

Embeddings persist in the Qdrant data volume; restarting does not lose them.

---

## Plugins

### Plugin signature verification failed

Logged as `IngestError: invalid signature for source=<plugin_id>`.

1. The plugin's public key changed (regenerated keypair). The daemon caches the registered public key per `source`. Either restore the old key file under `~/.coremind/keys/plugins/` or remove the registration so the daemon accepts the new key on reconnect (`coremind plugin forget <plugin_id>`).
2. The canonical-JSON serialization differs between client and daemon. The plugin must serialize using a JCS (RFC 8785) implementation. A non-JCS `json.dumps` will produce a stable-looking but wrong digest. Use `coremind_plugin_api.signing` on the Python side; the corresponding library is referenced in the PDK for other languages.

### Plugin connects then disconnects

Run the daemon with `COREMIND_LOG_LEVEL=debug`. The handshake exchange is logged with reasons. Common causes:

- `protocol_version` mismatch — the plugin was built against a newer or older `plugin.proto`. Regenerate stubs (`just proto-gen` or the equivalent in the plugin's repo).
- Manifest declares a permission the daemon's policy file rejects. Check `~/.coremind/policy.toml`.
- Plugin process is crashing on startup — check the plugin's own stderr; the daemon does not capture it.

### Events from a plugin stop arriving

```bash
coremind plugin list                  # last-seen timestamp per source
coremind events query --source <id> --limit 5
```

If `last_seen` is recent but no events: the plugin is connected but idle (probably correct). If `last_seen` is stale: the plugin process died or its keepalive expired. Restart the plugin process. The supervisor (post-v0.1.0) will do this automatically.

---

## LLM provider errors

### `LLMError: structured output validation failed`

The model returned text that did not parse against the Pydantic response schema. The daemon never tries to "fix" the output — it logs the raw response (truncated) and aborts the cycle. If this is consistent for one model, switch to a different provider in `~/.coremind/config.toml` and re-run `coremind reasoning cycle --now`.

### `LLMError: rate limit / 429`

The reasoning loop applies exponential backoff and re-queues the cycle. If errors persist, lower `reasoning.cycles_per_hour` or move to a higher-tier API key. The cycle scheduler is bounded; back-pressure is logged but does not crash the daemon.

### `LLMError: provider not in allowlist`

The configured `llm.model` is outside `agents.defaults.models`. Either add the provider to the allowlist (and re-evaluate the trust trade-off) or pick an allowed provider. The allowlist exists so a typo cannot silently exfiltrate data to an unintended endpoint.

---

## Audit journal

### `coremind audit verify` reports a broken hash chain

```
ERROR: chain broken at seq=<N>: prev_hash mismatch
```

This means `~/.coremind/audit.log` was tampered with or truncated. The daemon will refuse to append further entries. Investigation:

1. Save the existing log: `cp ~/.coremind/audit.log ~/.coremind/audit.log.broken`.
2. Find the last good seq: `coremind audit verify --report`.
3. Decide whether to truncate to the last good entry (loses subsequent history but resumes operation) or restore from backup.
4. File a security incident if tampering is suspected — the journal is signed, so chain breaks are not benign.

The journal is append-only by design; manual edits are never the right fix.

### Disk full

The journal has no built-in rotation in v0.1.0. Truncation that breaks the chain is a hard error (above). Operators should monitor `~/.coremind/audit.log` size and configure log-rotate-style archival externally. A native rotation policy is on the post-v0.1.0 roadmap.

---

## Dashboard

### `/api/approvals` returns 401

The shared bearer token in `dashboard.api_token` does not match the token your client sent. The token lives under `~/.coremind/secrets/dashboard_token` (chmod 600). Regenerate it with `coremind dashboard token rotate`; update any external client (Telegram bridge, browser bookmark) to match.

### `/api/approvals` returns 403

Origin/Referer mismatch. The dashboard rejects requests whose `Origin` does not match the configured dashboard origin (`http://127.0.0.1:9900` by default). If you reverse-proxy the dashboard, set `dashboard.expected_origin` to the public origin.

### Dashboard shows 503 on the intents page

The Notification Port adapter for the dashboard is not registered. Confirm that `notify.adapters` includes `dashboard` in `~/.coremind/config.toml` and that the daemon process started after the config change.

### SSE feed silent

`Content-Security-Policy` denies inline scripts; older browsers without EventSource may not connect. Use a current Firefox/Chromium build. If the feed works in the browser console (`new EventSource('/events/stream')`) but not in the page, file a bug — that is a regression in the static asset.

---

## Recovery

### Reset to a known-good state without losing data

```bash
coremind daemon stop
coremind audit verify          # confirm journal is intact
docker compose restart
coremind daemon start
```

This re-applies the SurrealDB schema, reconnects to Qdrant, and replays no events (the journal is append-only and the world model is read from L2, not rebuilt from the journal in v0.1.0).

### Full reset (development only)

This **destroys** the world model, semantic memory, and audit journal:

```bash
coremind daemon stop
docker compose down -v
rm -rf ~/.coremind/audit.log ~/.coremind/state
# keys and config preserved
docker compose up -d
coremind daemon start
```

Never run this on an instance whose journal might be needed for forensic or compliance purposes.

---

## When to file an issue

Reproducible behavior that contradicts the docs, a stack trace from the daemon process, or any failed `coremind audit verify` should be filed with:

- `coremind --version`
- `coremind doctor` output (redact key fingerprints if you want to)
- The last 100 log lines around the failure
- Steps to reproduce, with config snippets if relevant

For suspected security issues, follow [`SECURITY.md`](../SECURITY.md) (private disclosure) instead of filing a public issue.
