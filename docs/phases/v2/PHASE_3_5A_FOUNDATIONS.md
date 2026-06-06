# Phase 3.5A — Foundations

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_5_JEPA_PREDICTION.md](PHASE_3_5_JEPA_PREDICTION.md)
**Prerequisites:** Phase 3 complete (`SnapshotMemory` populated; `EmbeddingEncoder` operational)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Lay the typed foundations for the predictive layer without changing runtime behavior. After this sub-phase:

- The `coremind.prediction` package exists with stable Protocols and Pydantic schemas.
- A history-window read API is available on top of `SnapshotMemory`.
- A `PredictionConfig` block can be loaded from `~/.coremind/config.toml`.
- A `prediction` optional dependency extra installs the ML stack on demand.
- All new modules pass `mypy --strict` and `ruff check` with zero warnings.

No predictor implementation lands here. No daemon wiring. No CLI. Tests cover schemas, history slicing, and config loading only.

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/prediction/__init__.py` | Public re-exports (Protocols + schemas + errors). |
| `src/coremind/prediction/schemas.py` | Extend existing module with new frozen Pydantic models. |
| `src/coremind/prediction/base.py` | `TemporalPredictor`, `Trainer`, `PredictorPersistence` Protocols. |
| `src/coremind/prediction/history.py` | `EmbeddingHistory` — ordered window over `SnapshotMemory`. |
| `src/coremind/prediction/errors.py` | Exception hierarchy under `coremind.errors.CoreMindError`. |
| `src/coremind/config.py` | Add `PredictionConfig` and embed in `DaemonConfig`. |
| `pyproject.toml` | Add `[project.optional-dependencies] prediction = [...]`. |
| `tests/prediction/__init__.py` | Empty package. |
| `tests/prediction/test_schemas.py` | Frozen-ness, serialization, validation. |
| `tests/prediction/test_history.py` | Ordering, windowing, insufficient-history error. |
| `tests/prediction/test_config.py` | TOML round-trip with defaults. |
| `tests/prediction/conftest.py` | Shared fixtures (fake `SnapshotMemory`, synthetic embeddings). |

---

## 3. Data model

All new models are **frozen** Pydantic v2 `BaseModel`s, with explicit `model_config = ConfigDict(frozen=True, extra="forbid")`. Vectors are `tuple[float, ...]` (hashable & immutable) of length `EMBEDDING_DIM = 768`.

### 3.1 `prediction/schemas.py` (additions)

The module already declares `Prediction` and `PredictionEvidence` (do not delete). Add:

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EMBEDDING_DIM: int = 768


class PredictedState(BaseModel):
    """A predictor's output for a single target timestep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    embedding: tuple[float, ...] = Field(..., description="Predicted 768-d vector.")
    horizon_steps: int = Field(default=1, ge=1, description="How many steps ahead.")
    backend: Literal["baseline", "jepa"] = "baseline"
    model_version: str = Field(..., description="Persisted model version tag.")
    produced_at: datetime


class Divergence(BaseModel):
    """Comparison between predicted and observed embeddings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: Literal["cosine", "l2"]
    value: float = Field(..., ge=0.0)
    threshold: float = Field(..., ge=0.0)

    @property
    def is_anomaly(self) -> bool:
        return self.value > self.threshold


class AnomalyRecord(BaseModel):
    """Structured payload for an `anomaly.detected` WorldEventRecord."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    observed_at: datetime
    divergence: Divergence
    horizon_steps: int
    history_window: int
    backend: Literal["baseline", "jepa"]
    model_version: str
    top_drifting_entities: tuple[str, ...] = Field(
        default=(), description="Best-effort attribution (Phase 3.5E, empty in 3.5C)."
    )


class TrainingMetrics(BaseModel):
    """Returned by a `Trainer.train()` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["baseline", "jepa"]
    model_version: str
    samples: int
    epochs: int = 1
    train_loss: float
    val_loss: float | None = None
    calibrated_threshold: float
    duration_seconds: float
    trained_at: datetime


class PredictorState(BaseModel):
    """Runtime status for `coremind prediction status` and the dashboard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["baseline", "jepa"]
    enabled: bool
    trained: bool
    model_version: str | None
    samples_in_history: int
    last_training: TrainingMetrics | None
    last_divergence: Divergence | None
    recent_anomaly_count: int = 0
```

