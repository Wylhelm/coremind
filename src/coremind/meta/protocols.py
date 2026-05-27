"""Protocol definitions for the meta-cognition layer.

These protocols decouple L8 components from concrete store
implementations.  Each protocol declares the minimal surface needed by
:class:`~coremind.meta.observer.MetaObserver` and
:class:`~coremind.meta.evaluator.PolicyEvaluator`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from coremind.meta.schemas import AdjustmentRecord, MetaObservation, ProposedAdjustment

# ------------------------------------------------------------------
# Supporting data classes
# ------------------------------------------------------------------


class PluginStats(BaseModel):
    """Aggregated plugin statistics over a time window."""

    model_config = ConfigDict(frozen=True)

    plugin_id: str = Field(min_length=1)
    total_calls: int = Field(ge=0)
    errors: int = Field(ge=0)
    window_seconds: float = Field(gt=0.0)


class InvestigationSummary(BaseModel):
    """Minimal investigation record for success-rate calculation."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


# ------------------------------------------------------------------
# Store protocols
# ------------------------------------------------------------------


class IntentionStoreProtocol(Protocol):
    """Read-only view of the intention store for L8 observation."""

    async def recent(self, *, since: datetime) -> list[Any]:
        """Return intents created since *since*, newest first."""
        ...


class ActionStoreProtocol(Protocol):
    """Read-only view of the action journal for L8 observation."""

    async def list_actions(self, *, since: datetime, until: datetime) -> list[Any]:
        """Return actions with timestamp in [since, until)."""
        ...


class PluginRegistryProtocol(Protocol):
    """Read-only view of plugin health for L8 observation."""

    async def get_all_stats(self, window: timedelta) -> list[PluginStats]:
        """Return per-plugin stats for the given time window."""
        ...


class NarrativeStoreProtocol(Protocol):
    """Read-only narrative/memory view for L8 observation."""

    async def total_tokens(self, window: timedelta) -> int:
        """Return total LLM tokens consumed in the given window."""
        ...

    async def list_investigations(self, window: timedelta) -> list[InvestigationSummary]:
        """Return investigation summaries for the given window."""
        ...


# ------------------------------------------------------------------
# Evaluator protocols (synchronous — pure logic)
# ------------------------------------------------------------------


class AdjustmentHistoryProtocol(Protocol):
    """Read-only view of past adjustments for cooldown checks."""

    def last_adjustment(self, parameter_path: str) -> AdjustmentRecord | None:
        """Return the most recent adjustment for *parameter_path*, or None."""
        ...


class ConfigReaderProtocol(Protocol):
    """Read-only view of runtime configuration for current-value lookups."""

    def get(self, dotted_path: str) -> float:
        """Return the current numeric value at *dotted_path*."""
        ...


# ------------------------------------------------------------------
# Adjuster / Loop protocols (async, side-effecting)
# ------------------------------------------------------------------


class ConfigStoreProtocol(Protocol):
    """Mutable config store for the meta-adjuster."""

    async def get(self, dotted_path: str) -> Any:
        """Return the current value at *dotted_path*."""
        ...

    async def set(self, dotted_path: str, value: Any) -> None:
        """Persist *value* at *dotted_path*."""
        ...


class MetaStoreProtocol(Protocol):
    """Persistence for meta-loop records."""

    async def save_adjustment(self, record: AdjustmentRecord) -> None:
        """Persist an applied adjustment record."""
        ...

    async def get_adjustment(self, adjustment_id: str) -> AdjustmentRecord | None:
        """Return the adjustment record with *adjustment_id*, or None."""
        ...

    async def update_adjustment(self, record: AdjustmentRecord) -> None:
        """Update an existing adjustment record (e.g. after rollback)."""
        ...

    async def save_observations(self, observations: list[MetaObservation]) -> None:
        """Persist a batch of observations."""
        ...


class MetaEventBusProtocol(Protocol):
    """Publish meta-loop events (decoupled from the main WorldEvent bus)."""

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish *payload* under *topic*."""
        ...


class ApprovalQueueProtocol(Protocol):
    """Queue for proposals requiring user approval."""

    async def add(self, proposal: ProposedAdjustment) -> None:
        """Enqueue *proposal* for user approval."""
        ...
