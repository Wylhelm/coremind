# Phase 3.5E — Reaction, CLI & Dashboard

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_5_JEPA_PREDICTION.md](PHASE_3_5_JEPA_PREDICTION.md)
**Prerequisites:** [PHASE_3_5C_PIPELINE_AND_ANOMALY_EVENTS.md](PHASE_3_5C_PIPELINE_AND_ANOMALY_EVENTS.md)
**Optional pairing:** [PHASE_3_5D_TRUE_JEPA.md](PHASE_3_5D_TRUE_JEPA.md) (works with either backend)
**Estimated effort:** 4–5 hours

---

## 1. Goal

Turn anomaly events into agent behavior, and surface the predictive layer in the operator tools:

- L4 reacts to `anomaly.detected` by forcing a reasoning cycle (debounced).
- L5 surfaces recent anomalies in its prompt, grounded in the cited entities.
- Salience scoring maps divergence to a continuous score, used by L5's existing event-driven queue.
- The `coremind prediction` CLI gains `status`, `anomalies`, and (with 3.5D) `compare`.
- The web dashboard gets a `/prediction` page with a live divergence chart and a recent-anomalies table.
- `docs/ARCHITECTURE.md` gains a short "L2.5 — Predictive Layer" section, and a tuning guide is added.

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/intention/salience.py` | Add `anomaly.detected` scoring branch. |
| `src/coremind/reasoning/loop.py` | `force_cycle(reason: str)` with 1/min debounce. |
| `src/coremind/reasoning/prompts.py` | "Recent anomalies" prompt section. |
| `src/coremind/intention/prompts.py` | Surface anomalies as high-priority items. |
| `src/coremind/cli/prediction_commands.py` | Implement `status`, `anomalies`, finalize `compare`. |
| `src/coremind/dashboard/server.py` | Register `/prediction` page + 3 JSON endpoints. |
| `src/coremind/dashboard/data_sources.py` | New `PredictionData` Protocol; default impl. |
| `src/coremind/dashboard/templates/prediction.html` | New page. |
| `src/coremind/dashboard/static/prediction.js` | Divergence chart + table refresh. |
| `docs/ARCHITECTURE.md` | New "L2.5 — Predictive Layer" subsection. |
| `docs/guides/PREDICTION_TUNING.md` | New short guide. |
| `tests/intention/test_salience_anomaly.py` | Mapping correctness. |
| `tests/reasoning/test_force_cycle.py` | Debounce, idempotency. |
| `tests/cli/test_prediction_commands.py` | All four subcommands. |
| `tests/dashboard/test_prediction_endpoints.py` | API contract, empty state. |
| `tests/test_e2e.py` | Extend with anomaly → L5 question loop. |

---

## 3. Salience & reaction

### 3.1 Salience mapping

In `intention/salience.py`, add a branch in the existing `score_salience(event)` dispatcher:

```python
def score_salience(event: WorldEventRecord) -> float:
    ...
    if event.attribute == "anomaly.detected":
        payload = event.value  # already validated against schema
        d = float(payload["divergence"]["value"])
        t = float(payload["divergence"]["threshold"])
        # Linear ramp: at the threshold → 0.25, at 4×threshold → 1.0.
        return min(1.0, max(0.0, (d - t * 0.5) / (3.5 * t)))
```

Numbers chosen so that a just-over-threshold anomaly is mid-low salience (operator-tunable noise floor) and an extreme divergence saturates. The mapping is documented in `docs/guides/PREDICTION_TUNING.md`.

### 3.2 `ReasoningLoop.force_cycle`

```python
import asyncio
from datetime import datetime, UTC, timedelta

class ReasoningLoop:
    _last_forced: datetime | None = None
    _force_lock: asyncio.Lock

    async def force_cycle(self, reason: str) -> bool:
        """Trigger a reasoning cycle ASAP if not debounced. Returns True if accepted."""
        async with self._force_lock:
            now = datetime.now(UTC)
            if self._last_forced and now - self._last_forced < timedelta(minutes=1):
                log.info("reasoning.force_cycle.debounced", reason=reason)
                return False
            self._last_forced = now
        # Use the existing cycle scheduler — do not run the cycle inline here.
        self._wake_event.set()
        log.info("reasoning.force_cycle.accepted", reason=reason)
        return True
