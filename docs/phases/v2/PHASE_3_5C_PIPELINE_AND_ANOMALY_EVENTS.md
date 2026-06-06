# Phase 3.5C — Pipeline & Anomaly Events

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_5_JEPA_PREDICTION.md](PHASE_3_5_JEPA_PREDICTION.md)
**Prerequisites:** [PHASE_3_5A_FOUNDATIONS.md](PHASE_3_5A_FOUNDATIONS.md), [PHASE_3_5B_BASELINE_PREDICTOR.md](PHASE_3_5B_BASELINE_PREDICTOR.md)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Bring the predictor online. After this sub-phase:

- A `PredictiveService` is instantiated by the daemon (when `prediction.enabled = true`) and called by `WorldEncodingPipeline` immediately after `_memory.store(...)`.
- Divergences above the calibrated threshold publish a **signed** `WorldEventRecord` with `attribute="anomaly.detected"` onto the `EventBus`.
- A periodic background task retrains the predictor every `retrain_interval_hours`.
- The `spec/worldevent.schema.json` and `spec/audit_log.md` document the new event family.
- No L4/L5 behavior change yet (consumed in 3.5E).

This is the first sub-phase that affects the running system. It must remain a no-op when disabled, untrained, or below `min_training_samples`.

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/prediction/service.py` | `PredictiveService` orchestrator. |
| `src/coremind/world/pipeline.py` | Inject `predictive` dependency; call after store. |
| `src/coremind/core/daemon.py` | Build service in startup; register retrain task. |
| `src/coremind/world/model.py` | Document `anomaly.detected` attribute family (constant + docstring). |
| `spec/worldevent.schema.json` | Add `anomaly.detected` payload definition. |
| `spec/audit_log.md` | Note the new signed event. |
| `tests/prediction/test_service.py` | Behavior under untrained, below-threshold, anomalous. |
| `tests/prediction/test_pipeline_integration.py` | Pipeline emits event on injected anomaly. |
| `tests/prediction/test_retrain_task.py` | Periodic task triggers `Trainer.train()`. |

---

## 3. `PredictiveService` design

A single class owns: the live `TemporalPredictor`, the `EmbeddingHistory`, the `EventBus` handle, and the daemon's signer.

```python
from datetime import datetime, UTC
from typing import Final

import structlog

from coremind.core.event_bus import EventBus
from coremind.crypto.signatures import Signer
from coremind.prediction.base import PredictorPersistence, TemporalPredictor, Trainer
from coremind.prediction.errors import InsufficientHistoryError, UntrainedPredictorError
from coremind.prediction.history import EmbeddingHistory
from coremind.prediction.schemas import AnomalyRecord, Divergence
from coremind.world.model import WorldEventRecord

log = structlog.get_logger(__name__)

ANOMALY_ATTRIBUTE: Final[str] = "anomaly.detected"


class PredictiveService:
    def __init__(
        self,
        *,
        predictor: TemporalPredictor | None,
        trainer: Trainer,
        history: EmbeddingHistory,
        persistence: PredictorPersistence,
        event_bus: EventBus,
        signer: Signer,
        backend: str,
        anomaly_threshold: float,
        min_training_samples: int,
    ) -> None: ...

    async def on_snapshot_embedded(
        self,
        *,
        snapshot_id: str,
        observed_at: datetime,
        observed_embedding: tuple[float, ...],
    ) -> Divergence | None:
        """Called by WorldEncodingPipeline. Returns the divergence or None."""

    async def retrain(self) -> None:
        """Pull all history, train, persist, hot-swap the live predictor."""

    async def reload_from_disk(self) -> None:
        """Load the latest persisted model on daemon startup."""
```

### 3.1 `on_snapshot_embedded` algorithm

```text
1. If self._predictor is None or untrained → log "prediction.skipped" reason=untrained, return None.
2. window = await self._history.window()
   except InsufficientHistoryError → log "prediction.skipped" reason=insufficient_history, return None.
3. predicted = await self._predictor.predict(window)
4. divergence = await self._predictor.divergence(predicted.embedding, observed_embedding)
5. log "prediction.divergence" value=... threshold=... backend=...
6. If divergence.is_anomaly:
      record = AnomalyRecord(snapshot_id, observed_at, divergence, ...)
      event = WorldEventRecord(
          attribute=ANOMALY_ATTRIBUTE,
          entity_id=snapshot_id,
          value=record.model_dump(mode="json"),
          observed_at=observed_at,
          source="prediction",
          confidence=clamp(divergence.value / (2 * divergence.threshold), 0.0, 1.0),
      )
      signed = self._signer.sign_event(event)
      await self._event_bus.publish(signed)
      log "prediction.anomaly_emitted" snapshot_id=... divergence=...
