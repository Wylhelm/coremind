"""Tests for MetaAdjuster — apply and rollback logic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coremind.meta.adjuster import MetaAdjuster
from coremind.meta.schemas import (
    AdjustmentPolicy,
    MetaObservation,
    ProposedAdjustment,
)
from coremind.meta.stores import (
    InMemoryConfigStore,
    InMemoryMetaStore,
    LoggingMetaEventBus,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_policy(*, name: str = "test_policy") -> AdjustmentPolicy:
    return AdjustmentPolicy(
        name=name,
        description="A test policy",
        observation_kind="test_kind",
        trigger_condition="above",
        threshold=0.5,
        parameter_path="intention.min_salience",
        direction="increase",
        delta=0.05,
        min_value=0.2,
        max_value=0.7,
        cooldown_seconds=3600.0,
    )


def _make_observation(*, kind: str = "test_kind", value: float = 0.8) -> MetaObservation:
    return MetaObservation(
        kind=kind,
        value=value,
        threshold=0.5,
        window_seconds=3600.0,
        triggers_policy=True,
    )


def _make_proposal(
    *,
    old_value: float = 0.3,
    new_value: float = 0.35,
) -> ProposedAdjustment:
    return ProposedAdjustment(
        policy=_make_policy(),
        observation=_make_observation(),
        parameter_path="intention.min_salience",
        old_value=old_value,
        new_value=new_value,
    )


def _make_adjuster() -> tuple[
    MetaAdjuster, InMemoryConfigStore, InMemoryMetaStore, LoggingMetaEventBus
]:
    config_store = InMemoryConfigStore({"intention.min_salience": 0.3})
    meta_store = InMemoryMetaStore()
    event_bus = LoggingMetaEventBus()
    adjuster = MetaAdjuster(
        config_store=config_store,
        meta_store=meta_store,
        event_bus=event_bus,
    )
    return adjuster, config_store, meta_store, event_bus


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_persists_record() -> None:
    """Applied adjustment is saved to meta_store."""
    adjuster, _, meta_store, _ = _make_adjuster()
    proposal = _make_proposal()

    record = await adjuster.apply(proposal)

    stored = await meta_store.get_adjustment(record.adjustment_id)
    assert stored is not None
    assert stored.policy_name == "test_policy"
    assert stored.parameter_path == "intention.min_salience"
    assert stored.old_value == 0.3
    assert stored.new_value == 0.35


@pytest.mark.asyncio
async def test_apply_updates_config() -> None:
    """Applied adjustment updates the config_store value."""
    adjuster, config_store, _, _ = _make_adjuster()
    proposal = _make_proposal()

    await adjuster.apply(proposal)

    assert await config_store.get("intention.min_salience") == 0.35


@pytest.mark.asyncio
async def test_apply_publishes_event() -> None:
    """Applied adjustment publishes to event bus."""
    adjuster, _, _, event_bus = _make_adjuster()
    proposal = _make_proposal()

    record = await adjuster.apply(proposal)

    assert len(event_bus.events) == 1
    topic, payload = event_bus.events[0]
    assert topic == "meta.adjustment.applied"
    assert payload["adjustment_id"] == record.adjustment_id
    assert payload["parameter_path"] == "intention.min_salience"
    assert payload["old_value"] == 0.3
    assert payload["new_value"] == 0.35


@pytest.mark.asyncio
async def test_rollback_restores_old_value() -> None:
    """Rollback sets parameter back to old_value."""
    adjuster, config_store, _, _ = _make_adjuster()
    proposal = _make_proposal()

    record = await adjuster.apply(proposal)
    assert await config_store.get("intention.min_salience") == 0.35

    await adjuster.rollback(record.adjustment_id)
    assert await config_store.get("intention.min_salience") == 0.3


@pytest.mark.asyncio
async def test_rollback_marks_timestamp() -> None:
    """Rolled back record has rollback_at set."""
    adjuster, _, meta_store, _ = _make_adjuster()
    proposal = _make_proposal()

    record = await adjuster.apply(proposal)
    assert record.rollback_at is None

    await adjuster.rollback(record.adjustment_id)

    updated = await meta_store.get_adjustment(record.adjustment_id)
    assert updated is not None
    assert updated.rollback_at is not None
    assert updated.rollback_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_rollback_unknown_id_raises() -> None:
    """Rollback with invalid adjustment_id raises ValueError."""
    adjuster, _, _, _ = _make_adjuster()

    with pytest.raises(ValueError, match="No adjustment found"):
        await adjuster.rollback("nonexistent-id")


@pytest.mark.asyncio
async def test_rollback_publishes_event() -> None:
    """Rollback publishes a rolled_back event to the bus."""
    adjuster, _, _, event_bus = _make_adjuster()
    proposal = _make_proposal()

    record = await adjuster.apply(proposal)
    await adjuster.rollback(record.adjustment_id)

    assert len(event_bus.events) == 2
    topic, payload = event_bus.events[1]
    assert topic == "meta.adjustment.rolled_back"
    assert payload["adjustment_id"] == record.adjustment_id
    assert payload["restored_value"] == 0.3