```

Subscribe at daemon startup:

```python
self._event_bus.subscribe(
    predicate=lambda evt: evt.attribute == "anomaly.detected",
    handler=lambda evt: self._reasoning_loop.force_cycle(reason="anomaly"),
)
```

The debounce is intentional: a noisy predictor must not be able to monopolize the LLM budget.

### 3.3 Prompt updates

`reasoning/prompts.py` adds a section, populated from the last N anomaly events fetched from L2 within the cycle window:

```text
## Recent anomalies (last 1h)
- 2026-05-28T09:14:02Z — snapshot:s_8af2 — cosine 0.34 (threshold 0.15) — entities: sensor.living_room_temp, sensor.kitchen_temp
- ...
```

If the list is empty, the section is omitted entirely (no "No anomalies" noise). Each line cites entity IDs from `top_drifting_entities` when present (the field stays empty in v1; the formatting handles both cases gracefully).

`intention/prompts.py` adds an "Anomalous observations to investigate" bullet list when salience > 0.5 within the current cycle.

---

## 4. CLI — final shape

```bash
coremind prediction status
coremind prediction info
coremind prediction train [--backend baseline|jepa] [--limit N]
coremind prediction anomalies [--since 1h] [--limit 20] [--json]
coremind prediction compare [--backend-a baseline] [--backend-b jepa]
```

### 4.1 `status`

```text
$ coremind prediction status
Backend:           baseline
Enabled:           yes
Trained:           yes (baseline-20260528-091020-a3f1b22, 6h ago)
Window / horizon:  6 / 1
Divergence metric: cosine
Threshold:         0.142 (calibrated p95)
History samples:   1247
Last divergence:   0.041 (snapshot:s_8b09, 14s ago)
Anomalies (24h):   3
```

### 4.2 `anomalies`

```text
$ coremind prediction anomalies --since 24h --limit 5
TIMESTAMP             DIVERGENCE  THRESHOLD  SNAPSHOT  ENTITIES
2026-05-28T09:14:02Z  0.340       0.142      s_8af2    sensor.living_room_temp
2026-05-28T03:02:51Z  0.218       0.142      s_8a4d    (none)
...
```

`--json` switches to machine-readable output (NDJSON of `AnomalyRecord` model dumps).

Implementation reads from the audit log (already JSONL on disk) filtered by `attribute == "anomaly.detected"` — no new DB query path.

### 4.3 `compare`

Specified in [PHASE_3_5D §4](PHASE_3_5D_TRUE_JEPA.md#4-cli-additions). When 3.5D is not yet merged, `compare` exits with a friendly "JEPA backend not available" message.

---

## 5. Dashboard

### 5.1 Routes

In `dashboard/server.py` `create_app()`, add:

```python
app.router.add_get("/prediction", prediction_page)
app.router.add_get("/api/prediction/status", api_prediction_status)
app.router.add_get("/api/prediction/anomalies", api_prediction_anomalies)
app.router.add_get("/api/prediction/divergence_series", api_prediction_divergence)
```

### 5.2 Data source

Extend `dashboard/data_sources.py` with a Protocol:

```python
class PredictionData(Protocol):
    async def status(self) -> PredictorState: ...
    async def recent_anomalies(self, *, limit: int, since_seconds: float) -> list[AnomalyRecord]: ...
    async def divergence_series(self, *, points: int) -> list[tuple[datetime, float, float]]: ...  # (ts, value, threshold)
