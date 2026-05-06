"""Reflection loop — scheduled L7 cycle.

Runs on a configurable cadence (default: weekly) and is also triggered on
demand (``coremind reflect --now``).  Each cycle:

1. Pulls reasoning cycles, intents, and actions from the last reflection
   window from L2 / L6.
2. Evaluates each prediction emitted by L4 against what actually happened.
3. Evaluates each executed action against user feedback (approvals,
   reversals, dismissals).
4. Updates calibration tables.
5. Learns procedural rules from outcomes (proposals only — activation
   requires user approval, applied by the rule learner downstream).
6. Produces a human-readable Markdown report.
7. Notifies via the configured channel.

Failures in one cycle never kill the loop: exceptions inside ``run_cycle``
surface to the caller (or are logged by the scheduler) but the next tick
proceeds normally, mirroring the resilience contract of L4 / L5.

Concrete implementations of the evaluator, calibrator, rule learner, and
report producer ports are provided by sibling modules (Tasks 4.2-4.5).
This module owns only orchestration.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coremind.action.schemas import Action
from coremind.errors import ReflectionError
from coremind.intention.schemas import Intent
from coremind.reasoning.schemas import ReasoningOutput
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    ReflectionReport,
    RuleLearningResult,
)

log = structlog.get_logger(__name__)


class _NarrativeRefreshOutput(BaseModel):
    """LLM output schema for narrative state refresh."""

    model_config = ConfigDict(frozen=True)

    user_mood_trend: str = Field(default="stable")
    recent_patterns: list[str] = Field(default_factory=list)
    active_concerns: list[str] = Field(default_factory=list)
    relationship_notes: str = Field(default="")

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


_SECONDS_PER_DAY: int = 24 * 60 * 60


class ReflectionLoopConfig(BaseModel):
    """Scheduler configuration for the reflection loop.

    Attributes:
        interval_seconds: Cadence between automatic cycles.  Defaults to
            seven days; the loop also supports on-demand invocation via
            :meth:`ReflectionLoop.run_cycle`.  Must be at least
            ``window_days * 86400`` so consecutive windows do not overlap
            (otherwise predictions/actions would be evaluated multiple
            times and bias the calibration counts).
        window_days: Width of the reflection window in days; each cycle
            evaluates events whose timestamp falls in the half-open
            interval ``[now - window_days, now)``.
        notify_on_cycle: When ``True``, the produced report is delivered
            through the configured :class:`ReportNotifier` at the end of
            every cycle.  ``False`` lets callers (CLI, dashboard) decide.
    """

    model_config = ConfigDict(frozen=True)

    interval_seconds: int = Field(default=7 * _SECONDS_PER_DAY, ge=60)
    window_days: int = Field(default=7, ge=1, le=90)
    notify_on_cycle: bool = True

    @model_validator(mode="after")
    def _no_overlapping_windows(self) -> Self:
        """Reject configurations whose cadence is faster than the window.

        A cadence shorter than the window width would re-evaluate the
        same predictions and actions in successive cycles, biasing the
        calibration table.  Implementations of the evaluator and rule
        learner ports rely on this invariant.
        """
        min_interval = self.window_days * _SECONDS_PER_DAY
        if self.interval_seconds < min_interval:
            msg = (
                f"interval_seconds={self.interval_seconds} is shorter than "
                f"window_days*86400={min_interval}; consecutive windows "
                "would overlap"
            )
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Port protocols
# ---------------------------------------------------------------------------


class CycleSource(Protocol):
    """Yields :class:`ReasoningOutput` entries within a window."""

    async def list_cycles(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[ReasoningOutput]:
        """Return cycles whose ``timestamp`` lies in ``[since, until)``.

        The interval is half-open so consecutive reflection windows do
        not double-count cycles whose timestamp coincides with a
        boundary.
        """
        ...


class IntentSource(Protocol):
    """Yields :class:`Intent` entries within a window."""

    async def list_intents(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[Intent]:
        """Return intents whose ``created_at`` lies in ``[since, until)``.

        Half-open by the same convention as :meth:`CycleSource.list_cycles`.
        """
        ...


class ActionFeed(Protocol):
    """Yields :class:`Action` entries within a window (typically from the
    audit journal)."""

    async def list_actions(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[Action]:
        """Return actions whose ``timestamp`` lies in ``[since, until)``.

        Half-open by the same convention as :meth:`CycleSource.list_cycles`.
        """
        ...


class PredictionEvaluator(Protocol):
    """Evaluates predictions emitted by L4 against L2 history (Task 4.2)."""

    async def evaluate(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_end: datetime,
    ) -> PredictionEvaluationResult:
        """Score the predictions in ``cycles`` as of ``window_end``."""
        ...


class FeedbackEvaluator(Protocol):
    """Evaluates actions against user feedback (approvals, reversals)."""

    async def evaluate(
        self,
        actions: list[Action],
        intents: list[Intent],
    ) -> FeedbackEvaluationResult:
        """Score how the user reacted to ``actions``."""
        ...


class CalibrationUpdater(Protocol):
    """Updates calibration tables and returns the headline summary
    (Task 4.3)."""

    async def update(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> CalibrationResult:
        """Update calibration storage and return the latest summary.

        ``cycles`` provides the cycle-id → model mapping for the current
        reflection window.  ``window_start`` / ``window_end`` describe
        the loop's window so adapters that need it (e.g. for log
        context) do not consult the wall clock independently.

        At-most-once semantics across windows are the implementation's
        responsibility — the loop guarantees non-overlapping windows
        but late-arriving evaluations may still cross window boundaries.
        """
        ...


class RuleLearner(Protocol):
    """Promotes / deprecates procedural rules from the cycle's outcomes
    (Task 4.4)."""

    async def learn(
        self,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
    ) -> RuleLearningResult:
        """Run rule promotion / deprecation logic for this window."""
        ...


class ReportProducer(Protocol):
    """Renders the aggregated reflection data as Markdown (Task 4.5)."""

    async def produce(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
        predictions: PredictionEvaluationResult,
        feedback: FeedbackEvaluationResult,
        calibration: CalibrationResult,
        rules: RuleLearningResult,
    ) -> str:
        """Return a human-readable Markdown report."""
        ...


class ReportNotifier(Protocol):
    """Delivers a finished report to the user via a notification channel."""

    async def deliver(self, report: ReflectionReport) -> None:
        """Send ``report`` to the user.  Failures must raise so the loop
        can log and continue."""
        ...


# ---------------------------------------------------------------------------
# ReflectionLoop
# ---------------------------------------------------------------------------


class ReflectionLoop:
    """Scheduled L7 reflection cycle.

    Args:
        cycle_source: Source of recent reasoning cycles.
        intent_source: Source of recent intents.
        action_feed: Source of recent actions (typically the audit journal).
        prediction_evaluator: Port that scores predictions against history.
        feedback_evaluator: Port that scores actions against user feedback.
        calibration_updater: Port that maintains the calibration table.
        rule_learner: Port that proposes / deprecates procedural rules.
        report_producer: Port that renders Markdown reports.
        notifier: Optional notification port; when ``None`` the report is
            still produced but not delivered.
        narrative_state: Optional narrative identity store.  When provided,
            every reflection cycle asks the narrative LLM to refresh the
            user's persistent life context.
        narrative_llm: Optional LLM for narrative refresh.  Used together
            with ``narrative_state``.
        config: Scheduler parameters.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        cycle_source: CycleSource,
        intent_source: IntentSource,
        action_feed: ActionFeed,
        prediction_evaluator: PredictionEvaluator,
        feedback_evaluator: FeedbackEvaluator,
        calibration_updater: CalibrationUpdater,
        rule_learner: RuleLearner,
        report_producer: ReportProducer,
        *,
        notifier: ReportNotifier | None = None,
        narrative_state: object | None = None,
        narrative_llm: object | None = None,
        config: ReflectionLoopConfig | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._cycles = cycle_source
        self._intents = intent_source
        self._actions = action_feed
        self._predictions = prediction_evaluator
        self._feedback = feedback_evaluator
        self._calibration = calibration_updater
        self._rules = rule_learner
        self._report = report_producer
        self._notifier = notifier
        self._narrative_state = narrative_state
        self._narrative_llm = narrative_llm
        self._config = config or ReflectionLoopConfig()
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def config(self) -> ReflectionLoopConfig:
        """Return the scheduler configuration."""
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background scheduler task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._scheduler(), name="coremind.reflection")

    async def stop(self) -> None:
        """Stop the scheduler, cancelling any in-flight cycle.

        Cancelling is required for daemon shutdown to remain bounded:
        :meth:`run_cycle` awaits ports (SurrealDB, LLM, Telegram) that
        can stall indefinitely under failure.  ``stop`` therefore signals
        the stop event *and* cancels the scheduler task, letting
        :class:`asyncio.CancelledError` propagate through the awaited
        port and unwind the cycle.
        """
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _scheduler(self) -> None:
        """Run cycles until :meth:`stop` is called."""
        interval = self._config.interval_seconds
        while not self._stop_event.is_set():
            try:
                await self.run_cycle()
            except ReflectionError:
                log.error("reflection.cycle_failed", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Failures in one cycle never kill the loop.
                log.exception("reflection.cycle_unexpected")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Cycle execution
    # ------------------------------------------------------------------

    async def run_cycle(self) -> ReflectionReport:
        """Execute a single reflection cycle and return the report.

        Raises:
            ReflectionError: When any port fails.  The original exception
                is chained as the cause.
        """
        now = self._clock()
        window_start = now - timedelta(days=self._config.window_days)
        cycle_id = _make_cycle_id(now)
        log.info(
            "reflection.cycle.start",
            cycle_id=cycle_id,
            window_start=window_start.isoformat(),
            window_end=now.isoformat(),
        )

        cycles, intents, actions = await self._collect(window_start, now)

        try:
            prediction_result = await self._predictions.evaluate(cycles, window_end=now)
            feedback_result = await self._feedback.evaluate(actions, intents)
            calibration_result = await self._calibration.update(
                cycles,
                window_start=window_start,
                window_end=now,
            )
            rule_result = await self._rules.learn(cycles, intents, actions)
            await self._refresh_narrative(cycles, intents, actions)
            markdown = await self._report.produce(
                window_start=window_start,
                window_end=now,
                cycles=cycles,
                intents=intents,
                actions=actions,
                predictions=prediction_result,
                feedback=feedback_result,
                calibration=calibration_result,
                rules=rule_result,
            )
        except ReflectionError:
            raise
        except Exception as exc:
            raise ReflectionError("reflection cycle evaluation failed") from exc

        report = ReflectionReport(
            cycle_id=cycle_id,
            window_start=window_start,
            window_end=now,
            cycles_evaluated=len(cycles),
            intents_evaluated=len(intents),
            actions_evaluated=len(actions),
            predictions=prediction_result,
            feedback=feedback_result,
            calibration=calibration_result,
            rules=rule_result,
            markdown=markdown,
        )

        if self._config.notify_on_cycle and self._notifier is not None:
            try:
                await self._notifier.deliver(report)
            except Exception:
                log.exception("reflection.notify_failed", cycle_id=cycle_id)

        log.info(
            "reflection.cycle.done",
            cycle_id=cycle_id,
            cycles=report.cycles_evaluated,
            intents=report.intents_evaluated,
            actions=report.actions_evaluated,
            predictions_evaluated=prediction_result.evaluated,
            proposed_rules=len(rule_result.proposed_rule_ids),
            deprecated_rules=len(rule_result.deprecated_rule_ids),
        )
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_narrative(
        self,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
    ) -> None:
        """Ask the narrative LLM to refresh the user's persistent narrative state.

        Builds a prompt from the current narrative and this week's data,
        then updates the narrative store with the LLM's response.
        No-op if narrative_state or narrative_llm is not configured.
        """
        if self._narrative_state is None or self._narrative_llm is None:
            return

        narrative_module = self._narrative_state
        try:
            current = narrative_module.get_current()  # type: ignore[attr-defined]
        except Exception:
            log.warning("reflection.narrative_read_failed", exc_info=True)
            return

        patterns_summary = "\n".join(
            f"- {p.description}"
            for c in (cycles[-10:] if cycles else [])[:20]
            for p in c.patterns
        )
        anomalies_summary = "\n".join(
            f"- {a.description} (severity: {a.severity})"
            for c in (cycles[-10:] if cycles else [])[:20]
            for a in c.anomalies
        )[:2000]
        predictions_summary = "\n".join(
            f"- {p.hypothesis} (confidence: {p.confidence})"
            for c in (cycles[-10:] if cycles else [])[:20]
            for p in c.predictions
        )[:2000]

        prompt = (
            f"Based on this week's data, update the narrative state describing "
            f"the user's current life context.\n\n"
            "## Current Narrative State\n"
            f"- Mood trend: {current.user_mood_trend}\n"
            "- Recent patterns:\n"
            + "\n".join(f"  - {p.text}" for p in (current.recent_patterns or [])[:10])
            + "\n- Active concerns:\n"
            + "\n".join(f"  - {c.text}" for c in (current.active_concerns or [])[:10])
            + f"\n- Relationship notes: {current.relationship_notes or '(none)'}\n\n"
            "## This Week's Observations\n"
            f"### Patterns detected\n{patterns_summary or '(none)'}\n\n"
            f"### Anomalies detected\n{anomalies_summary or '(none)'}\n\n"
            f"### Predictions made\n{predictions_summary or '(none)'}\n\n"
            f"### Intents formed\n{len(intents)} intents\n\n"
            f"### Actions executed\n{len(actions)} actions\n\n"
            "Update the narrative state: has mood trend changed? "
            "What new patterns or concerns have emerged? "
            "Update relationship notes if relevant. "
            "Keep patterns and concerns lists to at most 10 items each, "
            "pruning stale ones. Retain important long-term context.\n\n"
            "Output a JSON object with these fields: "
            "user_mood_trend, recent_patterns (list), active_concerns (list), "
            "relationship_notes."
        )

        try:
            result = await self._narrative_llm.complete_structured(  # type: ignore[attr-defined]
                layer="reflection",
                system=(
                    "You are the narrative identity layer of CoreMind. "
                    "You maintain a living, evolving understanding of the user's life. "
                    "You are not just summarizing data — you are building a MODEL of who "
                    "this person is, what matters to them, and how their life is changing.\n\n"
                    "Your narrative should capture:\n"
                    "- IDENTITY: Who is this person? What defines them right now?\n"
                    "- TRENDS: What is changing in their life? Better? Worse? Stable?\n"
                    "- CONCERNS: What are they worried about or working on?\n"
                    "- PATTERNS: What rhythms define their days?\n"
                    "- RELATIONSHIPS: Who matters to them? What's happening with those people?\n"
                    "- ENVIRONMENT: What's happening in their home? Their cats? Their space?\n\n"
                    "Be specific. Use names, numbers, places. \"Sleep quality declining\" is vague. "
                    "\"Deep sleep dropped from 1h30 to 45min this week — correlated with bedroom "
                    "temperature staying above 25°C past midnight\" is intelligence.\n\n"
                    "Prune stale information. If something hasn't been relevant for 2+ weeks, "
                    "let it go. Keep what matters NOW.\n\n"
                    "Output valid JSON only."
                ),
                user=prompt,
                response_model=_NarrativeRefreshOutput,
            )
        except Exception:
            log.warning("reflection.narrative_llm_failed", exc_info=True)
            return

        try:
            await narrative_module.update(  # type: ignore[attr-defined]
                user_mood_trend=result.user_mood_trend,
                recent_patterns=result.recent_patterns,
                active_concerns=result.active_concerns,
                relationship_notes=result.relationship_notes,
            )
            log.info("reflection.narrative_refreshed")
        except Exception:
            log.warning("reflection.narrative_update_failed", exc_info=True)

    async def _collect(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[ReasoningOutput], list[Intent], list[Action]]:
        """Pull cycles, intents, and actions for the reflection window.

        Each source is awaited concurrently; the first to fail is
        re-raised as :class:`ReflectionError` carrying the offending
        source name so operators can diagnose from logs alone.
        """
        sources: list[tuple[str, asyncio.Future[object]]] = [
            (
                "cycle_source",
                asyncio.ensure_future(
                    self._cycles.list_cycles(since=window_start, until=window_end),
                ),
            ),
            (
                "intent_source",
                asyncio.ensure_future(
                    self._intents.list_intents(since=window_start, until=window_end),
                ),
            ),
            (
                "action_feed",
                asyncio.ensure_future(
                    self._actions.list_actions(since=window_start, until=window_end),
                ),
            ),
        ]
        results = await asyncio.gather(
            *(fut for _, fut in sources),
            return_exceptions=True,
        )
        for (name, _), result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                raise ReflectionError(
                    f"failed to collect reflection inputs from {name}",
                ) from result
        cycles, intents, actions = results
        # ``return_exceptions=True`` widens the static type to ``object``;
        # narrow back now that we've handled the exception case above.
        assert isinstance(cycles, list)  # noqa: S101 — internal invariant
        assert isinstance(intents, list)  # noqa: S101 — internal invariant
        assert isinstance(actions, list)  # noqa: S101 — internal invariant
        return cycles, intents, actions


def _make_cycle_id(now: datetime) -> str:
    """Return a unique cycle id for ``now``.

    Format: ``reflection-YYYYMMDDTHHMMSSZ-XXXX`` where ``XXXX`` is a
    4-hex-character random suffix.  The timestamp keeps logs and audit
    reads sortable, and the suffix prevents collisions between a
    scheduled tick and a manual ``coremind reflect --now`` invocation
    that happen within the same second.
    """
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(2)
    return f"reflection-{timestamp}-{suffix}"
