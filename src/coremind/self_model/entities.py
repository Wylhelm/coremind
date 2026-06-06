"""Pydantic models for the Self-Model entity system.

These models represent the core data structures for personal context:
facts about the user, their relationships, goals, projects, routines,
identity facets, and preferences.

Every fact carries a confidence level and provenance method, enabling
the system to distinguish between explicitly declared knowledge and
inferred observations.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type SelfModelEntityType = Literal["person", "goal", "project", "routine", "identity", "preference"]

type ConfidenceMethod = Literal["declared", "observed", "synthesized", "questioned"]

type JsonValue = str | int | float | bool | None | dict[str, "JsonValue"] | list["JsonValue"]


# ---------------------------------------------------------------------------
# Core fact model — the atomic unit of self-model knowledge
# ---------------------------------------------------------------------------


class SelfFact(BaseModel):
    """A single observation or inference about the user's world.

    Facts are immutable once created; updates create new versions linked
    via ``superseded_by``.  The ``active`` flag controls whether the fact
    participates in reasoning context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="ULID identifier.")
    entity_type: SelfModelEntityType
    entity_id: str = Field(min_length=1, description="Unique ID within the entity type.")
    attribute: str = Field(min_length=1, description="The attribute being described.")
    value: JsonValue = Field(description="The fact's value (structured JSON).")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this fact.")
    method: ConfidenceMethod = Field(description="How this fact was acquired.")
    source: str = Field(description="Plugin ID or 'user' for declared facts.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Event IDs or textual evidence supporting this fact.",
    )
    created_at: datetime
    updated_at: datetime
    superseded_by: str | None = Field(
        default=None,
        description="ID of the fact that replaced this one.",
    )
    active: bool = Field(default=True, description="Whether this fact is current.")


# ---------------------------------------------------------------------------
# Entity-specific models — typed views over SelfFact collections
# ---------------------------------------------------------------------------


class PersonEntity(BaseModel):
    """A person in the user's social network.

    Aggregated from person-type SelfFacts.  Represents relationships,
    contact patterns, and biographical data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(description="Stable identifier (e.g. 'aurelie', 'jeff').")
    name: str
    relationship: str = Field(description="E.g. 'fille', 'ami', 'collègue', 'ex'.")
    location: str | None = None
    birthday: date | None = None
    last_contact: datetime | None = None
    contact_frequency_days: float | None = Field(
        default=None,
        ge=0.0,
        description="Average days between contacts.",
    )


class GoalEntity(BaseModel):
    """A declared or inferred user goal.

    Goals track progress toward a specific outcome with optional deadlines.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    description: str
    target_metric: str | None = None
    deadline: date | None = None
    current_progress_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    status: Literal["active", "paused", "completed", "abandoned"] = "active"


class ProjectEntity(BaseModel):
    """An active project the user is working on.

    Tracks development activity, phase progression, and engagement intensity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    name: str
    current_phase: str | None = None
    progress_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    last_commit: datetime | None = None
    days_inactive: int | None = Field(default=None, ge=0)
    status: Literal["active", "paused", "completed"] = "active"
    intensity: Literal["high", "medium", "low"] | None = None


class RoutineEntity(BaseModel):
    """A detected behavioral pattern or habit.

    Routines are patterns observed over time, not single events.
    They carry higher confidence as more data points confirm them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    name: str
    time_window: str | None = Field(
        default=None,
        description="Time range (e.g. '20:00-00:00').",
    )
    days: list[str] | None = Field(
        default=None,
        description="Days of week (e.g. ['mon', 'tue', 'wed']).",
    )
    frequency: str | None = Field(
        default=None,
        description="Recurrence pattern (e.g. 'daily', 'weekly').",
    )
    avg_duration_minutes: float | None = Field(default=None, ge=0.0)


class IdentityEntity(BaseModel):
    """A facet of the user's identity.

    Represents professional roles, skills, values, and knowledge domains.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    domain: str = Field(description="Identity domain (e.g. 'tech', 'valeurs', 'connaissances').")
    attributes: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Key-value pairs for this identity facet.",
    )


class PreferenceEntity(BaseModel):
    """A learned or declared user preference.

    Preferences guide system behavior (e.g. notification timing, voice style).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    domain: str = Field(description="Preference domain (e.g. 'code', 'voice', 'food').")
    attribute: str = Field(description="What the preference is about.")
    value: JsonValue = Field(description="The preferred value.")
    learned_from: str | None = Field(
        default=None,
        description="How this preference was learned (source or method).",
    )
