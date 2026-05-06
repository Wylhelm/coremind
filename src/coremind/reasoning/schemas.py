"""Structured output schemas for the reasoning layer (L4).

Every LLM-produced reasoning cycle emits a :class:`ReasoningOutput` conforming
to this schema.  Free-form text is never accepted from the model; the
:class:`coremind.reasoning.llm.LLM` wrapper validates the JSON response against
the Pydantic models defined here and retries on malformed output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coremind.world.model import EntityRef


class Pattern(BaseModel):
    """A recurring regularity observed across the world snapshot.

    Patterns describe stable structure (e.g. ``"user commutes to office at
    08:15 on weekdays"``) grounded in concrete entities.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entities_involved: list[EntityRef]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Anomaly(BaseModel):
    """A deviation from an established baseline for an entity.

    The ``baseline_description`` records what the model expected; the overall
    ``description`` records what was actually observed.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entity: EntityRef
    severity: Literal["low", "medium", "high"]
    baseline_description: str = Field(min_length=1)


class Prediction(BaseModel):
    """A falsifiable hypothesis about near-future state.

    The ``falsifiable_by`` field captures the concrete observation that
    would refute the prediction — without it, the prediction has no
    epistemic value.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    horizon_hours: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    falsifiable_by: str = Field(min_length=1)


class TokenUsage(BaseModel):
    """Token accounting for a single LLM call."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class Investigation(BaseModel):
    """A question the reasoning layer wants to track over future cycles.

    These drive proactive curiosity — the system investigates on its own
    rather than waiting for explicit triggers.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    question: str = Field(min_length=1, description="What am I trying to understand?")
    cross_domains: list[str] = Field(default_factory=list, description="e.g. ['health', 'home', 'finance']")
    data_needed: str = Field(default="", description="What data would answer this?")
    timeframe_hours: int = Field(default=168, ge=1, description="How long to track?")
    confidence: float = Field(ge=0.0, le=1.0)


class ReasoningOutput(BaseModel):
    """The complete output of one reasoning cycle.

    Persisted to L2 as a ``reasoning_cycle`` entity and optionally to the
    audit journal.
    """

    cycle_id: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_used: str = Field(min_length=1)
    patterns: list[Pattern] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    investigations: list[Investigation] = Field(default_factory=list)
    token_usage: TokenUsage
