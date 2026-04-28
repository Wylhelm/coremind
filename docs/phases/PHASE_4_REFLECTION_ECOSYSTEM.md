# Phase 4 — Reflection + Ecosystem

**Duration:** ~1 week (plus ongoing ecosystem work)
**Prerequisite:** Phase 3 complete
**Deliverable:** The system evaluates its own effectiveness and improves. The project is ready for public release with polished docs, a dashboard, and a plugin marketplace.

---

## Goals

- Reflection loop (L7) runs weekly, evaluates predictions and intents against reality.
- Calibration improves over time (Brier score tracked).
- Procedural memory evolves (new rules promoted, bad rules deprecated).
- **Agency learning:** L7 analyzes the approval ledger and proposes category changes (`ask` → `suggest`, `suggest` → `safe`) when the user has demonstrated a consistent pattern. Proposed changes are themselves `ask` intents — CoreMind never silently changes its own agency level (see `ARCHITECTURE.md §15.7`).
- A web dashboard offers full read-only observability and renders in-dashboard approval notifications via the `NotificationPort` implemented in Phase 3.
- A second Notification Port adapter (Signal, Discord, or email).
- A documented Plugin Development Kit (PDK) for external contributors.
- v0.1.0 release on GitHub: tagged, with changelog, release notes, install instructions.

---

## Deliverables Checklist

- [ ] `src/coremind/reflection/loop.py` — weekly reflection cycle
- [ ] `src/coremind/reflection/evaluator.py` — prediction verification
- [ ] `src/coremind/reflection/calibration.py` — Brier score + reliability diagrams
- [ ] `src/coremind/reflection/rule_learner.py` — promote/deprecate procedural rules
- [ ] `src/coremind/reflection/agency_learner.py` — proposes category promotions/demotions from the approval ledger
- [ ] `src/coremind/reflection/report.py` — human-readable weekly report
- [ ] `src/coremind/dashboard/` — SvelteKit app embedded in the daemon's HTTP server (renders live events, world snapshot, intents, action journal, reflection reports) + live approval buttons via the dashboard `NotificationPort` adapter (port implemented in Phase 3)
- [ ] `src/coremind/notify/adapters/<second>.py` — one additional Notification Port adapter (Signal / Discord / email)
- [ ] `docs/PDK.md` — Plugin Development Kit
- [ ] `docs/RELEASE.md` — release process + versioning policy
- [ ] `CHANGELOG.md`
- [ ] v0.1.0 git tag + GitHub release

---

## Tasks for the Coding Agent

### 4.1 Reflection Loop

**File:** `src/coremind/reflection/loop.py`

Scheduled to run weekly (configurable). Can also run on demand (`coremind reflect --now`).

Cycle:
1. Pull all reasoning cycles, intents, and actions from the last reflection window.
2. Evaluate each prediction against what actually happened (L2 history).
3. Evaluate each action against user feedback (approvals, reversals, dismissals).
4. Update calibration tables.
5. Learn procedural rules from outcomes.
6. Produce a human-readable report.
7. Notify via configured channel.

### 4.2 Prediction Evaluation

**File:** `src/coremind/reflection/evaluator.py`

For each `Prediction` from L4 outputs:
- Did its `falsifiable_by` condition resolve in world state within `horizon_hours`?
- Score: right / wrong / undetermined (if the condition never became observable).

Results stored in a dedicated table, queryable.

### 4.3 Calibration

**File:** `src/coremind/reflection/calibration.py`

Track Brier score and reliability diagram data, per layer + per model:
- Bucket predictions by reported confidence (0–10%, 10–20%, …, 90–100%).
- For each bucket, compute the empirical success rate.
- A well-calibrated system has bucket success rate ≈ bucket midpoint.

Expose a calibration correction function that adjusts future confidence reports (reasoning and intention) toward reality.

### 4.4 Rule Learner

**File:** `src/coremind/reflection/rule_learner.py`

- **Promotion:** if a repeated reasoning pattern → intent → successful action sequence happens ≥ N times with ≥ M success rate, propose a new procedural rule. Require human approval before activation.
- **Deprecation:** procedural rules whose success rate falls below threshold get flagged for user review.
- Rules carry `last_evaluated_at` and `evaluation_count` so we don't churn on small samples.

#### v1 simplifications (tracked in module docstring)

The shipped Task 4.4 implementation is intentionally narrower than the spec above. Each item is a follow-up before the loop is wired to a notifier:

