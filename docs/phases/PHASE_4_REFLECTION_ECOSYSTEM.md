# Phase 4 — Reflection + Ecosystem

**Duration:** ~1 week (plus ongoing ecosystem work)
**Prerequisite:** Phase 3 complete
**Deliverable:** The system evaluates its own effectiveness and improves. The project is ready for public release with polished docs, a dashboard, and a plugin marketplace.

---

## Goals

- Reflection loop (L7) runs weekly, evaluates predictions and intents against reality.
- Calibration improves over time (Brier score tracked).
- Procedural memory evolves (new rules promoted, bad rules deprecated).
- A web dashboard offers full read-only observability.
- A second channel adapter (Slack or Discord).
- A documented Plugin Development Kit (PDK) for external contributors.
- v0.1.0 release on GitHub: tagged, with changelog, release notes, install instructions.

---

## Deliverables Checklist

- [ ] `src/coremind/reflection/loop.py` — weekly reflection cycle
- [ ] `src/coremind/reflection/evaluator.py` — prediction verification
- [ ] `src/coremind/reflection/calibration.py` — Brier score + reliability diagrams
- [ ] `src/coremind/reflection/rule_learner.py` — promote/deprecate procedural rules
- [ ] `src/coremind/reflection/report.py` — human-readable weekly report
- [ ] `src/coremind/dashboard/` — SvelteKit app embedded in the daemon's HTTP server
- [ ] `src/coremind/channels/slack.py` OR `src/coremind/channels/discord.py`
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

SvelteKit app built into static assets, served by the daemon's HTTP server on `127.0.0.1:9900`.

Pages:
- `/` — status overview
- `/events` — live event stream (SSE)
- `/graph` — force-directed visualization of the World Model
- `/reasoning` — recent cycles with their outputs
- `/intents` — intent queue + history
- `/actions` — audit journal with search
- `/reflection` — weekly reports archive

The dashboard is **read-only**. All state-changing operations still go through the CLI or channel adapters, which sign and journal them.

### 4.7 Second Channel Adapter

Implement one of: Slack, Discord, Signal, Matrix. Recommendation: **Discord** for community resonance and simplicity of bot setup.

Same interface as Telegram adapter. Docs updated to describe both options.

### 4.8 Plugin Development Kit

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
