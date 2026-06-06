# Phase 3.5D — True JEPA Backend

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_5_JEPA_PREDICTION.md](PHASE_3_5_JEPA_PREDICTION.md)
**Prerequisites:** [PHASE_3_5A_FOUNDATIONS.md](PHASE_3_5A_FOUNDATIONS.md), [PHASE_3_5C_PIPELINE_AND_ANOMALY_EVENTS.md](PHASE_3_5C_PIPELINE_AND_ANOMALY_EVENTS.md)
**Estimated effort:** 6–8 hours

---

## 1. Goal

Replace the LightGBM baseline with a faithful (small-scale) **Joint Embedding Predictive Architecture** over snapshot-embedding sequences:

- A **context encoder** (`f_θ`) — 2-layer Transformer over the visible portion of an embedding window.
- A **target encoder** (`f_ξ`) — EMA copy of `f_θ`, stop-gradient. Momentum τ = 0.996 (V-JEPA default).
- A **predictor** (`g_φ`) — 2-layer MLP that maps `f_θ(context) + position queries` to predicted target representations.
- **Masked self-supervised training** with V-JEPA-style spatio-temporal masks adapted to 1-D temporal sequences.
- **Smooth-L1 loss in latent space** between `g_φ(...)` and `f_ξ(target)` (stop-gradient on target).
- Selectable at runtime via `PredictionConfig.backend = "jepa"`; baseline remains available.

The backend must run on CPU within the same latency budget as the baseline (p95 < 100 ms inference, training < 30 min for 1 000 snapshots).