### 3.2 `prediction/base.py` — Protocols

```python
from typing import Protocol, runtime_checkable

from coremind.prediction.schemas import (
    Divergence,
    PredictedState,
    PredictorState,
    TrainingMetrics,
)


@runtime_checkable
class TemporalPredictor(Protocol):
    """A predictor over a fixed-length window of embeddings."""

    backend: str
    model_version: str | None

    async def predict(
        self, history: tuple[tuple[float, ...], ...]
    ) -> PredictedState:
        """Predict the next embedding from an ordered window (oldest → newest)."""

    async def divergence(
        self,
        predicted: tuple[float, ...],
        observed: tuple[float, ...],
    ) -> Divergence:
        """Compute divergence using the predictor's configured metric & threshold."""

    async def state(self) -> PredictorState: ...


@runtime_checkable
class Trainer(Protocol):
    """Trains a `TemporalPredictor` from historical embeddings."""

    async def train(
        self,
        history: tuple[tuple[float, ...], ...],
    ) -> TrainingMetrics: ...


@runtime_checkable
class PredictorPersistence(Protocol):
    """Save / load model artifacts under `~/.coremind/models/prediction/`."""

    async def save(self, predictor: TemporalPredictor) -> str:
        """Returns the new model_version tag."""

    async def load(self, version: str | None = None) -> TemporalPredictor | None:
        """Returns `None` when no artifact is found."""
```

### 3.3 `prediction/errors.py`

```python
from coremind.errors import CoreMindError


class PredictionError(CoreMindError):
    """Base class for all prediction-layer errors."""


class UntrainedPredictorError(PredictionError):
    """Raised when `predict` is called before any successful training."""


class InsufficientHistoryError(PredictionError):
    """Raised when fewer than `window` embeddings are available."""


class ModelArtifactError(PredictionError):
    """Raised on corrupt / version-mismatched model files."""
```

### 3.4 `prediction/history.py`

`EmbeddingHistory` wraps `SnapshotMemory` (read-only) and exposes a strict ordering API.

```python
from collections.abc import Sequence

from coremind.prediction.errors import InsufficientHistoryError
from coremind.world.snapshot_memory import SnapshotMemory


class EmbeddingHistory:
    """Ordered, fixed-size view over recently stored snapshot embeddings.

    Wraps `SnapshotMemory` rather than mutating it. All reads return tuples
    so vectors stay immutable end-to-end.
    """

    def __init__(self, memory: SnapshotMemory, *, window: int) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self._memory = memory
        self._window = window

    async def window(self) -> tuple[tuple[float, ...], ...]:
        """Return the last `window` embeddings (oldest → newest)."""

    async def all_for_training(
        self, *, limit: int | None = None
    ) -> tuple[tuple[float, ...], ...]:
        """Return up to `limit` embeddings in chronological order."""

    async def count(self) -> int: ...
```

Implementation notes:

- Both reads must guarantee chronological order. `SnapshotMemory` stores `produced_at` on the payload; sort by it.
- `window()` raises `InsufficientHistoryError` when fewer than `self._window` embeddings exist.
- `all_for_training()` returns an empty tuple when the store is empty (no error).
- No new Qdrant collection. No new schema. Read-only adapter.

---

## 4. Configuration

### 4.1 Add `PredictionConfig` to `src/coremind/config.py`

Mirror the style of `EmbeddingPipelineConfig` (the existing template; do not duplicate its fields):

```python
class PredictionConfig(BaseModel):
    """Configuration for the L2.5 predictive layer."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: Literal["baseline", "jepa"] = "baseline"

    # Pipeline
    history_window: int = Field(default=6, ge=2, le=64)
    horizon_steps: int = Field(default=1, ge=1, le=8)
    divergence_metric: Literal["cosine", "l2"] = "cosine"
    anomaly_threshold: float = Field(default=0.15, ge=0.0, le=2.0)

    # Training
    min_training_samples: int = Field(default=50, ge=10)
    retrain_interval_hours: float = Field(default=24.0, gt=0.0)
    holdout_fraction: float = Field(default=0.1, ge=0.0, lt=0.5)

    # Persistence
    model_dir: Path = Field(
        default_factory=lambda: Path.home() / ".coremind" / "models" / "prediction"
    )
```

