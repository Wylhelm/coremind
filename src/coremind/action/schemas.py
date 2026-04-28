"""Pydantic models for the action layer (L6).

An :class:`Action` is the concrete side effect L6 dispatches to an effector
plugin.  Every ``Action`` is signed by the daemon before it is journaled and
dispatched; the signature covers every field except ``signature``, ``result``,
and ``completed_at`` so that dispatch is committed before execution (see
:mod:`coremind.action.journal`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coremind.world.model import JsonValue

type ActionCategory = Literal["safe", "suggest", "ask"]
type ActionOutcome = Literal[
    "dispatched",
    "ok",
    "noop",
    "transient_failure",
    "permanent_failure",
    "rejected_invalid_signature",
]


class ActionResult(BaseModel):
    """Outcome of attempting to execute an :class:`Action`.

    Mirrors the ``ActionResult`` proto message from ``spec/plugin.proto``
    but uses JSON-serialisable Python types.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str
    status: ActionOutcome
    message: str = ""
    output: dict[str, JsonValue] | None = None
    completed_at: datetime
    reversed_by_operation: str | None = None
    reversal_parameters: dict[str, JsonValue] | None = None


class Action(BaseModel):
    """A single side-effect proposed by L6.

    Attributes:
        id: ``uuid4().hex`` unique identifier (32 hex chars).
        intent_id: ID of the originating :class:`~coremind.intention.schemas.Intent`.
        timestamp: When the daemon dispatched this action (UTC).
        category: Agency category inherited from the originating intent.
        operation: Plugin-qualified operation name
            (e.g. ``coremind.plugin.homeassistant.turn_on``).
        parameters: Operation parameters.
        action_class: Coarse classification used for forced-approval gating
            (e.g. ``"light"``, ``"hvac"``, ``"finance.transfer"``).
        expected_outcome: Human-readable description of what the system expects
            to happen on success.
        reversal: Optional operation name that would undo this action, or
            a short manual-steps string.
        signature: Base64-encoded ed25519 signature over the canonical form
            of every field except ``signature`` and ``result``.  ``None``
            only while the action is being constructed.
        result: Populated after dispatch completes.
    """

    model_config = ConfigDict(frozen=False)

    id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    timestamp: datetime
    category: ActionCategory
    operation: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    action_class: str = Field(min_length=1)
    expected_outcome: str = ""
    reversal: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    signature: str | None = None
    result: ActionResult | None = None