References: I-JEPA ([arXiv:2301.08243](https://arxiv.org/abs/2301.08243)), V-JEPA 2 ([arXiv:2506.09985](https://arxiv.org/abs/2506.09985)), LeCun *A Path Towards AMI* ([OpenReview BZ5a1r-kVsf](https://openreview.net/forum?id=BZ5a1r-kVsf)).

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/prediction/jepa.py` | `JEPAPredictor` implementing `TemporalPredictor`. |
| `src/coremind/prediction/encoder_ema.py` | EMA helper + stop-gradient utilities. |
| `src/coremind/prediction/masking.py` | Temporal mask generators (predict-future + block). |
| `src/coremind/prediction/jepa_modules.py` | `nn.Module` definitions (encoder, predictor). |
| `src/coremind/prediction/training.py` | Extend with `JEPATrainer`. |
| `src/coremind/prediction/persistence.py` | Extend `JoblibPersistence` with a `TorchStateDictPersistence` sibling, or add backend branch. |
| `src/coremind/prediction/service.py` | Backend dispatch in factory; no API change. |
| `src/coremind/cli/prediction_commands.py` | `train --backend jepa`; new `compare` subcommand. |
| `pyproject.toml` | Ensure `prediction-jepa = ["torch>=2.2.0"]` extra exists (added in 3.5A). |
| `tests/prediction/test_masking.py` | Mask shape & coverage. |
| `tests/prediction/test_jepa_modules.py` | Forward-pass shapes; EMA update math. |
| `tests/prediction/test_jepa_trainer.py` | Loss decreases over epochs on synthetic data. |
| `tests/prediction/test_jepa_predictor.py` | Inference contract & latency budget. |
| `tests/prediction/test_compare.py` | Baseline-vs-JEPA on held-out tail. |

---

## 3. Architecture

### 3.1 Notation

Let `W = history_window` (default 6), `H = horizon_steps` (default 1), `D_in = 768` (embedding dim from upstream), `D_hidden = 384`, `nhead = 4`, `n_layers = 2`.

An input sample is a sequence `S ∈ R^(W+H × D_in)`. We split it into:

- **Context** `C ⊆ S` — visible to `f_θ`.
- **Targets** `T = S \ C` — visible to `f_ξ` only (stop-gradient).

### 3.2 Masking — `prediction/masking.py`

Two mask kinds, sampled per training example:

1. **predict-future mask** (always present): the last `H` timesteps are targets, the first `W` are context. This is the inference-time configuration.
2. **block mask** (added stochastically with `p_block = 0.5` during training): one contiguous interior block of length `k ∈ {1, 2}` selected uniformly from `[1, W-2]` is *additionally* added to the target set. The context shrinks accordingly. Block masks teach the model bidirectional latent dynamics without ever shifting the prediction objective.

```python
import torch


def make_temporal_mask(
    *,
    window: int,
    horizon: int,
    block_prob: float = 0.5,
    rng: torch.Generator | None = None,
) -> tuple[torch.BoolTensor, torch.BoolTensor]:
    """Return (context_mask, target_mask) of shape (window+horizon,).

    True == position is in that set. Disjoint. Future positions are always
    in target_mask.
    """
```

Inference-time mask is the deterministic `block_prob = 0.0` variant.

### 3.3 Modules — `prediction/jepa_modules.py`

```python
import torch
from torch import nn


class PositionalEncoding(nn.Module):
    """Standard sinusoidal PE for length window+horizon."""


class ContextEncoder(nn.Module):
    """Linear projection 768→384, +PE, 2 Transformer encoder layers, return per-step 384-d."""

    def __init__(
        self,
        *,
        d_in: int = 768,
        d_hidden: int = 384,
        nhead: int = 4,
        n_layers: int = 2,
        dropout: float = 0.1,
    ) -> None: ...

    def forward(self, x: torch.Tensor, key_padding_mask: torch.BoolTensor) -> torch.Tensor:
        """x: (B, L, 768). Returns (B, L, 384). Masked positions are zeroed at input."""


class Predictor(nn.Module):
    """2-layer MLP: (384 + position-id-embedding) -> 768.

    Receives the context encoder's output at the LAST visible position (or
    a pooled context) plus a learnable per-target-step embedding, returns
    a predicted 768-d vector for that target step.
    """

    def __init__(self, *, d_ctx: int = 384, d_pos: int = 64, d_out: int = 768) -> None: ...

    def forward(
        self, ctx_summary: torch.Tensor, target_positions: torch.LongTensor
    ) -> torch.Tensor:
        """Returns (B, len(target_positions), 768)."""
```

Context summary policy (simple and verifiably effective at this scale): mean-pool the encoder outputs over **visible** positions only (`(B, 384)`). The predictor concatenates the pooled vector with each per-target-step positional embedding.

### 3.4 EMA target encoder — `prediction/encoder_ema.py`

```python
import torch

@torch.no_grad()
def ema_update(
    student: torch.nn.Module, teacher: torch.nn.Module, *, momentum: float
) -> None:
    """teacher = momentum * teacher + (1 - momentum) * student, parameter-wise."""


def stop_grad(t: torch.Tensor) -> torch.Tensor:
    return t.detach()
```

The target encoder is created as `copy.deepcopy(context_encoder)`, with all parameters set to `requires_grad_(False)`. `ema_update(student=ctx_enc, teacher=tgt_enc, momentum=0.996)` runs after every optimizer step.

### 3.5 `JEPAPredictor` — `prediction/jepa.py`

```python
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import torch

from coremind.prediction.base import TemporalPredictor
from coremind.prediction.errors import UntrainedPredictorError
from coremind.prediction.jepa_modules import ContextEncoder, Predictor
from coremind.prediction.metrics import cosine_distance, l2_distance
from coremind.prediction.schemas import Divergence, PredictedState, PredictorState


@dataclass
class JEPAPredictor:
    backend: Literal["jepa"] = "jepa"
    window: int = 6
    horizon: int = 1
    metric: Literal["cosine", "l2"] = "cosine"
    threshold: float = 0.15
    model_version: str | None = None

    context_encoder: ContextEncoder | None = None
    predictor: Predictor | None = None
    device: str = "cpu"

    async def predict(self, history) -> PredictedState: ...
    async def divergence(self, predicted, observed) -> Divergence: ...
    async def state(self) -> PredictorState: ...
```

Inference is single-threaded on CPU. Wrap the forward pass in `asyncio.to_thread(self._infer, history)`. Use `torch.inference_mode()` and `model.eval()`. Default `torch.set_num_threads(1)` to avoid contention with the daemon's event loop.

### 3.6 `JEPATrainer` — extension to `prediction/training.py`

```python
class JEPATrainer:
    def __init__(
        self,
        *,
        window: int,
        horizon: int = 1,
        metric: Literal["cosine", "l2"] = "cosine",
        holdout_fraction: float = 0.1,
        # Architecture
        d_hidden: int = 384,
        nhead: int = 4,
        n_layers: int = 2,
        dropout: float = 0.1,
        # Optimization
        epochs: int = 50,
        batch_size: int = 32,
        lr: float = 3e-4,
        weight_decay: float = 1e-2,
        warmup_epochs: int = 5,
        ema_momentum: float = 0.996,
        # Early stopping
        patience: int = 5,
        min_delta: float = 1e-4,
        # Reproducibility
        seed: int = 42,
    ) -> None: ...

    async def train(
        self, history: tuple[tuple[float, ...], ...]
    ) -> tuple[JEPAPredictor, TrainingMetrics]: ...
```

Training loop (pseudocode):

```text
require len(history) >= 100 (per phase non-goal; recommended >= 500)
build sliding windows of length (W + H)
time-ordered split: last holdout_fraction → val
build student (context_encoder + predictor), teacher (deepcopy of context_encoder, requires_grad=False)
optimizer = AdamW(student.parameters() + predictor.parameters(), lr, weight_decay)
scheduler = cosine with warmup
loss_fn = smooth_l1_loss (beta = 1.0)

for epoch in range(epochs):
    for batch in DataLoader(train, batch_size, shuffle=True):
        ctx_mask, tgt_mask = sample masks
        ctx_in = batch * ctx_mask (set masked positions to learnable mask token or zero)
        ctx_summary = student.context_encoder(ctx_in, key_padding_mask=~ctx_mask)
        ctx_pool = masked_mean(ctx_summary, ctx_mask)
        target_positions = positions where tgt_mask is True
        predicted = student.predictor(ctx_pool, target_positions)   # (B, K, 768)
        with torch.no_grad():
            target = teacher(batch).gather(target_positions)        # (B, K, 768)
        loss = smooth_l1_loss(predicted, target)
        loss.backward()
        optimizer.step(); optimizer.zero_grad()
        scheduler.step()
        ema_update(student.context_encoder, teacher, momentum=ema_momentum)

    val_loss = evaluate(val)
    if not improved by min_delta for patience epochs: break

calibrate threshold from val cosine divergences (same logic as baseline trainer)
build JEPAPredictor with frozen weights
```

Notes:

- **Stop-gradient on target is enforced by `requires_grad=False` + `torch.no_grad()`.** This is the single most important property to verify in tests.
- **No collapse safeguard yet.** Collapse risk is mitigated by (a) using a real upstream encoder (the targets are non-degenerate by construction) and (b) the EMA momentum. We do not implement VICReg-style variance/covariance penalties in v1; if collapse is observed, follow-up adds them.
- **Determinism.** Fix seeds in `torch`, `numpy`, `random`. Set `torch.use_deterministic_algorithms(True)` in tests only.

### 3.7 Persistence

`JoblibPersistence` is unsuitable for `torch.nn.Module` (joblib pickles work but are fragile across versions). Add a sibling:

```python
class TorchStateDictPersistence(PredictorPersistence):
    def __init__(self, root: Path) -> None: ...
    async def save(self, predictor: JEPAPredictor) -> str: ...
    async def load(self, version: str | None = None) -> JEPAPredictor | None: ...
```

Disk layout:

```text
~/.coremind/models/prediction/jepa/<version>/
  context_encoder.pt
  predictor.pt
  meta.json    # schema_version, window, horizon, metric, threshold, d_hidden, nhead, n_layers, trained_at, torch_version
  latest -> <version>
```

`service.py` picks the persistence by `cfg.backend`. The `coremind prediction info` output indicates which backend is active and which versions exist for each.

### 3.8 Backend dispatch

`PredictiveService` constructor takes `predictor` and `trainer` already. The factory in `daemon.start()` selects:

```python
match cfg.backend:
    case "baseline":
        persistence = JoblibPersistence(root=cfg.model_dir / "baseline")
        trainer = BaselineTrainer(...)
    case "jepa":
        persistence = TorchStateDictPersistence(root=cfg.model_dir / "jepa")
        trainer = JEPATrainer(...)
```

No behavioral change at the `PredictiveService` level. The `Divergence` and `AnomalyRecord` carry `backend` so consumers can distinguish.

---

## 4. CLI additions

```bash
coremind prediction train --backend jepa [--limit N] [--epochs N]
coremind prediction compare [--backend-a baseline] [--backend-b jepa] [--holdout-fraction 0.1]
```

`compare`:

1. Trains both backends on the same head split.
2. Evaluates both on the same tail split.
3. Prints a side-by-side table: train loss, val loss, calibrated threshold, mean cosine divergence on val, p95 inference latency.
4. Returns non-zero exit if either fails.

---

## 5. Tests

### 5.1 `tests/prediction/test_masking.py`

- `context_mask` and `target_mask` are disjoint and cover `window+horizon`.
- The last `horizon` positions are always in `target_mask`.
- With `block_prob=0.0`, the interior block is never added.
- With `block_prob=1.0`, exactly one interior block exists, length ∈ {1, 2}.
- Determinism: same `rng` seed → identical masks.

### 5.2 `tests/prediction/test_jepa_modules.py`

- Forward shapes: `ContextEncoder((2, 6, 768))` → `(2, 6, 384)`.
- `Predictor((2, 384), positions=tensor([6]))` → `(2, 1, 768)`.
- EMA update: after one step with `momentum=0.0`, teacher == student. With `momentum=1.0`, teacher unchanged. With `0.99`, teacher param close but not equal.
- Target encoder parameters have `requires_grad == False`.

### 5.3 `tests/prediction/test_jepa_trainer.py`

- Fewer than 50 samples → `InsufficientHistoryError`.
- Training on the synthetic 300-snapshot fixture decreases validation loss from epoch 1 to epoch 10 by at least 20 %.
- After training, all `teacher` parameters have `grad_fn is None` (stop-gradient invariant).
- Calibrated threshold is finite, in `[0.05, 0.5]`.
- Setting `torch.use_deterministic_algorithms(True)` + same seed → identical final `val_loss`.

### 5.4 `tests/prediction/test_jepa_predictor.py`

- Untrained instance → `UntrainedPredictorError`.
- Wrong-length history → `ValueError`.
- Trained model returns a 768-d tuple.
- Microbench (skipped under `pytest -m slow` by default but available): 100 inference calls; p95 < 100 ms on CPU. Test asserts `p95_ms < 100`.

### 5.5 `tests/prediction/test_compare.py`

- On the synthetic fixture seeded for predictable structure, `JEPATrainer` achieves cosine divergence on the val tail ≤ 0.9 × baseline.
- The comparison harness produces a stable table layout (golden-test the CLI output minus timing fields).

---

## 6. Integration

After this sub-phase the daemon supports either backend via a single config flip. No pipeline code changes (the dispatch lives in the factory). `worldevent.schema.json` already accepts `backend: "jepa"` from 3.5C.

Recommended rollout:

1. Keep `backend = "baseline"` in production for one week of dogfood.
2. Run `coremind prediction compare` weekly; collect five wins before promoting.
3. Flip `backend = "jepa"` only after the acceptance criterion in §7 is met on real data.

---

## 7. Success criteria

- [ ] All new tests pass; `mypy --strict` and `ruff` clean.
- [ ] On real history of ≥ 500 snapshots, JEPA val cosine divergence beats baseline by ≥ 10 %.
- [ ] Inference p95 < 100 ms on CPU, same window size.
- [ ] Stop-gradient invariant verified by test.
- [ ] Persistence round-trip preserves prediction equality to 1e-5 (single precision).
- [ ] `coremind prediction compare` runs end-to-end and prints a tabular report.
- [ ] `~/.coremind/models/prediction/jepa/` contains a valid versioned artifact after `coremind prediction train --backend jepa`.

---

## 8. Open questions (revisit before promoting to default)

- **Encoder retraining.** The fixed upstream encoder means `f_θ` and `f_ξ` operate on already-frozen 768-d vectors. A future iteration could train `f_θ` directly on tokenized snapshots (entity tuples) to recover the canonical I-JEPA setup. Out of scope for v1.
- **Collapse risk.** If empirical training shows representation collapse (uniformly low loss, divergence stuck near zero), add VICReg-style variance and covariance regularization terms.
- **H-JEPA.** A second timescale (one-hour horizon stacked on the existing one-cycle horizon) is the natural Phase 6 extension.

---

## 9. Out of scope

- Action-conditioned prediction (V-JEPA 2 stage 2).
- Hierarchical / multi-timescale prediction (H-JEPA).
- Training `f_θ` from raw snapshots (encoder remains fixed in v1).
- GPU support.
- Distributed or multi-host training.
- VICReg / Barlow / BYOL collapse regularizers.