1. **Pattern key.** Promotion candidates are keyed on `(action_class, operation)` only — the upstream reasoning cycle and intent are accepted by the `RuleLearner` Protocol but not folded into the candidate identity. Pattern-grounded promotion is deferred.
2. **Trigger shape.** Promoted `Rule.trigger` only carries an `action_class` precondition. Previews are emitted with `confidence=0.0` and a description that warns the reviewer to refine the precondition before approval. Activation remains human-gated, so the agency contract is preserved.
3. **Rule churn fields.** `coremind.memory.procedural.Rule` does not yet carry distinct `last_evaluated_at` / `evaluation_count` fields; deprecation uses `Rule.applied_count` as the proxy. A schema bump should add the dedicated fields when the procedural store gains its next mutation.
4. **Persistence.** `CandidateLedger` and `RuleProposalStore` ship with in-memory adapters only. The SurrealDB-backed pair lives in `coremind.reflection.store` as a follow-up; until it lands, pending proposals do not survive a daemon restart and the in-memory ledger's deduplication set grows without bound.

### 4.5 Weekly Report

**File:** `src/coremind/reflection/report.py`

Produces a Markdown report like:

```
# CoreMind — Weekly Reflection
Week of 2026-04-14 → 2026-04-20

## Highlights
- 127 reasoning cycles executed (avg latency 2.3 s, model: anthropic/claude-opus-4-7)
- 43 intents generated, 38 executed autonomously, 5 required approval
- 4 actions reversed by the user (see §Missed Calls)

## Predictions scoreboard
- 28 predictions made
- 19 correct, 6 wrong, 3 undetermined
- Brier score: 0.14 (better than last week's 0.17)

## New patterns noticed
- Morning wakeup window shifted from 7:00 to 7:30 (weekday)
- Humidifier activation correlates with sleep quality drops

## Proposed new rules (awaiting your approval)
1. "When bedroom humidity < 35% AND Guillaume is asleep → mist level 4"
2. ...

## Rules I should probably deprecate
1. "Morning briefing at 8:00 sharp" — you dismiss it 60% of weekdays lately.

## Things I want to ask
- Do you still care about the FluxLead project? No events in 11 days.
```

### 4.6 Web Dashboard

**Directory:** `src/coremind/dashboard/`

Server-rendered dashboard using **Starlette + Jinja2**, served on `127.0.0.1:9900` (loopback-only by default; host/port surfaced via config). The original phase plan called for a SvelteKit SPA built into static assets — we deliberately chose server-side rendering to keep the surface small, the CSP strict, and the dependency footprint Python-only. A future SPA migration is allowed but not required for v0.1.0.

Pages:

- `/` — status overview (counters from each data source; pending-approval count, not lifetime notification count)
- `/events` — recent events table + live tail via SSE
- `/reasoning` — recent cycles with their outputs
- `/intents` — **pending** intent queue (items disappear once a response is recorded) + recent history
- `/actions` — audit journal with search/filter
- `/reflection` — weekly reports archive

> The `/graph` force-directed World Model visualization from the original spec is **deferred** — it requires a client-side bundle and is not a v0.1.0 release blocker.

#### Read paths

All pages read through `Protocol`-typed ports (`data.py`):

- `JournalSource` exposes `read_recent(limit: int, since: datetime | None) -> list[JournalEntryView]` rather than `read_all() -> list[object]`. `JournalEntryView` is a Protocol with typed fields (`seq: int`, `kind: str`, `timestamp: datetime`, `payload: Mapping[str, Any]`) so `mypy --strict` covers the dashboard end-to-end.
- `DashboardNotificationPort` exposes a distinct `pending()` view in addition to `history()`. Entries are removed from `pending()` when a matching `ApprovalResponse` is submitted. The overview counter and intents page consume `pending()`.

#### Write path: approvals

The dashboard is otherwise read-only, but `/api/approvals` accepts `ApprovalResponse` submissions because the alternative (CLI-only approvals) is a poor UX. This endpoint is the only state-changing surface and must satisfy:

1. **Authentication.** A shared token loaded from config (`dashboard.api_token`, persisted under `~/.coremind/secrets/`) is required as `Authorization: Bearer <token>`. Missing/invalid token → `401`.
2. **Origin/Referer validation.** Requests whose `Origin` (or `Referer` when `Origin` is absent) does not match the configured dashboard origin are rejected with `403`. This blocks DNS-rebinding and drive-by CSRF from other localhost services.
3. **Bound responder identity.** The `UserRef` written into the journal is taken from `dashboard.operator` config (id + display name), not hardcoded to `"dashboard"`. The audit journal therefore attributes who approved.
4. **Forced-approval classes still flow through `ApprovalManager`.** The dashboard never bypasses category gating; it submits the same `ApprovalResponse` as the Telegram adapter would.

#### Output safety (XSS hardening)

Every value rendered into the DOM originates from plugin-supplied `WorldEvent`s and must be treated as **tainted**:

- Server-side templates use Jinja autoescape (already on). The custom `tojson` filter is documented inline as "safe only because the result still flows through autoescape".
- The SSE client script **never** uses `innerHTML` with interpolated event fields. Rows are constructed with `document.createElement` + `textContent` for `source`, `entity.type`, `entity.id`, `attribute`; `value` is rendered as `JSON.stringify(...)` inside a `<code>` element via `textContent`. The same rule applies to any future SSE-driven view.
- Inline scripts are replaced by a single static asset served from the dashboard origin so a strict CSP is enforceable.

