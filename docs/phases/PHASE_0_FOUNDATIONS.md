# Phase 0 — Foundations

**Duration:** ~1 week
**Prerequisite:** None
**Deliverable:** A fully specified project with no implementation yet, but all contracts, schemas, and conventions locked in.

---

## Goals

- Repository scaffolded and conventions documented.
- `WorldEvent` JSON Schema authoritative and versioned.
- Plugin gRPC contract (`plugin.proto`) written and compilable.
- Developer environment reproducible on Linux/macOS.
- CI pipeline skeleton green.
- Audit log format frozen.

---

## Deliverables Checklist

- [ ] `pyproject.toml` at the repo root, Python 3.12+, Poetry or `uv` managed
- [ ] `spec/worldevent.md` — prose spec
- [ ] `spec/worldevent.schema.json` — machine-readable JSON Schema (Draft 2020-12)
- [ ] `spec/plugin.proto` — gRPC contract for plugins
- [ ] `spec/audit_log.md` — spec for the signed, hash-chained journal
- [ ] `.github/workflows/ci.yml` — lint + spec validation (no real tests yet)
- [ ] `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `LICENSE` (AGPL-3.0)
- [ ] `Makefile` or `Justfile` with `setup`, `lint`, `spec-validate`, `proto-gen`
- [ ] Coding conventions doc at `docs/CONVENTIONS.md`

---

## Repository Layout

```
coremind/
├── README.md
├── LICENSE
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── pyproject.toml
├── Justfile
├── .github/
│   └── workflows/
│       └── ci.yml
├── docs/
│   ├── EXECUTIVE_SUMMARY.md
│   ├── ARCHITECTURE.md
│   ├── CONVENTIONS.md
│   └── phases/
│       └── PHASE_*.md
├── spec/
│   ├── worldevent.md
│   ├── worldevent.schema.json
│   ├── plugin.proto
│   └── audit_log.md
├── src/
│   └── coremind/             # empty package for now
│       └── __init__.py
├── plugins/
│   └── .gitkeep              # plugins live here
└── tests/
    └── .gitkeep
```

---

## Tasks for the Coding Agent

### 0.1 Initialize the repository

- Initialize as a plain git repo locally, no remote yet.
- Create the directory structure above.
- Write `pyproject.toml` with project metadata, Python 3.12 requirement, dev dependencies: `ruff`, `pytest`, `mypy`, `jsonschema`, `grpcio-tools`, `pydantic>=2.7`.
- Write a `Justfile` (or `Makefile`) with:
  - `setup` — create venv, install deps
  - `lint` — `ruff check .` + `mypy src/`
  - `spec-validate` — validate `worldevent.schema.json` against the JSON Schema meta-schema
  - `proto-gen` — compile `plugin.proto` into Python stubs in `src/coremind/plugin_api/_generated/`

### 0.2 Write `spec/worldevent.md`

Prose spec for the `WorldEvent` format. Must include:
- Purpose and scope
- Field-by-field description with rationale
- Canonical JSON serialization rules (for signing)
- Minimum, typical, and maximum payload examples
- Version compatibility rules (semver + `source_version` semantics)

### 0.3 Write `spec/worldevent.schema.json`

JSON Schema (Draft 2020-12) matching the prose spec exactly. Must validate the three example payloads from the prose doc.

Required top-level fields: `id`, `timestamp`, `source`, `source_version`, `signature`, `entity`, `attribute`, `value`, `confidence`.
Optional: `unit`, `delta`, `context`.

### 0.4 Write `spec/plugin.proto`

gRPC contract. Services:
- `CoreMindPlugin` — implemented by plugins
- `CoreMindHost` — implemented by the daemon (plugins can call back for things like requesting secrets)

See §5 of `ARCHITECTURE.md` for the service shape.

Include messages for `PluginManifest`, `PluginConfig`, `WorldEvent`, `HealthStatus`, `ActionRequest`, `ActionResult`.

### 0.5 Write `spec/audit_log.md`

Specification for the tamper-evident action journal:
- JSONL format, one action per line
- Each line ends with an ed25519 signature of the canonical form
- Hash-chained: the signature is over `canonical(line) || previous_signature`
- Verification procedure
- Recovery procedure for a broken chain (abort and quarantine, never auto-repair)

### 0.6 Write `docs/CONVENTIONS.md`

Document:
- Python style: Ruff-enforced, type hints mandatory
- Naming: `snake_case` for functions, `PascalCase` for classes, no abbreviations in public names
- Error handling: custom exception hierarchy, never swallow errors silently
- Logging: structured (JSON in prod, human in dev), use `structlog`
- Testing: pytest, `tests/` mirrors `src/`, every public function has at least one test
- Commit message format: Conventional Commits

### 0.7 Wire up CI

`.github/workflows/ci.yml` on push / PR to main:
- Set up Python 3.12
- `just setup`
- `just lint`
- `just spec-validate`
- `just proto-gen` (verify generated files are committed by failing if git diff is non-empty)

### 0.8 Write `CONTRIBUTING.md`

How to submit plugins, how to submit core changes, how the RFC process works for layer-level changes. Phase 0 version can be brief.

---

## Success Criteria

1. `just setup && just lint && just spec-validate && just proto-gen` runs clean.
2. `spec/worldevent.schema.json` validates all three example payloads in `spec/worldevent.md`.
3. `spec/plugin.proto` compiles without errors.
4. CI is green on a trivial PR.
5. The repo, as-is, gives an external contributor everything they need to understand the contracts before a single line of runtime code exists.

---

## Explicitly Out of Scope

- Any runtime code in `src/coremind/` beyond the generated proto stubs
- Any plugin implementation
- Any database setup
- Any LLM integration

Phase 0 ships **contracts**, not behavior.

---

## Handoff to Phase 1

Phase 1 begins with:
- `spec/worldevent.schema.json` as the authoritative validator
- Generated proto stubs in `src/coremind/plugin_api/_generated/`
- A green CI baseline
- Documented conventions

**Next:** [`PHASE_1_CORE_DAEMON.md`](PHASE_1_CORE_DAEMON.md)
