"""Pydantic models for the CoreMind World Model (L2).

These models represent the core domain objects that flow through the daemon:
entities, relationships, and event records.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

type JsonValue = str | int | float | bool | None | dict[str, JsonValue] | list[JsonValue]


class EntityRef(BaseModel):
    """Reference to the entity being observed.

    The combined key (type, id) uniquely identifies an entity across
    all plugins and all time.
    """

    type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    id: str = Field(min_length=1)


class Entity(BaseModel):
    """A persisted entity node in the World Model graph.

    Entities are created on first observation and updated on subsequent
    events that reference the same (type, id) pair.
    """

    type: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    properties: dict[str, JsonValue] = Field(default_factory=dict)
    source_plugins: list[str] = Field(default_factory=list)


class Relationship(BaseModel):
    """A directed relationship between two entities in the World Model.

    Relationships are reinforced each time the same (from, to, type) triple
    is observed; ``last_reinforced`` tracks recency.
    """

    type: str
    from_entity: EntityRef
    to_entity: EntityRef
    weight: float = Field(default=1.0, ge=0.0)
    created_at: datetime
    last_reinforced: datetime


class WorldEventRecord(BaseModel):
    """Immutable record of a single observation in the world model.

    Produced by L1 plugins and consumed by the L2 World Model ingest task.
    Internal daemon meta-events (e.g. bus.overflow) may carry signature=None;
    no meta-event is ever persisted to L2.
    """

    id: str
    timestamp: datetime
    source: str
    source_version: str
    signature: str | None
    entity: EntityRef
    attribute: str
    value: JsonValue
    confidence: float = Field(ge=0.0, le=1.0)
    unit: str | None = None


class WorldSnapshot(BaseModel):
    """A point-in-time view of the World Model.

    Contains all known entities and their current properties, along with
    recent event records used to reconstruct state.
    """

    taken_at: datetime
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    recent_events: list[WorldEventRecord] = Field(default_factory=list)