Embed in `DaemonConfig`:

```python
class DaemonConfig(BaseModel):
    ...
    embedding_pipeline: EmbeddingPipelineConfig = Field(default_factory=...)
    prediction: PredictionConfig = Field(default_factory=PredictionConfig)
```

### 4.2 TOML section (documentation only — not added to default config)

```toml
[prediction]
enabled = false
backend = "baseline"
history_window = 6
horizon_steps = 1
divergence_metric = "cosine"
anomaly_threshold = 0.15
min_training_samples = 50
retrain_interval_hours = 24.0
holdout_fraction = 0.1
model_dir = "~/.coremind/models/prediction"
```

---

## 5. Packaging

Add an optional extra so the ML stack does not bloat the core install:

```toml
# pyproject.toml
[project.optional-dependencies]
prediction = [
    "lightgbm>=4.0.0",
    "scikit-learn>=1.3.0",
    "joblib>=1.3.0",
]
prediction-jepa = [
    "torch>=2.2.0",
]
```

`torch` lives in its own extra (`prediction-jepa`) so contributors who only need the baseline are not forced into a >2 GB download. The default install (`uv pip install -e .`) gains nothing new.

---

## 6. Tests

Place under `tests/prediction/`. All async tests use `@pytest.mark.asyncio`. No real Qdrant required; use a fake `SnapshotMemory` exposing `produced_at`-ordered vectors.

### 6.1 `tests/prediction/test_schemas.py`

- `PredictedState`, `Divergence`, `AnomalyRecord`, `TrainingMetrics`, `PredictorState` are all frozen (attempted mutation raises).
- `Divergence.is_anomaly` is `True` iff `value > threshold` (boundary case included).
- Round-trip `model_dump_json` → `model_validate_json` preserves equality.
- Wrong-dimension embedding tuples are accepted by the model (no length check; that is enforced at predictor boundary in 3.5B).

### 6.2 `tests/prediction/test_history.py`

- `window()` returns tuples in chronological order (oldest → newest).
- `window()` raises `InsufficientHistoryError` when store has fewer than N entries.
- `all_for_training()` returns empty tuple on empty store.
- `all_for_training(limit=10)` truncates to the **most recent** 10 in chronological order.
- Out-of-order inserts in the fake store are reordered on read.

### 6.3 `tests/prediction/test_config.py`

- Defaults round-trip through TOML.
- Invalid `history_window = 1` raises `ValidationError`.
- Invalid `backend = "transformer"` raises `ValidationError`.
- `model_dir` accepts `~/...` and expands it.

---

## 7. Integration

No daemon, pipeline, CLI, or dashboard changes in this sub-phase. The new package compiles, is imported nowhere outside its own tests, and exposes only Protocols + schemas + errors + config.

`src/coremind/prediction/__init__.py` re-exports:

```python
from coremind.prediction.base import PredictorPersistence, TemporalPredictor, Trainer
from coremind.prediction.errors import (
    InsufficientHistoryError,
    ModelArtifactError,
    PredictionError,
    UntrainedPredictorError,
)
from coremind.prediction.history import EmbeddingHistory
from coremind.prediction.schemas import (
    AnomalyRecord,
    Divergence,
    PredictedState,
    PredictorState,
    TrainingMetrics,
)

__all__ = [...]  # explicit list
```

---

## 8. Success criteria

- [ ] `coremind.prediction` imports cleanly with no optional deps installed.
- [ ] `mypy --strict src/coremind/prediction` reports zero issues.
- [ ] `ruff check src/coremind/prediction tests/prediction` reports zero issues.
- [ ] All tests in `tests/prediction/` pass; coverage on `prediction/history.py` ≥ 90 %.
- [ ] `DaemonConfig` loads with and without a `[prediction]` TOML section.
- [ ] No existing tests regress (`just test`).
- [ ] `pip install -e ".[prediction]"` succeeds in a clean venv.

---

## 9. Out of scope

- Any concrete predictor implementation (3.5B and 3.5D).
- Wiring into `WorldEncodingPipeline` (3.5C).
- Persistence to disk (3.5B).
- CLI / dashboard (3.5E).
- Mutating `SnapshotMemory` (Phase 3.5 is read-only on the store).
