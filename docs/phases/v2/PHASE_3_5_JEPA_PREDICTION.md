# Phase 3.5 — JEPA Prediction Layer

**Version:** 0.2 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code), reviewers

**Target:** CoreMind v2
**Duration estimate:** 1–2 weeks (split across 5 sub-phases)
**Prerequisites:** Phase 3 complete (embedding pipeline operational, `SnapshotMemory` populated)

> A French exploratory draft is preserved at [`PHASE_3_5_JEPA_PREDICTION.fr.md`](PHASE_3_5_JEPA_PREDICTION.fr.md). The English suite below supersedes it.

---

## Table of Contents

1. [Problem](#1-problem)
2. [Background — JEPA in 60 seconds](#2-background--jepa-in-60-seconds)
3. [CoreMind ↔ JEPA mapping](#3-coremind--jepa-mapping)
4. [Architecture](#4-architecture)
5. [Sub-phase roadmap](#5-sub-phase-roadmap)
6. [Non-goals (phase-wide)](#6-non-goals-phase-wide)
7. [Out of scope (deferred to later phases)](#7-out-of-scope-deferred-to-later-phases)
8. [Phase acceptance criteria](#8-phase-acceptance-criteria)
9. [References](#9-references)

---

## 1. Problem

Phase 3 gave CoreMind an efficient *retrospective* view of the world: `SnapshotDiffer` answers "what changed since the last cycle?" and `SnapshotMemory` answers "have I seen something like this before?". Neither answers the more useful question for an agent that *notices*: **"is what I observe what I should be observing right now?"**

Without prediction, every change is treated equally. A scheduled change (lights dim at 22:00, kettle on at 07:15) is indistinguishable from an unexpected one (fridge door left open, sensor unresponsive for 30 min). All anomaly signal is currently inferred by L4/L5 from raw text — which is expensive, slow, and unreliable on slow drifts.

Phase 3.5 adds a small predictive component that runs **inside the embedding pipeline** (no LLM in the hot path). It predicts the next snapshot embedding from a short history, compares it to the observed embedding, and — when divergence exceeds a calibrated threshold — emits a signed `anomaly.detected` `WorldEventRecord` onto the bus. L4 and L5 react via existing salience and event-driven cycle paths.

The framing is borrowed from Yann LeCun's *Joint Embedding Predictive Architecture* (JEPA): **predict in representation space, not in input space**. We ship two backends: a pragmatic LightGBM baseline (3.5B–3.5C) and a true JEPA implementation (3.5D) once the baseline has validated the pipeline.

---

## 2. Background — JEPA in 60 seconds

JEPA was introduced in LeCun's 2022 position paper *A Path Towards Autonomous Machine Intelligence* and instantiated in two follow-ups:

- **I-JEPA** (Assran et al., 2023) — images: predict the embedding of a masked target block from the embedding of a visible context block.
- **V-JEPA / V-JEPA 2** (Meta, 2024 / 2025) — video: predict the embedding of masked spatio-temporal regions; V-JEPA 2 adds action-conditioned prediction for robot control.

The shared recipe is:

1. A **context encoder** `f_θ` produces an embedding of the visible input.
2. A **predictor** `g_φ` maps that embedding (plus positional tokens for what to predict) to a predicted embedding of the unseen target.
3. A **target encoder** `f_ξ` produces the ground-truth embedding of the actual target. Its weights are an exponential moving average (EMA) of `f_θ` (stop-gradient through `f_ξ`).
4. The loss is computed in **embedding space** (smooth-L1, cosine, or similar) — **never in input space**.

Why it matters for CoreMind:

- **Latent-space loss** ignores the unpredictable details of a raw snapshot (a sensor's last-decimal jitter, the order of new emails) and focuses on the predictable structure (the daily routine, the room's thermal trajectory).
- **No generation** in the hot path: inference is a forward pass through a small encoder + predictor — measurable in tens of milliseconds on CPU.
- **Self-supervised**: we already collect the data needed for training (`SnapshotMemory` in Qdrant). No labels required.

We do not need image- or video-grade architectures. Our "frames" are 768-dimensional snapshot embeddings produced upstream by `EmbeddingEncoder`. The JEPA pieces (3.5D) are small: 2-layer Transformer encoders with hidden dim 384, an MLP predictor, and EMA on the target encoder.

---

## 3. CoreMind ↔ JEPA mapping

| JEPA concept (V-JEPA-style) | CoreMind analog (Phase 3.5) |
| --- | --- |
| Video frame | `WorldSnapshot` |
| Frame token | 768-d snapshot embedding from `EmbeddingEncoder` |
| Short video clip | Ordered history window of N snapshot embeddings (default N = 6) |
| Spatio-temporal mask | Temporal mask: hide the last K timesteps (predict-future) and optionally one middle block (predict-context) |
| Context encoder `f_θ` | Small Transformer over the visible (unmasked) embeddings |
| Target encoder `f_ξ` | EMA copy of `f_θ` (momentum τ = 0.996) |
| Predictor `g_φ` | 2-layer MLP that outputs the predicted target embedding |
| Latent-space loss | Smooth-L1 in 768-d (training); cosine distance (reporting & anomaly threshold) |
| Anomaly signal | `cosine_distance(predicted, observed) > threshold` → `anomaly.detected` event |
| Action conditioning (V-JEPA 2) | **Not implemented in Phase 3.5** — explicit non-goal |
| Hierarchical H-JEPA | **Not implemented in Phase 3.5** — explicit non-goal |

Two deliberate simplifications relative to canonical V-JEPA, both motivated by stability and scope:

- **Encoder is fixed.** Six other CoreMind components depend on `EmbeddingEncoder`'s output (semantic memory, similarity search, dashboard). Phase 3.5 does **not** retrain the encoder. JEPA's `f_θ` and `f_ξ` operate on top of those pre-computed embeddings as a temporal model, not as a visual encoder. This is documented and revisited in 3.5D §11 (Open Questions).
- **No action conditioning.** CoreMind's effectors (Phase 3) are slow, heterogeneous, and frequently human-in-the-loop. Action-conditioned prediction is interesting for autonomous robots; for now, observation-only prediction is sufficient to surface anomalies.

---

## 4. Architecture

```mermaid
flowchart TD
    subgraph L2["L2 — World Model"]
        SNAP[WorldSnapshot]
    end

    subgraph L25["L2.5 — Predictive Layer (NEW)"]
        ENC[EmbeddingEncoder<br/>768-d]
        MEM[(SnapshotMemory<br/>Qdrant)]
        HIST[EmbeddingHistory<br/>ordered window]
        PRED{TemporalPredictor<br/>baseline | jepa}
        DIV[divergence > threshold ?]
        EVT[AnomalyRecord]
    end

    subgraph BUS["EventBus"]
        SIGN[ed25519 sign<br/>publish]
    end

    subgraph CONSUMERS["L4 / L5 reaction"]
        L4[ReasoningLoop<br/>force_cycle 'anomaly']
        L5[IntentionLoop<br/>event-driven<br/>salience boost]
    end

    SNAP --> ENC
    ENC --> MEM
    ENC --> HIST
    HIST -- last N embeddings --> PRED
    PRED -- predicted vector --> DIV
    ENC -- observed vector --> DIV
    DIV -- yes --> EVT
    EVT --> SIGN
    SIGN --> L4
    SIGN --> L5
    SIGN --> MEM
```

Key properties:

- The predictor is **side-by-side** with the existing pipeline. When disabled or untrained it is a no-op; when enabled it adds one CPU-bound forward pass after `_memory.store(...)`.
- Anomaly events are **signed** by the daemon key (treated like any other internal emission) and **persisted to L2** so L4/L5 can recall them across cycles.
- No new external service. No GPU requirement. Local-only artifacts under `~/.coremind/models/prediction/`.

---

## 5. Sub-phase roadmap

The phase is split into five independent sub-phases following the established `PHASE_3A`–`PHASE_3E` pattern. Each sub-phase lands its own tests and passes `just lint && just test` before the next starts.

| Sub-phase | Focus | Prerequisites | Effort |
| --- | --- | --- | --- |
| [3.5A — Foundations](PHASE_3_5A_FOUNDATIONS.md) | `coremind.prediction` package skeleton, Protocols, config, deps. No runtime change. | None | 2–3 h |
| [3.5B — Baseline Predictor](PHASE_3_5B_BASELINE_PREDICTOR.md) | LightGBM regressor + trainer + persistence. Offline only. | 3.5A | 3–4 h |
| [3.5C — Pipeline & Anomaly Events](PHASE_3_5C_PIPELINE_AND_ANOMALY_EVENTS.md) | Wire `PredictiveService` into `WorldEncodingPipeline`, emit signed `anomaly.detected` events, schedule retrain. | 3.5A, 3.5B | 3–4 h |
| [3.5D — True JEPA](PHASE_3_5D_TRUE_JEPA.md) | PyTorch JEPA backend (context + EMA target encoder + predictor + masked SSL training). | 3.5A, 3.5C | 6–8 h |
| [3.5E — Reaction, CLI & Dashboard](PHASE_3_5E_REACTION_CLI_DASHBOARD.md) | Salience scoring, L4 `force_cycle`, `coremind prediction` CLI, `/prediction` dashboard. | 3.5C | 4–5 h |

**Parallelism:** 3.5D can be developed in parallel with 3.5E once 3.5C lands — they touch different surfaces.

**Default backend:** the baseline ships as default in 3.5C. The JEPA backend (3.5D) only becomes default once it beats the baseline by ≥ 10 % on the held-out cosine metric (acceptance criterion in 3.5D §10).

---

## 6. Non-goals (phase-wide)

- **No LLM in the hot path.** All prediction is CPU-local; LLM cost stays at zero per cycle.
- **No replacement of `SnapshotDiffer`.** Diffing remains the authoritative source of "what changed". Prediction adds "was it expected?".
- **No replacement of `EmbeddingEncoder`.** The fixed-encoder simplification is documented in §3 and revisited in 3.5D §11.
- **No online learning.** Training is periodic (default every 24 h) and triggered explicitly (CLI or scheduler). v1 ships without continual update.
- **No GPU.** All backends — baseline and JEPA — must train and serve on CPU. Latency budgets are stated per backend.

---

## 7. Out of scope (deferred to later phases)

- **Action-conditioned prediction** (V-JEPA 2 stage 2). Requires an actor port that does not yet exist in CoreMind.
- **Hierarchical H-JEPA** (multi-timescale prediction: minute / hour / day). Possible Phase 6.
- **Multimodal fusion** beyond the current `nomic-embed-text` pipeline (audio, vision, structured sensor streams encoded independently).
- **Counterfactual / planning** uses of the world model. Phase 3.5 only produces anomaly *signals*, not action recommendations.
- **Model-predictive control / planning** of effectors via the predictor (V-JEPA 2 robot demo). Effectors stay under L6's existing routing.

---

## 8. Phase acceptance criteria

- [ ] All five sub-phases merged and `just lint && just test` green at each step.
- [ ] `prediction.enabled = false` by default; daemon starts unchanged when disabled.
- [ ] With `prediction.enabled = true`, baseline backend, and a pre-trained model fixture, injecting a synthetic anomalous snapshot causes:
  - [ ] a signed `anomaly.detected` `WorldEventRecord` in the audit log,
  - [ ] a row visible on the `/prediction` dashboard page,
  - [ ] an entry in `coremind prediction anomalies`,
  - [ ] an L5 question citing the anomalous entity within one cycle.
- [ ] JEPA backend (3.5D) trains successfully on ≥ 500 real snapshots and beats baseline cosine divergence by ≥ 10 % on a held-out tail.
- [ ] Inference latency p95 < 100 ms per snapshot on CPU for both backends.
- [ ] No regressions in existing L1–L2–L4–L5–L6 tests.
- [ ] `docs/ARCHITECTURE.md` updated with a short "L2.5 — Predictive Layer" section.

---

## 9. References

- LeCun, Y. *A Path Towards Autonomous Machine Intelligence*. OpenReview, 2022. <https://openreview.net/forum?id=BZ5a1r-kVsf>
- Assran, M. *et al.* *Self-Supervised Learning from Images with a Joint-Embedding Predictive Architecture* (I-JEPA). CVPR, 2023. <https://arxiv.org/abs/2301.08243>
- Meta AI. *V-JEPA: The next step toward Yann LeCun's vision of advanced machine intelligence*. Blog, 2024. <https://ai.meta.com/blog/v-jepa-yann-lecun-ai-model-video-joint-embedding-predictive-architecture/>
- Meta AI. *Introducing the V-JEPA 2 world model and new benchmarks for physical reasoning*. Blog, 2025. <https://ai.meta.com/blog/v-jepa-2-world-model-benchmarks/>; paper <https://arxiv.org/abs/2506.09985>.
- Internal: [`PHASE_3_EMBEDDING_WORLD.md`](PHASE_3_EMBEDDING_WORLD.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`PHASE_4_AUTO_INVESTIGATION.md`](PHASE_4_AUTO_INVESTIGATION.md) (downstream consumer of anomaly events).
