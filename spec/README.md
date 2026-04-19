# CoreMind Specs

This directory holds the **contracts** of CoreMind — the things every implementation
must agree on before any code runs.

## Files (to be written during Phase 0)

| File | Task | Status |
|------|------|--------|
| `worldevent.md` | 0.2 — Prose spec for the `WorldEvent` format | ✓ |
| `worldevent.schema.json` | 0.3 — Machine-readable JSON Schema (Draft 2020-12) | ✓ |
| `plugin.proto` | 0.4 — gRPC contract for plugins | ✓ |
| `audit_log.md` | 0.5 — Tamper-evident action journal spec | ✓ |

Once all four are written, `just spec-validate` and `just proto-gen` must pass.

See `docs/phases/PHASE_0_FOUNDATIONS.md` for the full task list.
