# Phase 3.5B — Baseline Predictor (LightGBM)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_5_JEPA_PREDICTION.md](PHASE_3_5_JEPA_PREDICTION.md)
**Prerequisites:** [PHASE_3_5A_FOUNDATIONS.md](PHASE_3_5A_FOUNDATIONS.md)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Deliver a working, offline-trainable, CPU-only `TemporalPredictor` backend named **baseline**:

- Predicts the next 768-d snapshot embedding from a window of N past embeddings.
- Trains via gradient-boosted regression (LightGBM wrapped in `MultiOutputRegressor`).
- Calibrates `anomaly_threshold` automatically from divergence percentiles on a held-out tail.
- Persists to disk under `~/.coremind/models/prediction/baseline/<version>/` with version metadata.
- Exposes deterministic, vectorized divergence helpers (cosine, L2).

No runtime wiring yet — that lands in 3.5C. This sub-phase ends with a CLI-runnable `coremind prediction train` and `info` that operate offline against a Qdrant fixture.

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/prediction/baseline.py` | `LightGBMPredictor` implementing `TemporalPredictor`. |
| `src/coremind/prediction/training.py` | `BaselineTrainer` implementing `Trainer`; sliding-window dataset builder. |
| `src/coremind/prediction/persistence.py` | `JoblibPersistence` implementing `PredictorPersistence`. |
| `src/coremind/prediction/metrics.py` | Vectorized `cosine_distance`, `l2_distance`, percentile helpers. |
| `src/coremind/cli/prediction_commands.py` | New CLI module with `train` and `info` subcommands (unwired until 3.5E). |
| `src/coremind/cli/__init__.py` | Register the `prediction` Click group (unwired but discoverable). |
| `tests/prediction/test_metrics.py` | Distance math, NaN safety, equivalence with `scipy` reference. |
| `tests/prediction/test_baseline_predictor.py` | Train-then-predict on synthetic stable history; threshold sanity. |
| `tests/prediction/test_baseline_trainer.py` | Window construction, holdout split, threshold calibration. |
| `tests/prediction/test_persistence.py` | Save / load round-trip, version mismatch raises. |
| `tests/prediction/fixtures/synthetic_history.py` | Reproducible synthetic embedding stream + injected anomaly. |

---

## 3. Data shapes

A training example for window N predicting horizon h = 1:

- **Input X**: flat vector of shape `(N * 768,)` — the concatenation of the N past embeddings in chronological order.
- **Target y**: 768-d vector (the embedding at step t).

Dataset of T contiguous embeddings yields `T - N` examples. With `holdout_fraction = 0.1` we reserve the **most recent** 10 % for validation (no shuffling — the tail matters for drift detection).

---

## 4. Implementation

### 4.1 `prediction/metrics.py`

Pure functions, no async, no class state. All take and return `numpy.ndarray`.

```python
import numpy as np


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2]. NaN-safe (returns 1.0 on zero-norm)."""

def l2_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Euclidean distance, finite & non-negative."""

def percentile_threshold(
    divergences: np.ndarray, *, percentile: float = 95.0
) -> float:
    """Return the divergence value at the given percentile (clipped >= 0)."""
```

Notes:

- Reject `np.nan` / `np.inf` in inputs with `ValueError`; the predictor's caller passes clean vectors.
- Cosine: `1 - (a @ b) / (|a| * |b|)`. When either norm is zero, return `1.0` (maximum dissimilarity) — never raise.
- Vectorize for batches: callers pass `(K, D)` arrays for K candidates.

### 4.2 `prediction/baseline.py`

```python
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Literal

import numpy as np
from sklearn.multioutput import MultiOutputRegressor
import lightgbm as lgb

from coremind.prediction.base import TemporalPredictor
from coremind.prediction.errors import UntrainedPredictorError
from coremind.prediction.metrics import cosine_distance, l2_distance
from coremind.prediction.schemas import Divergence, PredictedState, PredictorState


@dataclass(slots=True)
class LightGBMPredictor:
    """Baseline predictor: per-dim LightGBM regressors over a flat window."""

    backend: Literal["baseline"] = "baseline"
    window: int = 6
    horizon: int = 1
    metric: Literal["cosine", "l2"] = "cosine"
    threshold: float = 0.15
    model_version: str | None = None
    _model: MultiOutputRegressor | None = None

    async def predict(
        self, history: tuple[tuple[float, ...], ...]
    ) -> PredictedState: ...

    async def divergence(
        self,
        predicted: tuple[float, ...],
        observed: tuple[float, ...],
    ) -> Divergence: ...

    async def state(self) -> PredictorState: ...
