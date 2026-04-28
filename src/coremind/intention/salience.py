"""Salience and confidence scoring heuristics for L5 intents.

Phase 3 uses static heuristics.  Phase 4 feeds calibration corrections back
from reflection; both signals remain in the ``0.0 - 1.0`` range so upstream
consumers are unaffected.

Salience is a blend of:

- **Novelty** — penalised when a near-duplicate intent was emitted recently.
- **Urgency** — boosted when the grounding entities have been changing fast.
- **Impact** — scaled by how many entities the question touches.
- **Coherence** — boosted when a matching procedural rule exists.

Confidence is a blend of:

- **Model-reported confidence** (from ``RawIntent.model_confidence``).
- **Procedural support** — increases with the number of matching rules.
- **Ensemble agreement** (optional; defaults to 1.0 when a single model is used).

Exact formulas are kept simple and documented so the scoring can be
audited and golden-tested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from coremind.intention.schemas import Intent, RawIntent
from coremind.world.model import WorldSnapshot


@dataclass(frozen=True)
class SalienceWeights:
    """Linear weights for the salience blend."""

    novelty: float = 0.30
    urgency: float = 0.30
    impact: float = 0.20
    coherence: float = 0.20


_DEFAULT_SALIENCE_WEIGHTS = SalienceWeights()


@dataclass(frozen=True)
class ConfidenceWeights:
    """Linear weights for the confidence blend."""

    model: float = 0.6
    procedural: float = 0.3
    ensemble: float = 0.1


_DEFAULT_CONFIDENCE_WEIGHTS = ConfidenceWeights()

# Confidence thresholds for the default category policy.
_CONF_SAFE_MIN = 0.90
_CONF_SUGGEST_MIN = 0.50


def score_salience(
    raw: RawIntent,
    snapshot: WorldSnapshot,
    recent_intents: Iterable[Intent],
    *,
    weights: SalienceWeights = _DEFAULT_SALIENCE_WEIGHTS,
) -> float:
    """Return a salience score in ``[0.0, 1.0]``.

    Args:
        raw: The freshly generated intent candidate.
        snapshot: Current world snapshot; used for urgency and impact signals.
        recent_intents: Recent intents for novelty detection.
        weights: Optional weight override.

    Returns:
        The blended salience score.
    """
    novelty = _novelty(raw, recent_intents)
    urgency = _urgency(raw, snapshot)
    impact = _impact(raw, snapshot)
    coherence = raw.model_salience  # model-hinted coherence proxy

    total = (
        weights.novelty * novelty
        + weights.urgency * urgency
        + weights.impact * impact
        + weights.coherence * coherence
    )
    return _clamp(total)


def score_confidence(
    raw: RawIntent,
    matching_rules: int,
    *,
    weights: ConfidenceWeights = _DEFAULT_CONFIDENCE_WEIGHTS,
    ensemble_agreement: float = 1.0,
) -> float:
    """Return a confidence score in ``[0.0, 1.0]``.

    Args:
        raw: The freshly generated intent candidate.
        matching_rules: Count of procedural-memory rules whose trigger
            matched the current snapshot.
        weights: Optional weight override.
        ensemble_agreement: Fraction of ensemble models that agreed (defaults
            to 1.0 when a single model is in use).

    Returns:
        The blended confidence score.
    """
    model = raw.model_confidence
    procedural = min(matching_rules / 5.0, 1.0)  # saturates at 5 rules
    ensemble = max(0.0, min(ensemble_agreement, 1.0))

    total = weights.model * model + weights.procedural * procedural + weights.ensemble * ensemble
    return _clamp(total)


def categorize(confidence: float, proposed_action_class: str | None) -> str:
    """Default confidence-based categorisation.

    The forced-approval classes are enforced downstream by the action router;
    this helper only implements the confidence-tiered default per
    `ARCHITECTURE.md §3.6`.

    Args:
        confidence: Score from :func:`score_confidence`.
        proposed_action_class: Action class string, if a proposal exists.

    Returns:
        One of ``"safe"``, ``"suggest"``, ``"ask"``.
    """
    _ = proposed_action_class  # reserved for later category policy refinement
    if confidence >= _CONF_SAFE_MIN:
        return "safe"
    if confidence >= _CONF_SUGGEST_MIN:
        return "suggest"
    return "ask"


# ---------------------------------------------------------------------------
# Internal signal functions
# ---------------------------------------------------------------------------


def _clamp(x: float) -> float:
    """Clamp ``x`` to the unit interval."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _novelty(raw: RawIntent, recent_intents: Iterable[Intent]) -> float:
    """Return a novelty score based on text overlap with recent intents.

    Uses a simple token-set Jaccard as the overlap signal.  Perfect novelty
    returns 1.0; a near-duplicate of a recent intent returns 0.0.
    """
    new_tokens = _tokenise(raw.question.text)
    if not new_tokens:
        return 0.0
    max_overlap = 0.0
    for prior in recent_intents:
        prior_tokens = _tokenise(prior.question.text)
        if not prior_tokens:
            continue
        inter = len(new_tokens & prior_tokens)
        union = len(new_tokens | prior_tokens)
        if union == 0:
            continue
        max_overlap = max(max_overlap, inter / union)
    return _clamp(1.0 - max_overlap)


def _urgency(raw: RawIntent, snapshot: WorldSnapshot) -> float:
    """Return an urgency score based on how many grounding entities are recent.

    An entity counts as "recently active" if it appears in
    ``snapshot.recent_events``.
    """
    if not raw.question.grounding:
        return 0.0
    recent_keys = {(e.entity.type, e.entity.id) for e in snapshot.recent_events}
    if not recent_keys:
        return 0.0
    hits = sum(1 for ref in raw.question.grounding if (ref.type, ref.id) in recent_keys)
    return _clamp(hits / len(raw.question.grounding))


def _impact(raw: RawIntent, snapshot: WorldSnapshot) -> float:
    """Return an impact score based on the count of grounded entities.

    Larger sets saturate at 1.0 after 5 entities.
    """
    n = len(raw.question.grounding)
    if n == 0:
        return 0.0
    _ = snapshot  # reserved for relationship-based impact in Phase 4
    return min(n / 5.0, 1.0)


def _tokenise(text: str) -> set[str]:
    """Tokenise ``text`` to a lower-cased word set for Jaccard."""
    return {tok for tok in text.lower().split() if tok.isalnum()}