7. Return divergence.
```

All numerical work runs in `asyncio.to_thread` via the predictor's own implementation; this method awaits, never blocks the loop.

### 3.2 `retrain` algorithm

```text
1. samples = await self._history.all_for_training()
2. If len(samples) < self._min_training_samples → log "prediction.retrain.skipped" reason=insufficient_samples; return.
3. log "prediction.retrain.start" samples=len(samples) backend=self._backend.
4. (predictor, metrics) = await self._trainer.train(samples)
5. version = await self._persistence.save(predictor)
6. self._predictor = predictor   # hot swap
7. log "prediction.retrain.done" version=... train_loss=... val_loss=... calibrated_threshold=... duration=...
```

Concurrency: guarded by an `asyncio.Lock` so a manual `coremind prediction train` cannot race the periodic task.

### 3.3 `reload_from_disk`

Called once during daemon startup. `await self._persistence.load(version=None)`. On success, the loaded predictor replaces the default-untrained one; on `None` the service stays untrained until the first retrain. `ModelArtifactError` is logged and swallowed — the daemon must not crash because of a corrupt local artifact.

---

## 4. Pipeline injection

`src/coremind/world/pipeline.py`:

```python
class WorldEncodingPipeline:
    def __init__(
        self,
        *,
        encoder: EmbeddingEncoder,
        differ: SnapshotDiffer,
        memory: SnapshotMemory,
        prompt_builder: CompressedPromptBuilder,
        predictive: "PredictiveService | None" = None,  # NEW
    ) -> None: ...

    async def process(self, snapshot: WorldSnapshot) -> CompressedPrompt:
        # ... existing logic ending in:
        await self._memory.store(snapshot_id, embedding, payload)

        # NEW — strictly after store, before prompt build:
        if self._predictive is not None:
            try:
                await self._predictive.on_snapshot_embedded(
                    snapshot_id=snapshot_id,
                    observed_at=snapshot.produced_at,
                    observed_embedding=embedding,
                )
            except Exception:  # noqa: BLE001 — must not break the pipeline
                log.exception("prediction.callback_failed", snapshot_id=snapshot_id)

        return await self._prompt_builder.build(...)
```

Rationale:

- Storing the observation **before** prediction guarantees the next window always contains the real observation, even on predictor failure.
- A blanket `except` here is the only allowed instance: prediction is non-critical. The exception is logged with full traceback and counted on a meta-event (see §6).

---

## 5. Daemon wiring

In `src/coremind/core/daemon.py`, during `start()`:

```python
self._prediction_service: PredictiveService | None = None
if self._config.prediction.enabled:
    persistence = JoblibPersistence(root=self._config.prediction.model_dir)
    trainer = BaselineTrainer(
        window=self._config.prediction.history_window,
        horizon=self._config.prediction.horizon_steps,
        metric=self._config.prediction.divergence_metric,
        holdout_fraction=self._config.prediction.holdout_fraction,
    )
    initial = await persistence.load(version=None)
    self._prediction_service = PredictiveService(
        predictor=initial,
        trainer=trainer,
        history=EmbeddingHistory(
            memory=self._snapshot_memory,
            window=self._config.prediction.history_window,
        ),
        persistence=persistence,
        event_bus=self._event_bus,
        signer=self._signer,
        backend=self._config.prediction.backend,
        anomaly_threshold=self._config.prediction.anomaly_threshold,
        min_training_samples=self._config.prediction.min_training_samples,
    )
    self._encoding_pipeline = WorldEncodingPipeline(
        ..., predictive=self._prediction_service
    )
```

Background retrain task:

```python
async def _retrain_prediction_loop(self) -> None:
    interval = self._config.prediction.retrain_interval_hours * 3600.0
    while not self._shutdown.is_set():
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
        except TimeoutError:
            if self._prediction_service is not None:
                try:
                    await self._prediction_service.retrain()
                except Exception:
                    log.exception("prediction.retrain.failed")