```

Behavior:

- `predict()`:
  - Validate `len(history) == self.window` and each row has length 768.
  - Stack into `np.ndarray(shape=(1, window*768))`.
  - Raise `UntrainedPredictorError` when `self._model is None`.
  - `embedding = self._model.predict(X)[0]` → cast to `tuple[float, ...]`.
  - Run on a thread: wrap in `asyncio.to_thread(self._model.predict, X)`.
- `divergence()`:
  - Convert to numpy; call `cosine_distance` or `l2_distance` per `self.metric`.
  - Return `Divergence(metric=..., value=..., threshold=self.threshold)`.
- `state()`:
  - Plain accessor — trained iff `_model is not None`.

### 4.3 `prediction/training.py`

```python
import numpy as np
from sklearn.multioutput import MultiOutputRegressor
import lightgbm as lgb

from coremind.prediction.baseline import LightGBMPredictor
from coremind.prediction.metrics import cosine_distance, percentile_threshold
from coremind.prediction.schemas import TrainingMetrics


class BaselineTrainer:
    def __init__(
        self,
        *,
        window: int,
        horizon: int = 1,
        metric: str = "cosine",
        holdout_fraction: float = 0.1,
        threshold_percentile: float = 95.0,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        num_leaves: int = 31,
        random_state: int = 42,
    ) -> None: ...

    async def train(
        self, history: tuple[tuple[float, ...], ...]
    ) -> tuple[LightGBMPredictor, TrainingMetrics]: ...
```

Algorithm:

1. Validate `len(history) >= window + 1 + ceil(window / (1 - holdout))` so both splits are non-empty. Otherwise raise `InsufficientHistoryError`.
2. Build sliding windows: `X[i] = concat(history[i:i+window])`, `y[i] = history[i+window+horizon-1]`. Shape `(T - window - horizon + 1, window*D)` and `(_, D)`.
3. Time-ordered split: last `holdout_fraction` of the rows → validation set.
4. Fit `MultiOutputRegressor(lgb.LGBMRegressor(n_estimators, learning_rate, num_leaves, random_state, n_jobs=-1, verbose=-1))` on the train split. Run in `asyncio.to_thread`.
5. Compute training loss = mean smooth-L1 over train, validation loss = mean smooth-L1 over val.
6. Compute per-row cosine divergence between predicted and observed on the **validation tail**; calibrate `threshold = percentile_threshold(divs, percentile=threshold_percentile)` clipped to `[0.05, 0.5]`.
7. Return a fully-populated `LightGBMPredictor` (with `_model` set and `model_version = f"baseline-{ts}-{git_sha[:7]}"`) and a `TrainingMetrics` instance.

Note: pure-Python sklearn `MultiOutputRegressor` trains 768 independent regressors. For 500 samples, single CPU, this fits in well under 60 seconds. If profiling shows this is excessive, swap for a single `lgb.LGBMRegressor` with `multi_output` not natively supported — defer to a follow-up.

### 4.4 `prediction/persistence.py`

Disk layout:

```text
~/.coremind/models/prediction/
  baseline/
    <model_version>/
      model.joblib          # the sklearn regressor
      meta.json             # version, window, horizon, metric, threshold, trained_at
    latest -> <model_version>   # symlink (or text file on non-POSIX)
```

API:

```python
from pathlib import Path

from coremind.prediction.baseline import LightGBMPredictor
from coremind.prediction.base import PredictorPersistence


class JoblibPersistence(PredictorPersistence):
    def __init__(self, root: Path) -> None: ...

    async def save(self, predictor: LightGBMPredictor) -> str: ...

    async def load(
        self, version: str | None = None
    ) -> LightGBMPredictor | None: ...
```

Notes:

- All I/O via `asyncio.to_thread`. The model file can reach ~50 MB.
- `meta.json` carries a `schema_version: int = 1` field. Loading a future schema raises `ModelArtifactError`.
- `load(version=None)` resolves `latest`. Returns `None` if no model exists yet.

### 4.5 CLI scaffolding (`prediction_commands.py`)

```python
import click

@click.group(name="prediction")
def prediction_cli() -> None:
    """Inspect and manage the L2.5 predictive layer."""


