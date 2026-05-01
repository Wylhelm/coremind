"""Pydantic models for the intention layer (L5).

The intention loop emits :class:`Intent` objects representing questions the
system has posed to itself.  Each intent may or may not carry a concrete
:class:`ActionProposal`; an intent without one is a pure question (handled
downstream by future reflection cycles).

All models are strictly validated: the ``category`` literal is kept in sync
with the agency taxonomy in `ARCHITECTURE.md §15.2`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coremind.world.model import EntityRef, JsonValue

type IntentCategory = Literal["safe", "suggest", "ask"]
type IntentStatus = Literal[
    "pending",
    "pending_approval",
    "approved",
    "rejected",
    "snoozed",
    "executing",
    "done",
    "failed",
    "expired",
    "auto_dismissed",
]


class InternalQuestion(BaseModel):
    """A question the system has formed about its own world.

    Grounding requires at least one entity reference or a non-empty
    ``reasoning_refs`` list — ungrounded questions are rejected at construction
    time per the design constraint in `ARCHITECTURE.md §3.5`.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    grounding: list[EntityRef] = Field(default_factory=list)
    reasoning_refs: list[str] = Field(default_factory=list)


class ActionProposal(BaseModel):
    """A concrete action the system believes would answer an intent.

    Attributes:
        operation: Plugin-qualified operation name (e.g.
            ``coremind.plugin.homeassistant.turn_on``).
        parameters: Operation parameters.
        expected_outcome: Human-readable description of the predicted effect.
        reversal: Optional reversal operation or manual-steps string.
        action_class: Coarse classification used by the forced-approval
            gate (e.g. ``"light"``, ``"hvac"``, ``"finance.transfer"``).
    """

    model_config = ConfigDict(frozen=True)

    operation: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    expected_outcome: str = ""
    reversal: str | None = None
    action_class: str = Field(min_length=1)


class Intent(BaseModel):
    """An L5 intent — a question plus optional proposed action.

    Attributes:
        id: ``uuid4().hex`` unique identifier (32 hex chars).
        created_at: Generation time (UTC).
        question: The internal question this intent embodies.
        proposed_action: Optional concrete action to satisfy the question.
        salience: How much this intent deserves attention (0-1).
        confidence: How confident the system is in ``proposed_action`` (0-1).
        category: Agency category — one of ``safe``/``suggest``/``ask``.
        status: Lifecycle status.
        expires_at: Hard deadline for pending approvals.
        human_feedback: Free-form text from the user if any.
        snooze_count: Number of times this intent has been snoozed.  A second
            snooze is refused by the approval gate.
    """

    model_config = ConfigDict(frozen=False)

    id: str = Field(min_length=1)
    created_at: datetime
    question: InternalQuestion
    proposed_action: ActionProposal | None = None
    salience: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    category: IntentCategory
    status: IntentStatus = "pending"
    expires_at: datetime | None = None
    human_feedback: str | None = None
    snooze_count: int = Field(default=0, ge=0)


class QuestionBatch(BaseModel):
    """Structured output from the intention LLM call.

    The model returns a list of internal questions plus an optional proposal
    for each.  The intention loop wraps these into :class:`Intent` objects
    after scoring salience, confidence, and category.
    """

    questions: list[RawIntent] = Field(default_factory=list)


class RawIntent(BaseModel):
    """LLM-emitted intent candidate before scoring.

    The daemon enforces grounding, salience, and confidence; the model is
    trusted only for textual content.
    """

    model_config = ConfigDict(frozen=True)

    question: InternalQuestion
    proposed_action: ActionProposal | None = None
    model_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    model_salience: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


# Forward-reference resolution for QuestionBatch.questions
QuestionBatch.model_rebuild()
