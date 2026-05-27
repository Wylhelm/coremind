"""Tests for MetaLoop — orchestration and lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from coremind.meta.adjuster import MetaAdjuster
from coremind.meta.constants import FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS
from coremind.meta.loop import MetaLoop
from coremind.meta.safety_validator import MetaSafetyValidator
from coremind.meta.schemas import (
    AdjustmentPolicy,
    MetaConfig,
    MetaObservation,
    ProposedAdjustment,
    ValidationResult,
)
from coremind.meta.stores import (
    InMemoryApprovalQueue,
    InMemoryConfigStore,
    InMemoryMetaStore,
    LoggingMetaEventBus,
)

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class _FakeObserver:
    """Fake observer that returns preconfigured observations."""

    def __init__(self, observations: list[MetaObservation] | None = None) -> None:
        self._observations = observations or []
        self.call_count = 0

    async def observe_all(self) -> list[MetaObservation]:
        self.call_count += 1
        return list(self._observations)


class _FakeEvaluator:
    """Fake evaluator that returns preconfigured proposals."""

    def __init__(self, proposals: list[ProposedAdjustment] | None = None) -> None:
        self._proposals = proposals or []

    def evaluate(self, observations: list[MetaObservation]) -> list[ProposedAdjustment]:
        return list(self._proposals)


class _FakeValidator:
    """Fake validator that always returns valid=True."""

    def __init__(self, *, valid: bool = True, reason: str = "") -> None:
        self._valid = valid
        self._reason = reason

    def validate(self, proposal: ProposedAdjustment) -> ValidationResult:
        return ValidationResult(valid=self._valid, reason=self._reason)


class _FailingObserver:
    """Observer that raises on observe_all."""

    async def observe_all(self) -> list[MetaObservation]:
        msg = "observer exploded"
        raise RuntimeError(msg)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_policy(
    *,
    name: str = "test_policy",
    requires_approval: bool = False,
) -> AdjustmentPolicy:
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
        requires_user_approval=requires_approval,
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
    requires_approval: bool = False,
) -> ProposedAdjustment:
    return ProposedAdjustment(
        policy=_make_policy(requires_approval=requires_approval),
        observation=_make_observation(),
        parameter_path="intention.min_salience",
        old_value=old_value,
        new_value=new_value,
    )


def _make_config(*, enabled: bool = True, max_per_hour: int = 4) -> MetaConfig:
    return MetaConfig(
        enabled=enabled,
        observation_interval_seconds=1.0,
        max_adjustments_per_hour=max_per_hour,
        log_observations=True,
    )


def _make_loop(
    *,
    observer: _FakeObserver | _FailingObserver | None = None,
    evaluator: _FakeEvaluator | None = None,
    validator: _FakeValidator | MetaSafetyValidator | None = None,
    config: MetaConfig | None = None,
) -> tuple[MetaLoop, InMemoryMetaStore, InMemoryApprovalQueue, InMemoryConfigStore]:
    config_store = InMemoryConfigStore({"intention.min_salience": 0.3})
    meta_store = InMemoryMetaStore()
    event_bus = LoggingMetaEventBus()
    approval_queue = InMemoryApprovalQueue()
    adjuster = MetaAdjuster(
        config_store=config_store,
        meta_store=meta_store,
        event_bus=event_bus,
    )

    loop = MetaLoop(
        observer=observer or _FakeObserver(),
        evaluator=evaluator or _FakeEvaluator(),
        validator=validator or _FakeValidator(),
        adjuster=adjuster,
        meta_store=meta_store,
        approval_queue=approval_queue,
        config=config or _make_config(),
    )
    return loop, meta_store, approval_queue, config_store


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_applies_valid_proposal() -> None:
    """One tick with a valid triggered policy applies the adjustment."""
    proposal = _make_proposal()
    loop, _meta_store, _, config_store = _make_loop(
        observer=_FakeObserver([_make_observation()]),
        evaluator=_FakeEvaluator([proposal]),
    )

    await loop.tick()

    assert await config_store.get("intention.min_salience") == 0.35


@pytest.mark.asyncio
async def test_tick_rejects_invalid_proposal() -> None:
    """Proposals rejected by safety validator are logged, not applied."""
    proposal = _make_proposal()
    loop, _, _, config_store = _make_loop(
        observer=_FakeObserver([_make_observation()]),
        evaluator=_FakeEvaluator([proposal]),
        validator=_FakeValidator(valid=False, reason="forbidden path"),
    )

    await loop.tick()

    # Config unchanged
    assert await config_store.get("intention.min_salience") == 0.3


@pytest.mark.asyncio
async def test_tick_routes_approval_required() -> None:
    """Proposals with requires_user_approval go to the approval queue."""
    proposal = _make_proposal(requires_approval=True)
    loop, _, approval_queue, config_store = _make_loop(
        observer=_FakeObserver([_make_observation()]),
        evaluator=_FakeEvaluator([proposal]),
    )

    await loop.tick()

    # Config unchanged — not applied directly
    assert await config_store.get("intention.min_salience") == 0.3
    # Queued for approval
    assert len(approval_queue.pending) == 1
    assert approval_queue.pending[0].parameter_path == "intention.min_salience"


@pytest.mark.asyncio
async def test_tick_with_no_observations() -> None:
    """Empty observations produce no proposals and no side effects."""
    loop, _meta_store, approval_queue, config_store = _make_loop(
        observer=_FakeObserver([]),
        evaluator=_FakeEvaluator([]),
    )

    await loop.tick()

    assert await config_store.get("intention.min_salience") == 0.3
    assert len(approval_queue.pending) == 0


@pytest.mark.asyncio
async def test_tick_exception_does_not_crash_loop() -> None:
    """An exception in one tick is caught, loop continues."""
    loop, _, _, _ = _make_loop(observer=_FailingObserver())

    # tick() should not raise — the loop catches exceptions
    # We need to test _run_forever behavior. Instead, verify tick catches.
    # Actually, tick() does NOT catch internally — _run_forever does.
    # So let's verify that start/stop still work after a failing tick.
    loop._config = _make_config(enabled=True)

    # Start the loop, let it fail once, then stop
    await loop.start()
    await asyncio.sleep(0.05)  # Let at least one tick attempt
    await loop.stop()

    # No exception propagated — loop stopped cleanly


@pytest.mark.asyncio
async def test_start_disabled_is_noop() -> None:
    """MetaLoop with enabled=False does nothing on start."""
    loop, _, _, _ = _make_loop(config=_make_config(enabled=False))

    await loop.start()

    assert loop._task is None


@pytest.mark.asyncio
async def test_rate_limit_prevents_excess_adjustments() -> None:
    """Rate limiter blocks adjustments beyond max_adjustments_per_hour."""
    proposals = [_make_proposal(new_value=0.3 + 0.01 * i) for i in range(1, 6)]
    loop, _, _, config_store = _make_loop(
        observer=_FakeObserver([_make_observation()]),
        evaluator=_FakeEvaluator(proposals),
        config=_make_config(max_per_hour=2),
    )

    await loop.tick()

    # Only 2 adjustments applied (the rate limit)
    # The last applied value should be 0.32 (0.3 + 0.01*2)
    val = await config_store.get("intention.min_salience")
    assert val == pytest.approx(0.32)


@pytest.mark.asyncio
async def test_meta_loop_cannot_modify_hard_ask() -> None:
    """Integration: a policy targeting hard_ask is blocked by the real validator."""
    # Use real validator with production forbidden paths
    validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS)

    proposal = ProposedAdjustment(
        policy=AdjustmentPolicy(
            name="evil_policy",
            description="Tries to modify hard_ask",
            observation_kind="test_kind",
            trigger_condition="above",
            threshold=0.5,
            parameter_path="autonomy.hard_ask",
            direction="increase",
            delta=1.0,
            min_value=0.0,
            max_value=10.0,
            cooldown_seconds=60.0,
        ),
        observation=_make_observation(),
        parameter_path="autonomy.hard_ask",
        old_value=0.0,
        new_value=1.0,
    )

    loop, _, _, config_store = _make_loop(
        observer=_FakeObserver([_make_observation()]),
        evaluator=_FakeEvaluator([proposal]),
        validator=validator,
    )

    # Seed config so it doesn't raise KeyError
    await config_store.set("autonomy.hard_ask", 0.0)

    await loop.tick()

    # Value unchanged — proposal was rejected
    assert await config_store.get("autonomy.hard_ask") == 0.0