#### Security headers

Every response (HTML and SSE) carries:

- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `X-Content-Type-Options: nosniff`

#### Tests

In addition to the existing coverage (empty sources, idempotent lifecycle, SSE happy/no-subscriber, journal filtering, invalid JSON, invalid decision enum, missing adapter → 503), add:

- An XSS regression test: feed an event whose `value`/`entity.id`/`attribute` contains `<img src=x onerror=alert(1)>`-style payloads and assert the rendered HTML and the SSE frame contain only escaped output.
- An auth test matrix for `/api/approvals`: missing token → 401, wrong token → 401, bad `Origin` → 403, valid token + origin → 202 and the journal entry's responder matches the configured operator.
- A "pending vs history" test: submitting a response removes the intent from `pending()` while leaving it in `history()`; the overview counter reflects `pending()`.
- A counter assertion that targets a labelled element (e.g. `data-testid="pending-approvals"`) instead of brittle `">N<"` substring matches.

The dashboard remains read-only with respect to the World Model, Memory, and any side-effecting subsystem; the only state-changing endpoint is `/api/approvals`, which goes through `ApprovalManager` exactly as channel adapters do.

### 4.7 Second Channel Adapter - Skipped for now

Implement one of: Slack, Discord, Signal, Matrix. Recommendation: **Discord** for community resonance and simplicity of bot setup.

Same interface as Telegram adapter. Docs updated to describe both options.

### 4.8 Plugin Development Kit - Skipped for now

**File:** `docs/PDK.md`

Covers:
- How to scaffold a new plugin (Python first, JS/TS second, Go/Rust referenced)
- Manifest specification
- The gRPC contract in detail
- Signing and key management for plugins
- Testing a plugin against a local daemon
- Publishing a plugin: naming conventions, where to list it (official plugin directory in `docs/plugins/`)
- Versioning policy (semver, compat with `source_version` field on WorldEvents)

Include a working template at `examples/plugin-template-python/`.

### 4.9 Release Process

**File:** `docs/RELEASE.md`

- Semantic versioning policy
- How to cut a release (tag, changelog, build artifacts)
- Supported install paths: `pipx install coremind`, Docker image, standalone binary (via PyInstaller or Rust migration later)
- Signing releases with a dedicated project GPG key

### 4.10 Polish

- Complete docstrings across public API
- `docs/FAQ.md`
- `docs/TROUBLESHOOTING.md`
- Demo GIF / video for the README
- License headers on source files where applicable
- `CHANGELOG.md` backfilled with Phase 0–4 summary

### 4.11 Tests & Scenarios

Golden-path end-to-end scenarios that the CI runs before release:
- **S1:** Cold start → 1 hour of simulated events → at least 1 complete reasoning cycle produced.
- **S2:** Reflection → rule promoted → next week's reasoning uses the rule.
- **S3:** Journal integrity: inject a tampered entry → `coremind audit verify` fails with clear error.
- **S4:** Plugin crash → auto-restart (if supervisor enabled) → events resume without data loss beyond the crash window.
- **S5:** LLM provider fails mid-cycle → cycle aborts cleanly → next cycle succeeds after restore.

---

## Success Criteria

1. Reflection runs weekly, emits a report, user receives it.
2. Brier score is tracked over weeks (stored; viewable in dashboard).
3. At least one procedural rule has been learned and activated (after user approval) in a soak test.
4. Dashboard is reachable on `127.0.0.1:9900` and shows all expected views.
5. A second channel adapter (Slack or Discord) delivers approval requests end-to-end.
6. `docs/PDK.md` lets an external developer ship a plugin in under an hour.
7. `git tag v0.1.0` exists; GitHub release is published with a changelog.

---

## Post-Release: Ecosystem Phase (ongoing)

After v0.1.0, the project enters community mode:

### Community-track priorities
- Plugin directory with vetted community plugins
- Official plugin SDK libraries: Python, JavaScript/TypeScript, Go, Rust
- Translated docs (starting with French, then broader)
- Federation RFC: two CoreMind instances sharing subsets of world state under explicit consent
- Hosted demo (opt-in, showing what a fresh instance looks like after seeding)

### Core-track priorities
- Rust migration of hot paths (event bus, signature verification)
- Embedded LLM support (ship a small model, make offline mode default)
- Memory decay policies
- Multi-user instances (households)

---

## Closing Note

When Phase 4 ships, CoreMind is more than a personal tool — it's a reference implementation for a new kind of software:

**Software that thinks about you, with your consent, for your benefit, that you own completely.**

If we get this right, others will fork and rebuild in ways we can't yet imagine. That's the point.

---

**Back to:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md) · [`../EXECUTIVE_SUMMARY.md`](../EXECUTIVE_SUMMARY.md)