```

Register with the same pattern as `_prune_snapshot_embeddings`. The task is cancelled cleanly on shutdown.

---

## 6. Spec & schema updates

### 6.1 `spec/worldevent.schema.json`

Add a payload definition under `$defs`:

```json
"AnomalyDetectedPayload": {
  "type": "object",
  "required": ["snapshot_id", "observed_at", "divergence", "backend", "model_version", "horizon_steps", "history_window"],
  "additionalProperties": false,
  "properties": {
    "snapshot_id": {"type": "string"},
    "observed_at": {"type": "string", "format": "date-time"},
    "divergence": {
      "type": "object",
      "required": ["metric", "value", "threshold"],
      "properties": {
        "metric": {"enum": ["cosine", "l2"]},
        "value": {"type": "number", "minimum": 0.0},
        "threshold": {"type": "number", "minimum": 0.0}
      }
    },
    "horizon_steps": {"type": "integer", "minimum": 1},
    "history_window": {"type": "integer", "minimum": 2},
    "backend": {"enum": ["baseline", "jepa"]},
    "model_version": {"type": "string"},
    "top_drifting_entities": {
      "type": "array", "items": {"type": "string"}
    }
  }
}
```

Reference it conditionally on `attribute == "anomaly.detected"` via the existing `oneOf` mechanism used by other event families.

### 6.2 `spec/audit_log.md`

Add a short entry to the event-types table:

```md
| `anomaly.detected` | L2.5 PredictiveService | Predicted-vs-observed embedding divergence exceeded the calibrated threshold. Always signed. |
```

### 6.3 Pipeline meta-event on failure

When the `except` branch in `pipeline.process` fires, emit a `meta.prediction_error` event (unsigned is fine for meta-events — they are observability data). Reuse the existing meta-event infrastructure described in the EventBus docs; do not invent a new path.

---

## 7. Tests

### 7.1 `tests/prediction/test_service.py`

- Untrained predictor → `on_snapshot_embedded` returns `None`, no event published.
- Trained predictor, observed embedding equal to predicted → returns `Divergence` with `is_anomaly=False`, no event published.
- Trained predictor, observed embedding far from predicted → returns `Divergence` with `is_anomaly=True`, exactly one `WorldEventRecord(attribute="anomaly.detected")` published, payload validates against `worldevent.schema.json`.
- `retrain()` is serialized by the lock: two concurrent calls run sequentially.
- `reload_from_disk` survives `ModelArtifactError` (predictor stays `None`, error logged).

### 7.2 `tests/prediction/test_pipeline_integration.py`

- Build a `WorldEncodingPipeline` with a real `PredictiveService` backed by a stub predictor whose `divergence()` returns `value=0.5, threshold=0.15`.
- Feed it a snapshot.
- Assert: `_memory.store` called once; `event_bus.publish` called once with an `anomaly.detected` `WorldEventRecord` whose signature verifies.
- Assert: when the stub predictor raises, `pipeline.process` still returns a valid `CompressedPrompt` and a `meta.prediction_error` was emitted.

### 7.3 `tests/prediction/test_retrain_task.py`

- Patch `time` via an injected clock (no `asyncio.sleep`).
- Run the retrain loop for two ticks; assert `Trainer.train()` called twice.
- Cancel the shutdown event; assert the loop exits within 100 ms.

---

## 8. Integration

After this sub-phase:

- A fresh daemon with `prediction.enabled = true` boots, finds no model, runs without anomalies.
- After `coremind prediction train` (or the first scheduled retrain), the next snapshot whose divergence exceeds the calibrated threshold produces a signed event visible in `~/.coremind/audit.log`.
- L4/L5 see the event on the bus but do not yet react specially — that lands in 3.5E.

Manual smoke recipe (documentation only):

```bash
just dev-up
coremind run &   # default config has prediction.enabled=false → no change
# Edit ~/.coremind/config.toml:
#   [prediction]
#   enabled = true
coremind prediction train
# Wait one cycle, inject a synthetic anomaly via a plugin event.
coremind prediction info     # placeholder until 3.5E
tail -n 5 ~/.coremind/audit.log | jq 'select(.attribute=="anomaly.detected")'
```

---

## 9. Success criteria

- [ ] With `prediction.enabled = false`, `just test` is byte-identical in behavior to before this sub-phase.
- [ ] With `prediction.enabled = true` and no model, the daemon runs and logs `prediction.skipped reason=untrained` once per cycle, no events emitted.
- [ ] `worldevent.schema.json` validates an emitted `anomaly.detected` payload (round-trip via `jsonschema`).
- [ ] All signatures on emitted events verify with the daemon's public key.
- [ ] `mypy --strict` and `ruff` clean.
- [ ] All new tests pass; pipeline integration test asserts both happy and failure paths.

---

## 10. Out of scope

- Salience scoring, L4 `force_cycle`, L5 prompt update (3.5E).
- Backend selection logic for JEPA (3.5D).
- CLI subcommands beyond `info` and `train` (3.5E).
- `top_drifting_entities` attribution — emitted empty for now.
- Online learning (continual updates on every snapshot).