@prediction_cli.command("info")
def info_cmd() -> None:
    """Print backend, config, and latest model metadata."""


@prediction_cli.command("train")
@click.option("--backend", type=click.Choice(["baseline"]), default="baseline")
@click.option("--limit", type=int, default=None, help="Cap training samples.")
def train_cmd(backend: str, limit: int | None) -> None:
    """Train a fresh predictor from the daemon's snapshot history."""
```

Both commands instantiate the components from `DaemonConfig` loaded via the standard config path. `train` reads embeddings via `EmbeddingHistory(memory=SnapshotMemory(...), window=cfg.history_window).all_for_training(limit=limit)`. Other subcommands (`status`, `anomalies`, `compare`) are stubbed and ship in 3.5E.

Register the group in `cli/__init__.py`:

```python
from coremind.cli.prediction_commands import prediction_cli
cli.add_command(prediction_cli)
```

---

## 5. Tests

### 5.1 `tests/prediction/fixtures/synthetic_history.py`

Generates a reproducible stream of 300 embeddings:

- 768-d vectors from a deterministic random walk in a 16-d subspace (so per-dim regressors actually have signal).
- An "anomaly" helper to inject a hard step-change into the last K snapshots.

### 5.2 `tests/prediction/test_metrics.py`

- `cosine_distance` matches `scipy.spatial.distance.cosine` to 1e-6 on 100 random pairs.
- `l2_distance` matches `numpy.linalg.norm(a-b)`.
- Zero-norm input → `cosine_distance == 1.0`, no exception.
- `nan` input → `ValueError`.
- `percentile_threshold` agrees with `np.percentile`.

### 5.3 `tests/prediction/test_baseline_predictor.py`

- Predicting on untrained instance raises `UntrainedPredictorError`.
- After training on 200 synthetic snapshots, `predict(history[-6:])` returns a 768-d tuple within cosine distance < 0.10 of the next true vector (loose threshold; tightens after JEPA).
- `divergence()` returns a `Divergence` with `metric == cfg.metric`, `threshold == predictor.threshold`.
- Wrong-length history raises `ValueError`.

### 5.4 `tests/prediction/test_baseline_trainer.py`

- Fewer than `window + 2` samples → `InsufficientHistoryError`.
- After training, `metrics.calibrated_threshold` is finite, in `[0.05, 0.5]`, and `> 0`.
- Validation loss is finite and not orders of magnitude above training loss.
- Injecting a 6-snapshot anomaly tail produces validation divergences whose 99th percentile is well above the calibrated 95th-percentile threshold.

### 5.5 `tests/prediction/test_persistence.py`

- `save → load` returns an equivalent predictor (same `predict()` output to 1e-6).
- `load(version="does-not-exist")` returns `None`.
- Corrupting `meta.json` `schema_version` → `ModelArtifactError`.
- Concurrent `save()` calls (via `asyncio.gather`) produce distinct versions; `latest` resolves to the last completed.

---

## 6. Integration

This sub-phase still does not change the running daemon. After merge:

- `coremind prediction info` runs and prints `trained=False, model_version=None` on a clean install.
- `coremind prediction train` (with a populated `SnapshotMemory`) writes a model under `~/.coremind/models/prediction/baseline/<version>/` and prints `TrainingMetrics`.
- Re-running `info` shows the new `latest` version.

---

## 7. Success criteria

- [ ] `mypy --strict src/coremind/prediction` clean.
- [ ] `ruff check` clean across new modules and tests.
- [ ] Trainer succeeds end-to-end on the synthetic fixture (`pytest tests/prediction/test_baseline_trainer.py`).
- [ ] Predictor latency p95 < 50 ms per call on CPU for window = 6, dim = 768 (informal microbench in the test file with `time.perf_counter`).
- [ ] Persistence round-trip preserves prediction equality to 1e-6.
- [ ] `coremind prediction info|train` succeed in a manual run against a dev Qdrant.
- [ ] `just lint && just test` green.

---

## 8. Out of scope

- Wiring into `WorldEncodingPipeline` and event emission (3.5C).
- True JEPA backend (3.5D).
- Salience scoring, L4 reactions, dashboard (3.5E).
- Drift attribution (`top_drifting_entities`) — placeholder remains empty.
- Hyperparameter search; defaults are chosen to be safe, not optimal.
