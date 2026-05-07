"""Predictive memory — stores patterns and generates falsifiable predictions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

import structlog

from coremind.errors import PredictionError
from coremind.prediction.schemas import Prediction, PredictionEvidence

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PredictiveMemory:
    """Stores temporal patterns and generates falsifiable predictions.

    Uses SemanticMemory backend for pattern storage and retrieval.
    """

    def __init__(self, semantic_memory: object, *, clock: Clock = _utc_now) -> None:
        self._semantic = semantic_memory
        self._clock = clock
        self._predictions: dict[str, Prediction] = {}

    async def predict(self, observations: list[dict[str, object]]) -> list[Prediction]:
        """Generate predictions from observations using semantic similarity search."""
        if not observations:
            return []

        results: list[Prediction] = []
        for obs in observations:
            query = f"Pattern in {obs.get('domain', 'unknown')}: {obs.get('description', '')}"
            try:
                patterns = await self._semantic.recall(  # type: ignore[attr-defined]
                    query=query, k=5, tags=["pattern"], collection="semantic_facts"
                )
            except Exception:
                continue
            if not patterns:
                continue

            evidence = []
            for p in patterns:
                score = getattr(p, "score", 0.0) if hasattr(p, "score") else 0.5
                text = getattr(p, "text", str(p))
                if score > 0.3:
                    evidence.append(PredictionEvidence(observation=text, similarity_score=score))

            confidence = (
                sum(e.similarity_score for e in evidence) / len(evidence) * 1.5 if evidence else 0.0
            )
            confidence = min(confidence, 1.0)
            if confidence < 0.5:
                continue

            domain = obs.get("domain", "unknown")
            horizon = obs.get("horizon_hours", 24)
            pid = uuid.uuid4().hex
            pred = Prediction(  # type: ignore[arg-type]
                id=pid,
                domain=domain,
                description=obs.get("description", ""),
                confidence=confidence,
                evidence_points=[e.observation for e in evidence],
                verification_criteria=(
                    f"Vérifier dans {horizon}h si: {obs.get('description', '')}"
                ),
                horizon_hours=horizon,
            )
            results.append(pred)
            self._predictions[pid] = pred

        results.sort(key=lambda p: p.confidence, reverse=True)
        return results[:10]

    async def store(self, prediction: Prediction) -> None:
        """Persist a prediction to memory."""
        self._predictions[prediction.id] = prediction
        try:
            await self._semantic.remember(  # type: ignore[attr-defined]  # type: ignore[attr-defined]
                text=prediction.description,
                tags=["prediction", prediction.domain],
                metadata={
                    "prediction_id": prediction.id,
                    "confidence": prediction.confidence,
                    "horizon_hours": prediction.horizon_hours,
                    "status": prediction.status,
                },
                collection="semantic_facts",
            )
        except Exception as exc:
            raise PredictionError(f"Failed to store prediction {prediction.id}") from exc

    async def verify(self, prediction_id: str, outcome: bool) -> None:
        """Mark a prediction as verified or falsified."""
        pred = self._predictions.get(prediction_id)
        if pred is None:
            return
        self._predictions[prediction_id] = pred.model_copy(
            update={"status": "verified" if outcome else "falsified", "verified_at": self._clock()}
        )

    async def store_observation(self, observation: dict) -> None:
        """Store an observation as a pattern for future predictions."""
        text = f"Pattern in {observation.get('domain', 'unknown')}: {observation.get('description', '')}"
        try:
            await self._semantic.remember(  # type: ignore[attr-defined]
                text=text,
                tags=["pattern", "observation"],
                metadata={
                    "timestamp": self._clock().isoformat(),
                    "domain": observation.get("domain", "unknown"),
                },
                collection="semantic_facts",
            )
        except Exception:
            pass

    async def get_active_predictions(self, max_age_hours: int = 24) -> list[Prediction]:
        """Return all pending predictions within the time window."""
        cutoff = self._clock() - timedelta(hours=max_age_hours)
        return sorted(
            [
                p
                for p in self._predictions.values()
                if p.status == "pending" and p.created_at >= cutoff
            ],
            key=lambda p: p.confidence,
            reverse=True,
        )