```

Default implementation reads `PredictorState` from the live `PredictiveService` and replays the audit log (with a small in-memory ring buffer of the last 500 divergences for charting). Buffer is updated by a structlog processor or a bus subscriber — pick one and document the choice in the module docstring.

### 5.3 Page

`templates/prediction.html`:

- Header card: backend, enabled, trained, version, samples in history.
- Time-series chart (Chart.js, already used elsewhere): divergence value with a horizontal threshold reference line.
- Recent-anomalies table.
- "Train now" button POSTing to `/api/prediction/train` (protected by the existing dashboard auth).

`static/prediction.js`:

- 5-second polling on the three endpoints.
- Pause polling when the tab is hidden (`document.visibilityState`).

### 5.4 Empty state

When the predictor is untrained or disabled, the page renders a "Predictor not yet trained" banner with the exact command to run (`coremind prediction train`). No charts, no errors.

---

## 6. Metrics & logging

`structlog` event names (consumed by existing log shippers):

| Event | Fields |
| --- | --- |
| `prediction.divergence` | `value`, `threshold`, `metric`, `backend`, `snapshot_id` |
| `prediction.anomaly_emitted` | `snapshot_id`, `value`, `threshold`, `confidence` |
| `prediction.skipped` | `reason` (`untrained`, `insufficient_history`, `disabled`) |
| `prediction.callback_failed` | exception traceback |
| `prediction.training.start` | `samples`, `backend` |
| `prediction.training.done` | `version`, `train_loss`, `val_loss`, `calibrated_threshold`, `duration_seconds` |
| `prediction.retrain.skipped` | `reason` |
| `prediction.retrain.failed` | exception traceback |
| `reasoning.force_cycle.accepted` / `.debounced` | `reason` |

---

## 7. Docs

### 7.1 `docs/ARCHITECTURE.md` — new subsection

Between the existing L2 and L3 sections, add a "L2.5 — Predictive Layer" subsection (≈ 30 lines):

- One-paragraph summary mirroring the parent phase doc's §1.
- The Mermaid diagram from the parent doc, reused.
- Pointer to `docs/phases/v2/PHASE_3_5_*` for the full design.
- Note on the fixed-encoder simplification.

### 7.2 `docs/guides/PREDICTION_TUNING.md` (new, ≈ 80 lines)

Sections:

1. When to enable.
2. Choosing `history_window` and `horizon_steps`.
3. Reading the dashboard chart.
4. Adjusting `anomaly_threshold` manually vs trusting calibration.
5. When to retrain.
6. Switching backends (`baseline` ↔ `jepa`).
7. Debugging silent predictors (`prediction.skipped` reasons).
8. Known noise sources (sensor restarts, plugin reconnections).

---

## 8. Tests

### 8.1 `tests/intention/test_salience_anomaly.py`

- Divergence equal to threshold → salience in `[0, 0.3]`.
- Divergence = 2 × threshold → salience in `[0.4, 0.6]`.
- Divergence ≥ 4 × threshold → salience = 1.0.
- Non-anomaly events → unaffected.

### 8.2 `tests/reasoning/test_force_cycle.py`

- First call returns `True` and sets the wake event.
- Second call within 60 s returns `False` and emits `debounced` log.
- After 60 s, accepted again.
- Concurrent calls from `asyncio.gather` produce exactly one acceptance.

### 8.3 `tests/cli/test_prediction_commands.py`

- `status` against an untrained service prints `Trained: no` and exits 0.
- `anomalies --json` produces valid NDJSON, each line validates against `AnomalyRecord`.
- `compare` without `torch` installed exits 0 with a clear stderr message.

### 8.4 `tests/dashboard/test_prediction_endpoints.py`

- Each endpoint returns 200 and a schema-conformant JSON body, even when the predictor is untrained (empty payload, no error).
- `divergence_series` returns up to `points` entries in chronological order.
- Auth: unauthenticated requests get 401 if the dashboard auth is enabled in the test fixture.

### 8.5 `tests/test_e2e.py` — extension

Add a scenario:

1. Daemon starts with `prediction.enabled = true` and a pre-trained model fixture.
2. A synthetic plugin emits a stream of stable snapshots, then a sharp anomaly.
3. Within the next cycle:
   - An `anomaly.detected` event appears in the audit log with a valid signature.
   - `ReasoningLoop._last_forced` was updated.
   - L5's prompt for that cycle includes the "Anomalous observations" section.
   - At least one generated `IntentionItem` cites the anomalous snapshot ID or one of its entities.

---

## 9. Success criteria

- [ ] L4 receives `anomaly.detected` and forces a cycle within 200 ms p95.
- [ ] L5 prompt includes recent anomalies when salience > 0.5.
- [ ] All CLI subcommands documented in `coremind prediction --help` and tested.
- [ ] Dashboard `/prediction` renders cleanly in both trained and untrained states.
- [ ] `docs/ARCHITECTURE.md` reflects the new layer.
- [ ] `docs/guides/PREDICTION_TUNING.md` exists and is linked from the dashboard page.
- [ ] E2E test passes end-to-end without flakes (10 consecutive runs).
- [ ] `just lint && just test` green.

---

## 10. Out of scope

- Plugin permission to expose anomalies externally (Notify, Slack, etc.) — handled in Phase 4.
- Per-entity drift attribution (`top_drifting_entities` ≠ empty). Requires per-attribute contribution analysis; deferred.
- Adaptive thresholds per time-of-day / day-of-week. v1 uses a single calibrated value.
- User feedback loop ("mark as not anomaly"). Requires UI work beyond this phase.
- Forwarding anomalies into the OpenClaw adapter (Phase 2.5 territory).
