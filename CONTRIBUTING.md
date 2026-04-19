# Contributing to CoreMind

**Version:** 0.1 (Phase 0)
**Status:** Stable
**Audience:** New contributors, plugin authors, core developers

---

## Table of Contents

1. [Before You Start](#1-before-you-start)
2. [Development Setup](#2-development-setup)
3. [Submitting a Core Change](#3-submitting-a-core-change)
4. [Submitting a Plugin](#4-submitting-a-plugin)
5. [RFC Process for Layer Changes](#5-rfc-process-for-layer-changes)
6. [Code Review Standards](#6-code-review-standards)
7. [Commit Conventions](#7-commit-conventions)

---

## 1. Before You Start

Read these documents in order before writing any code:

1. [`.github/copilot-instructions.md`](.github/copilot-instructions.md) — conventions, non-negotiables, tech stack
2. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — authoritative technical design
3. [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) — Python style, naming, error handling
4. [`docs/phases/README.md`](docs/phases/README.md) — active phase and what is in scope

**Do not implement anything that falls outside the active phase.** Each phase doc has an explicit "Out of Scope" section; respect it.

---

## 2. Development Setup

```bash
# Clone the repository
git clone <repo-url>
cd coremind

# Bootstrap the dev environment (creates venv, installs all deps)
just setup

# Verify the environment
just lint           # ruff check . && mypy src/
just spec-validate  # validate JSON schemas
just proto-gen      # regenerate protobuf stubs
```

All commands must pass clean before opening a pull request.

### Requirements

- Python 3.12+
- [`just`](https://github.com/casey/just) command runner
- Docker + Docker Compose (for integration tests only)

---

## 3. Submitting a Core Change

Core changes touch `src/coremind/`, `spec/`, `.github/`, or `docs/`.

### Checklist

- [ ] `just lint` passes with zero warnings.
- [ ] `just spec-validate` and `just proto-gen` pass clean.
- [ ] Tests accompany the change. Target ≥ 80 % coverage on touched modules.
- [ ] No new `Any` in public signatures. `mypy --strict` must pass.
- [ ] Every autonomous side-effect is signed and journaled (see [Architecture §9](docs/ARCHITECTURE.md)).
- [ ] User data does not leave the host by default.
- [ ] Commit messages follow the [Conventional Commits](#7-commit-conventions) format.

### Workflow

1. Fork the repository and create a feature branch off `main`.
2. Work in small, focused commits. One logical change per commit.
3. Open a pull request against `main` with a short description of *what* changed and *why*.
4. A maintainer will review within 5 business days. Address all blocking comments before merge.

**Layer-level changes** (changes to the contract between two of the seven cognitive layers, or changes that affect `spec/`) require an RFC. See [§5](#5-rfc-process-for-layer-changes).

---

## 4. Submitting a Plugin

Plugins extend CoreMind through the gRPC plugin protocol defined in [`spec/plugin.proto`](spec/plugin.proto). A plugin runs as a separate process; the daemon never loads plugin code in-process.

### Plugin Requirements

1. **Implement the `CoreMindPlugin` gRPC service** as specified in `spec/plugin.proto`.
2. **Ship a `PluginManifest`** — name, version, declared permissions, and supported `WorldEvent` attribute namespaces.
3. **Declare every outbound network permission** your plugin needs. Undeclared network calls are a disqualifying defect.
4. **Sign every `WorldEvent` you emit** using an ed25519 key pair. The daemon rejects unsigned events.
5. **Include tests** that exercise your plugin in isolation (no daemon required).

### Directory Layout

Place your plugin under `plugins/<plugin-name>/` with at least:

```text
plugins/<plugin-name>/
├── README.md          # what it does, how to configure it
├── pyproject.toml     # or package.json for TypeScript plugins
├── plugin.py          # or main entry point
└── tests/
    └── test_plugin.py
```

### Review Criteria

Plugin pull requests are reviewed for:

- Correct implementation of the plugin protocol
- No undeclared side-effects or network calls
- No raw message bodies stored outside L3 semantic memory
- No secrets embedded in source or committed files

---

## 5. RFC Process for Layer Changes

A **layer-level change** is any change that:

- Modifies the contract between two of the seven cognitive layers (L1–L7)
- Alters a field in `spec/worldevent.schema.json` or `spec/plugin.proto`
- Introduces a new storage dependency or replaces an existing one
- Changes the audit log format in `spec/audit_log.md`
- Changes the signing or verification algorithm

These changes require an RFC to capture the trade-offs before implementation begins.

### RFC Workflow

1. **Open a discussion** (GitHub Discussions, label `rfc`) describing the problem and proposed change.
2. **Write an RFC document** in `docs/rfcs/NNNN-<short-title>.md`. Template:

   ```md
   # RFC NNNN — <Title>

   **Status:** Draft | Accepted | Rejected | Superseded
   **Author:** <GitHub handle>
   **Created:** YYYY-MM-DD

   ## Problem
   What is broken or missing?

   ## Proposal
   What change do you propose, and why?

   ## Alternatives Considered
   What other approaches did you evaluate?

   ## Impact
   Which layers are affected? What existing tests break? Is the audit log format stable after this?

   ## Open Questions
   What remains unresolved?
   ```

3. **Discussion period:** 7 calendar days minimum. Maintainers and active contributors may comment.
4. **Resolution:** a maintainer marks the RFC `Accepted` or `Rejected` with a brief rationale. Accepted RFCs unblock the implementation PR.

Implementation PRs for an accepted RFC reference the RFC number in their description and commit messages:

```text
feat(world): add delta field to WorldEvent (RFC 0002)
```

---

## 6. Code Review Standards

Reviewers check for:

- **Correctness:** does the code do what the description claims?
- **Invariants:** is every autonomous side-effect signed and journaled?
- **Type safety:** does `mypy --strict` pass on new and changed code?
- **Test coverage:** are new code paths exercised by the accompanying tests?
- **Scope creep:** does the PR stay within the active phase?
- **Security posture:** no secrets in code, no tainted data flowing to action-shaping paths without classification.

Reviewers may not request stylistic changes that are not enforced by Ruff or mypy. Automated tools are the authority on style.

---

## 7. Commit Conventions

CoreMind uses [Conventional Commits](https://www.conventionalcommits.org/).

```text
<type>(<scope>): <short description>
```

**Types:**

| Type | When to use |
| --- | --- |
| `feat` | New user-visible capability |
| `fix` | Bug fix |
| `refactor` | No behavior change |
| `docs` | Documentation only |
| `test` | Tests only |
| `chore` | Tooling, deps, housekeeping |
| `perf` | Performance improvement |

**Scope** is the top-level module or concern: `world`, `crypto`, `plugin`, `ci`, `phases`, etc.

**Examples:**

```text
feat(world): add delta field to WorldEvent
fix(crypto): reject events with expired timestamps
docs(phases): clarify phase 1 handoff criteria
test(world): cover signature verification error paths
chore(deps): bump grpcio to 1.64.0
```

Every commit on `main` must pass `just lint && just spec-validate` locally before push.
