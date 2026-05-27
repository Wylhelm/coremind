"""Unit tests for MetaObserver."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from coremind.meta.observer import MetaObserver
from coremind.meta.protocols import InvestigationSummary, PluginStats

# ------------------------------------------------------------------
# Factory helpers
# ------------------------------------------------------------------


def _make_intent(
    *,
    topic: str = "default topic",
    entities: list[str] | None = None,
    salience: float = 0.7,
    confidence: float = 0.8,
    status: str = "done",
) -> Any:
    """Create a minimal intent-like object."""

    class _Question:
        def __init__(self, text: str, grounding: list[str]) -> None:
            self.text = text
            self.grounding = grounding

    class _Intent:
        def __init__(
            self,
            question: _Question,
            salience: float,
            confidence: float,
            status: str,
            intent_id: str,
        ) -> None:
            self.question = question
            self.salience = salience
            self.confidence = confidence
            self.status = status
            self.id = intent_id

    return _Intent(
        question=_Question(text=topic, grounding=entities or []),
        salience=salience,
        confidence=confidence,
        status=status,
        intent_id=uuid.uuid4().hex,
    )


def _make_action(
    *,
    action_class: str = "light.turn_on",
    category: str = "ask",
    result_status: str | None = "ok",
) -> Any:
    """Create a minimal action-like object."""

    class _Result:
        def __init__(self, status: str) -> None:
            self.status = status

    class _Action:
        def __init__(self, action_class: str, category: str, result: _Result | None) -> None:
            self.action_class = action_class
            self.category = category
            self.result = result

    result = _Result(result_status) if result_status is not None else None
    return _Action(action_class=action_class, category=category, result=result)


# ------------------------------------------------------------------
# Fake stores
# ------------------------------------------------------------------


class _FakeIntentionStore:
    def __init__(self, intents: list[Any] | None = None) -> None:
        self._intents = intents or []

    async def recent(self, *, since: datetime) -> list[Any]:
        return self._intents


class _FakeActionStore:
    def __init__(self, actions: list[Any] | None = None) -> None:
        self._actions = actions or []

    async def list_actions(self, *, since: datetime, until: datetime) -> list[Any]:
        return self._actions


class _FakePluginRegistry:
    def __init__(self, stats: list[PluginStats] | None = None) -> None:
        self._stats = stats or []

    async def get_all_stats(self, window: timedelta) -> list[PluginStats]:
        return self._stats


class _FakeNarrativeStore:
    def __init__(
        self,
        tokens: int = 0,
        investigations: list[InvestigationSummary] | None = None,
    ) -> None:
        self._tokens = tokens
        self._investigations = investigations or []

    async def total_tokens(self, window: timedelta) -> int:
        return self._tokens

    async def list_investigations(self, window: timedelta) -> list[InvestigationSummary]:
        return self._investigations


class _BrokenStore:
    """Store that raises on any call."""

    async def recent(self, *, since: datetime) -> list[Any]:
        raise RuntimeError("store is broken")

    async def list_actions(self, *, since: datetime, until: datetime) -> list[Any]:
        raise RuntimeError("store is broken")

    async def get_all_stats(self, window: timedelta) -> list[PluginStats]:
        raise RuntimeError("store is broken")

    async def total_tokens(self, window: timedelta) -> int:
        raise RuntimeError("store is broken")

    async def list_investigations(self, window: timedelta) -> list[InvestigationSummary]:
        raise RuntimeError("store is broken")


# ------------------------------------------------------------------
# Fixture
# ------------------------------------------------------------------


def _make_observer(
    *,
    intents: list[Any] | None = None,
    actions: list[Any] | None = None,
    plugin_stats: list[PluginStats] | None = None,
    tokens: int = 0,
    investigations: list[InvestigationSummary] | None = None,
) -> MetaObserver:
    return MetaObserver(
        intention_store=_FakeIntentionStore(intents),
        action_store=_FakeActionStore(actions),
        plugin_registry=_FakePluginRegistry(plugin_stats),
        narrative_store=_FakeNarrativeStore(tokens, investigations),
    )


# ------------------------------------------------------------------
# Intent repeat rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_repeat_rate_no_repeats() -> None:
    """All unique intents produce a repeat rate of 0.0."""
    observer = _make_observer(
        intents=[
            _make_intent(topic="a", entities=["x"]),
            _make_intent(topic="b", entities=["y"]),
            _make_intent(topic="c", entities=["z"]),
        ]
    )

    obs = await observer._observe_intent_repeat_rate()

    assert obs.kind == "intent_repeat_rate"
    assert obs.value == 0.0
    assert obs.triggers_policy is False


@pytest.mark.asyncio
async def test_intent_repeat_rate_all_repeats() -> None:
    """All identical intents produce rate = (n-1)/n."""
    observer = _make_observer(
        intents=[
            _make_intent(topic="same", entities=["e1"]),
            _make_intent(topic="same", entities=["e1"]),
            _make_intent(topic="same", entities=["e1"]),
        ]
    )

    obs = await observer._observe_intent_repeat_rate()

    assert obs.kind == "intent_repeat_rate"
    assert abs(obs.value - 2 / 3) < 0.01
    assert obs.triggers_policy is True  # 0.67 > 0.30


@pytest.mark.asyncio
async def test_intent_repeat_rate_empty() -> None:
    """No intents produce rate of 0.0."""
    observer = _make_observer(intents=[])

    obs = await observer._observe_intent_repeat_rate()

    assert obs.value == 0.0
    assert obs.triggers_policy is False


# ------------------------------------------------------------------
# Intents per hour
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intents_per_hour() -> None:
    """6 intents in a 6-hour window = 1.0 per hour."""
    observer = _make_observer(intents=[_make_intent() for _ in range(6)])

    obs = await observer._observe_intents_per_hour()

    assert obs.kind == "intents_per_hour"
    assert obs.value == 1.0
    # 1.0 is not below threshold 1.0, so does not trigger.
    assert obs.triggers_policy is False


@pytest.mark.asyncio
async def test_intents_per_hour_empty() -> None:
    """No intents = 0 per hour, triggers (below threshold)."""
    observer = _make_observer(intents=[])

    obs = await observer._observe_intents_per_hour()

    assert obs.value == 0.0
    assert obs.triggers_policy is True


# ------------------------------------------------------------------
# Notification engagement rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_engagement_no_data() -> None:
    """No suggest-category actions returns default 0.5."""
    observer = _make_observer(actions=[])

    obs = await observer._observe_notification_engagement_rate()

    assert obs.kind == "notification_engagement_rate"
    assert obs.value == 0.5
    assert obs.triggers_policy is False  # 0.5 is not below 0.30


@pytest.mark.asyncio
async def test_notification_engagement_all_engaged() -> None:
    """All suggest actions with successful results = 1.0."""
    observer = _make_observer(
        actions=[
            _make_action(category="suggest", result_status="ok"),
            _make_action(category="suggest", result_status="noop"),
            _make_action(category="suggest", result_status="dispatched"),
        ]
    )

    obs = await observer._observe_notification_engagement_rate()

    assert obs.value == 1.0
    assert obs.triggers_policy is False


@pytest.mark.asyncio
async def test_notification_engagement_none_engaged() -> None:
    """All suggest actions without results = 0.0, triggers."""
    observer = _make_observer(
        actions=[
            _make_action(category="suggest", result_status=None),
            _make_action(category="suggest", result_status=None),
        ]
    )

    obs = await observer._observe_notification_engagement_rate()

    assert obs.value == 0.0
    assert obs.triggers_policy is True  # 0.0 < 0.30


# ------------------------------------------------------------------
# Domain approval rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_approval_rate_skips_small_domains() -> None:
    """Domains with fewer than 10 actions produce no observations."""
    observer = _make_observer(
        actions=[
            _make_action(action_class="light.turn_on", category="ask", result_status="ok")
            for _ in range(5)
        ]
    )

    obs_list = await observer._observe_domain_approval_rates()

    assert obs_list == []


@pytest.mark.asyncio
async def test_domain_approval_rate_computes_correctly() -> None:
    """10 ask-actions with 8 approved = rate of 0.8."""
    actions = [
        _make_action(action_class="light.turn_on", category="ask", result_status="ok")
        for _ in range(8)
    ] + [
        _make_action(action_class="light.turn_off", category="ask", result_status=None)
        for _ in range(2)
    ]
    observer = _make_observer(actions=actions)

    obs_list = await observer._observe_domain_approval_rates()

    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.kind == "domain_approval_rate"
    assert obs.value == 0.8
    assert obs.metadata["domain"] == "lights"


@pytest.mark.asyncio
async def test_domain_approval_rate_ignores_non_ask() -> None:
    """Only ask-category actions count."""
    actions = [
        _make_action(action_class="light.turn_on", category="safe", result_status="ok")
        for _ in range(20)
    ]
    observer = _make_observer(actions=actions)

    obs_list = await observer._observe_domain_approval_rates()

    assert obs_list == []


# ------------------------------------------------------------------
# Plugin error rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_error_rate_skips_zero_calls() -> None:
    """Plugins with zero calls produce no observation."""
    observer = _make_observer(
        plugin_stats=[PluginStats(plugin_id="p1", total_calls=0, errors=0, window_seconds=3600.0)]
    )

    obs_list = await observer._observe_plugin_error_rates()

    assert obs_list == []


@pytest.mark.asyncio
async def test_plugin_error_rate_high_errors() -> None:
    """6 errors out of 10 calls = 0.6, triggers."""
    observer = _make_observer(
        plugin_stats=[PluginStats(plugin_id="ha", total_calls=10, errors=6, window_seconds=3600.0)]
    )

    obs_list = await observer._observe_plugin_error_rates()

    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.kind == "plugin_error_rate"
    assert obs.value == 0.6
    assert obs.triggers_policy is True  # 0.6 > 0.50
    assert obs.metadata["plugin_id"] == "ha"


@pytest.mark.asyncio
async def test_plugin_error_rate_low_errors() -> None:
    """1 error out of 100 calls = 0.01, does not trigger."""
    observer = _make_observer(
        plugin_stats=[
            PluginStats(plugin_id="weather", total_calls=100, errors=1, window_seconds=3600.0)
        ]
    )

    obs_list = await observer._observe_plugin_error_rates()

    assert len(obs_list) == 1
    assert obs_list[0].triggers_policy is False


# ------------------------------------------------------------------
# Token per useful intent
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_per_useful_intent_no_intents() -> None:
    """No useful intents: divide by 1 (guard), returns raw token count."""
    observer = _make_observer(
        intents=[_make_intent(status="pending")],
        tokens=10000,
    )

    obs = await observer._observe_token_per_useful_intent()

    assert obs.kind == "token_per_useful_intent"
    assert obs.value == 10000.0
    assert obs.triggers_policy is True  # 10000 > 5000


@pytest.mark.asyncio
async def test_token_per_useful_intent_with_useful() -> None:
    """5000 tokens / 5 useful intents = 1000."""
    observer = _make_observer(
        intents=[_make_intent(status="done") for _ in range(5)],
        tokens=5000,
    )

    obs = await observer._observe_token_per_useful_intent()

    assert obs.value == 1000.0
    assert obs.triggers_policy is False  # 1000 < 5000


# ------------------------------------------------------------------
# Investigation success rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investigation_success_rate_no_data() -> None:
    """No investigations returns 1.0 (nothing to worry about)."""
    observer = _make_observer(investigations=[])

    obs = await observer._observe_investigation_success_rate()

    assert obs.kind == "investigation_success_rate"
    assert obs.value == 1.0
    assert obs.triggers_policy is False


@pytest.mark.asyncio
async def test_investigation_success_rate_partial() -> None:
    """2 resolved out of 5 = 0.4, triggers (below 0.60)."""
    observer = _make_observer(
        investigations=[
            InvestigationSummary(investigation_id="i1", status="resolved"),
            InvestigationSummary(investigation_id="i2", status="resolved"),
            InvestigationSummary(investigation_id="i3", status="escalated"),
            InvestigationSummary(investigation_id="i4", status="unresolved"),
            InvestigationSummary(investigation_id="i5", status="escalated"),
        ]
    )

    obs = await observer._observe_investigation_success_rate()

    assert obs.value == 0.4
    assert obs.triggers_policy is True  # 0.4 < 0.60


# ------------------------------------------------------------------
# Low quality intent rate
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_quality_intent_rate_all_good() -> None:
    """All high-quality intents produce rate 0.0."""
    observer = _make_observer(
        intents=[
            _make_intent(salience=0.7, confidence=0.8),
            _make_intent(salience=0.9, confidence=0.9),
        ]
    )

    obs = await observer._observe_low_quality_intent_rate()

    assert obs.kind == "low_quality_intent_rate"
    assert obs.value == 0.0
    assert obs.triggers_policy is False


@pytest.mark.asyncio
async def test_low_quality_intent_rate_mixed() -> None:
    """2 out of 4 are low quality = 0.5, triggers."""
    observer = _make_observer(
        intents=[
            _make_intent(salience=0.7, confidence=0.8),  # good
            _make_intent(salience=0.3, confidence=0.8),  # bad (salience < 0.4)
            _make_intent(salience=0.7, confidence=0.4),  # bad (confidence < 0.5)
            _make_intent(salience=0.5, confidence=0.6),  # good
        ]
    )

    obs = await observer._observe_low_quality_intent_rate()

    assert obs.value == 0.5
    assert obs.triggers_policy is False  # 0.5 == threshold, not strictly above


@pytest.mark.asyncio
async def test_low_quality_intent_rate_empty() -> None:
    """No intents = 0.0."""
    observer = _make_observer(intents=[])

    obs = await observer._observe_low_quality_intent_rate()

    assert obs.value == 0.0


# ------------------------------------------------------------------
# observe_all integration
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_all_returns_all_kinds() -> None:
    """observe_all() covers all expected observation kinds."""
    observer = _make_observer(
        intents=[_make_intent(topic="a"), _make_intent(topic="b")],
        actions=[
            _make_action(category="suggest", result_status="ok"),
        ],
        plugin_stats=[
            PluginStats(plugin_id="ha", total_calls=10, errors=1, window_seconds=3600.0),
        ],
        tokens=1000,
        investigations=[
            InvestigationSummary(investigation_id="i1", status="resolved"),
        ],
    )

    results = await observer.observe_all()

    kinds = {o.kind for o in results}
    assert "intent_repeat_rate" in kinds
    assert "intents_per_hour" in kinds
    assert "notification_engagement_rate" in kinds
    assert "plugin_error_rate" in kinds
    assert "token_per_useful_intent" in kinds
    assert "investigation_success_rate" in kinds
    assert "low_quality_intent_rate" in kinds


@pytest.mark.asyncio
async def test_observe_all_survives_individual_failure() -> None:
    """A broken intention store does not prevent other observations."""
    broken = _BrokenStore()
    observer = MetaObserver(
        intention_store=broken,
        action_store=_FakeActionStore([_make_action(category="suggest", result_status="ok")]),
        plugin_registry=_FakePluginRegistry(
            [
                PluginStats(plugin_id="p", total_calls=5, errors=1, window_seconds=3600.0),
            ]
        ),
        narrative_store=_FakeNarrativeStore(tokens=100, investigations=[]),
    )

    results = await observer.observe_all()

    # Should still get observations from non-broken stores.
    kinds = {o.kind for o in results}
    assert "notification_engagement_rate" in kinds
    assert "plugin_error_rate" in kinds
    assert "investigation_success_rate" in kinds
    # Broken store prevents intent-related observations.
    assert "intent_repeat_rate" not in kinds
