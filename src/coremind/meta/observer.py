"""MetaObserver -- collects system performance observations from L1-L7.

The observer is read-only: it queries stores and produces
:class:`~coremind.meta.schemas.MetaObservation` objects but never mutates
state.  Downstream consumers (adjuster, persistence) act on the observations.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from coremind.action.autonomy import classify_domain
from coremind.meta.protocols import (
    ActionStoreProtocol,
    IntentionStoreProtocol,
    NarrativeStoreProtocol,
    PluginRegistryProtocol,
)
from coremind.meta.schemas import MetaObservation

log = structlog.get_logger(__name__)

# ------------------------------------------------------------------
# Time windows for each observation kind
# ------------------------------------------------------------------

_INTENT_REPEAT_WINDOW = timedelta(hours=6)
_INTENTS_PER_HOUR_WINDOW = timedelta(hours=6)
_NOTIFICATION_WINDOW = timedelta(days=7)
_APPROVAL_WINDOW = timedelta(days=30)
_PLUGIN_ERROR_WINDOW = timedelta(hours=1)
_TOKEN_WINDOW = timedelta(hours=24)
_INVESTIGATION_WINDOW = timedelta(days=7)
_LOW_QUALITY_WINDOW = timedelta(hours=24)

# ------------------------------------------------------------------
# Thresholds (match DEFAULT_POLICIES observation_kind expectations)
# ------------------------------------------------------------------

_THRESHOLD_INTENT_REPEAT = 0.30
_THRESHOLD_INTENTS_PER_HOUR = 1.0
_THRESHOLD_NOTIFICATION_ENGAGEMENT = 0.30
_THRESHOLD_DOMAIN_APPROVAL = 0.80
_THRESHOLD_PLUGIN_ERROR = 0.50
_THRESHOLD_TOKEN_PER_INTENT = 5000.0
_THRESHOLD_INVESTIGATION_SUCCESS = 0.60
_THRESHOLD_LOW_QUALITY = 0.50

# Cutoffs for "low quality" intent classification.
_LOW_SALIENCE_CUTOFF = 0.4
_LOW_CONFIDENCE_CUTOFF = 0.5

# Minimum actions required to compute domain approval rate.
_MIN_DOMAIN_ACTIONS = 10


class MetaObserver:
    """Collects observations about system performance from all layers.

    Each observation kind is implemented as a private async method. The
    public :meth:`observe_all` orchestrates them and handles individual
    failures gracefully.
    """

    def __init__(
        self,
        intention_store: IntentionStoreProtocol,
        action_store: ActionStoreProtocol,
        plugin_registry: PluginRegistryProtocol,
        narrative_store: NarrativeStoreProtocol,
    ) -> None:
        self._intention_store = intention_store
        self._action_store = action_store
        self._plugin_registry = plugin_registry
        self._narrative_store = narrative_store

    async def observe_all(self) -> list[MetaObservation]:
        """Collect all observation kinds.

        Individual failures are logged and skipped — a broken store never
        blocks observations from other stores.
        """
        observations: list[MetaObservation] = []

        collectors: list[tuple[str, Any]] = [
            ("intent_repeat_rate", self._observe_intent_repeat_rate),
            ("intents_per_hour", self._observe_intents_per_hour),
            ("notification_engagement_rate", self._observe_notification_engagement_rate),
            ("domain_approval_rate", self._observe_domain_approval_rates),
            ("plugin_error_rate", self._observe_plugin_error_rates),
            ("token_per_useful_intent", self._observe_token_per_useful_intent),
            ("investigation_success_rate", self._observe_investigation_success_rate),
            ("low_quality_intent_rate", self._observe_low_quality_intent_rate),
        ]

        for kind, collector in collectors:
            try:
                result = await collector()
                if isinstance(result, list):
                    observations.extend(result)
                else:
                    observations.append(result)
            except Exception:
                log.warning("meta_observer.collector_failed", kind=kind, exc_info=True)

        return observations

    # ------------------------------------------------------------------
    # Private observation methods
    # ------------------------------------------------------------------

    async def _observe_intent_repeat_rate(self) -> MetaObservation:
        """Compute intent repetition rate over the repeat window.

        Groups intents by md5(topic + sorted(key_entities)). Value is the
        fraction of intents that are duplicates of an earlier one in the window.
        """
        since = datetime.now(UTC) - _INTENT_REPEAT_WINDOW
        intents = await self._intention_store.recent(since=since)

        if not intents:
            return self._make(
                kind="intent_repeat_rate",
                value=0.0,
                threshold=_THRESHOLD_INTENT_REPEAT,
                window=_INTENT_REPEAT_WINDOW,
            )

        seen: dict[str, int] = {}
        for intent in intents:
            key = self._intent_fingerprint(intent)
            seen[key] = seen.get(key, 0) + 1

        total = len(intents)
        repeats = sum(count - 1 for count in seen.values())
        value = repeats / total

        return self._make(
            kind="intent_repeat_rate",
            value=value,
            threshold=_THRESHOLD_INTENT_REPEAT,
            window=_INTENT_REPEAT_WINDOW,
        )

    async def _observe_intents_per_hour(self) -> MetaObservation:
        """Compute average intents per hour over the window."""
        since = datetime.now(UTC) - _INTENTS_PER_HOUR_WINDOW
        intents = await self._intention_store.recent(since=since)

        hours = _INTENTS_PER_HOUR_WINDOW.total_seconds() / 3600.0
        value = len(intents) / hours

        return self._make(
            kind="intents_per_hour",
            value=value,
            threshold=_THRESHOLD_INTENTS_PER_HOUR,
            window=_INTENTS_PER_HOUR_WINDOW,
        )

    async def _observe_notification_engagement_rate(self) -> MetaObservation:
        """Compute notification engagement rate.

        Engagement is proxied by suggest-category actions that received a
        successful result (ok, noop, or dispatched).
        """
        now = datetime.now(UTC)
        since = now - _NOTIFICATION_WINDOW
        actions = await self._action_store.list_actions(since=since, until=now)

        suggestions = [a for a in actions if getattr(a, "category", None) == "suggest"]

        if not suggestions:
            return self._make(
                kind="notification_engagement_rate",
                value=0.5,
                threshold=_THRESHOLD_NOTIFICATION_ENGAGEMENT,
                window=_NOTIFICATION_WINDOW,
            )

        engaged = sum(1 for a in suggestions if self._is_engaged(a))
        value = engaged / len(suggestions)

        return self._make(
            kind="notification_engagement_rate",
            value=value,
            threshold=_THRESHOLD_NOTIFICATION_ENGAGEMENT,
            window=_NOTIFICATION_WINDOW,
        )

    async def _observe_domain_approval_rates(self) -> list[MetaObservation]:
        """Compute per-domain approval rate for ask-category actions.

        Domains with fewer than _MIN_DOMAIN_ACTIONS are skipped.
        Returns one MetaObservation per qualifying domain.
        """
        now = datetime.now(UTC)
        since = now - _APPROVAL_WINDOW
        actions = await self._action_store.list_actions(since=since, until=now)

        ask_actions = [a for a in actions if getattr(a, "category", None) == "ask"]

        # Group by domain.
        domain_groups: dict[str, list[Any]] = {}
        for action in ask_actions:
            action_class = getattr(action, "action_class", "unknown")
            domain = classify_domain(action_class)
            domain_groups.setdefault(domain, []).append(action)

        observations: list[MetaObservation] = []
        for domain, group in domain_groups.items():
            if len(group) < _MIN_DOMAIN_ACTIONS:
                continue

            approved = sum(1 for a in group if self._is_approved(a))
            value = approved / len(group)

            observations.append(
                self._make(
                    kind="domain_approval_rate",
                    value=value,
                    threshold=_THRESHOLD_DOMAIN_APPROVAL,
                    window=_APPROVAL_WINDOW,
                    metadata={"domain": domain},
                )
            )

        return observations

    async def _observe_plugin_error_rates(self) -> list[MetaObservation]:
        """Compute per-plugin error rate.

        Plugins with zero calls in the window are skipped.
        """
        stats_list = await self._plugin_registry.get_all_stats(_PLUGIN_ERROR_WINDOW)

        observations: list[MetaObservation] = []
        for stats in stats_list:
            if stats.total_calls == 0:
                continue

            value = stats.errors / stats.total_calls

            observations.append(
                self._make(
                    kind="plugin_error_rate",
                    value=value,
                    threshold=_THRESHOLD_PLUGIN_ERROR,
                    window=_PLUGIN_ERROR_WINDOW,
                    metadata={"plugin_id": stats.plugin_id},
                )
            )

        return observations

    async def _observe_token_per_useful_intent(self) -> MetaObservation:
        """Compute tokens consumed per useful intent.

        Useful intents are those with status in (done, executing, approved).
        """
        since = datetime.now(UTC) - _TOKEN_WINDOW
        tokens = await self._narrative_store.total_tokens(_TOKEN_WINDOW)
        intents = await self._intention_store.recent(since=since)

        useful_statuses = {"done", "executing", "approved"}
        useful_count = sum(1 for i in intents if getattr(i, "status", None) in useful_statuses)

        value = tokens / max(useful_count, 1)

        return self._make(
            kind="token_per_useful_intent",
            value=value,
            threshold=_THRESHOLD_TOKEN_PER_INTENT,
            window=_TOKEN_WINDOW,
        )

    async def _observe_investigation_success_rate(self) -> MetaObservation:
        """Compute investigation resolution rate.

        Returns 1.0 when there are no investigations (nothing to worry about).
        """
        investigations = await self._narrative_store.list_investigations(_INVESTIGATION_WINDOW)

        if not investigations:
            return self._make(
                kind="investigation_success_rate",
                value=1.0,
                threshold=_THRESHOLD_INVESTIGATION_SUCCESS,
                window=_INVESTIGATION_WINDOW,
            )

        resolved = sum(1 for i in investigations if i.status == "resolved")
        value = resolved / len(investigations)

        return self._make(
            kind="investigation_success_rate",
            value=value,
            threshold=_THRESHOLD_INVESTIGATION_SUCCESS,
            window=_INVESTIGATION_WINDOW,
        )

    async def _observe_low_quality_intent_rate(self) -> MetaObservation:
        """Compute fraction of low-quality intents.

        An intent is low-quality if salience < 0.4 or confidence < 0.5.
        """
        since = datetime.now(UTC) - _LOW_QUALITY_WINDOW
        intents = await self._intention_store.recent(since=since)

        if not intents:
            return self._make(
                kind="low_quality_intent_rate",
                value=0.0,
                threshold=_THRESHOLD_LOW_QUALITY,
                window=_LOW_QUALITY_WINDOW,
            )

        low_quality = sum(
            1
            for i in intents
            if getattr(i, "salience", 1.0) < _LOW_SALIENCE_CUTOFF
            or getattr(i, "confidence", 1.0) < _LOW_CONFIDENCE_CUTOFF
        )
        value = low_quality / len(intents)

        return self._make(
            kind="low_quality_intent_rate",
            value=value,
            threshold=_THRESHOLD_LOW_QUALITY,
            window=_LOW_QUALITY_WINDOW,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _intent_fingerprint(intent: Any) -> str:
        """Compute a deduplication fingerprint for an intent."""
        question = getattr(intent, "question", None)
        if question is None:
            return getattr(intent, "id", "unknown")

        topic = getattr(question, "text", "")
        grounding = getattr(question, "grounding", [])
        entities = sorted(str(e) for e in grounding)
        raw = f"{topic}|{'|'.join(entities)}"
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

    @staticmethod
    def _is_engaged(action: Any) -> bool:
        """Check whether a suggest-category action was engaged with."""
        result = getattr(action, "result", None)
        if result is None:
            return False
        status = getattr(result, "status", None)
        return status in ("ok", "noop", "dispatched")

    @staticmethod
    def _is_approved(action: Any) -> bool:
        """Check whether an ask-category action was approved."""
        result = getattr(action, "result", None)
        if result is None:
            return False
        status = getattr(result, "status", None)
        return status in ("ok", "noop", "dispatched")

    @staticmethod
    def _make(
        *,
        kind: str,
        value: float,
        threshold: float,
        window: timedelta,
        metadata: dict[str, Any] | None = None,
    ) -> MetaObservation:
        """Create a MetaObservation with triggers_policy derived from value vs threshold."""
        # Determine if the observation crosses its threshold.
        # For most metrics, "above threshold" triggers. For engagement/success
        # rates, "below threshold" triggers.
        below_is_bad = kind in (
            "notification_engagement_rate",
            "investigation_success_rate",
            "intents_per_hour",
        )

        triggers = value < threshold if below_is_bad else value > threshold

        return MetaObservation(
            kind=kind,
            value=value,
            threshold=threshold,
            window_seconds=window.total_seconds(),
            triggers_policy=triggers,
            metadata=metadata or {},
        )
