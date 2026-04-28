"""Markdown weekly report producer — Task 4.5.

Renders the aggregated outputs of one L7 reflection cycle as a
human-readable Markdown document, mirroring the layout in
``docs/phases/PHASE_4_REFLECTION_ECOSYSTEM.md`` §4.5.

Implements the :class:`coremind.reflection.loop.ReportProducer`
Protocol; the loop owns orchestration and supplies the cycle inputs.

The producer is **read-only**: it never mutates L2/L3/L6 state, never
calls effectors, and never raises on empty windows.  When a section has
no content it is rendered with an explicit "no X this week" line so
the cadence stays observable.

Rule proposals carry rich descriptions only on the
:class:`coremind.reflection.rule_learner.RuleProposal` row, not on the
``RuleLearningResult`` summary returned by the learner port.  When a
:class:`RuleProposalStore` is supplied at construction time, the
producer enriches the "Proposed new rules" / "Rules I should probably
deprecate" sections with the full proposal records by id; otherwise it
falls back to listing proposal ids only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final

import structlog

from coremind.action.schemas import Action
from coremind.intention.schemas import Intent
from coremind.reasoning.schemas import Anomaly, Pattern, ReasoningOutput
from coremind.reflection.rule_learner import RuleProposal, RuleProposalStore
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    RuleLearningResult,
)

log = structlog.get_logger(__name__)


_TITLE: Final[str] = "# CoreMind — Weekly Reflection"
_DATE_FMT: Final[str] = "%Y-%m-%d"


class MarkdownReportProducer:
    """Render a reflection cycle as a Markdown document.

    Args:
        proposal_store: Optional read-side of the rule-proposal store.
            When provided, the producer fetches full
            :class:`RuleProposal` records to enrich the proposal
            sections.  When ``None`` (e.g. in early bring-up wiring),
            those sections list proposal ids only.
    """

    def __init__(self, proposal_store: RuleProposalStore | None = None) -> None:
        self._proposal_store = proposal_store

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
        """Render a Markdown report. See class docstring for guarantees."""
        proposals_by_id = await self._fetch_proposals(rules)

        sections: list[str] = [
            _render_header(window_start, window_end),
            _render_highlights(cycles, intents, actions, feedback),
            _render_predictions(predictions, calibration),
            _render_patterns(cycles),
            _render_proposed_rules(rules.proposed_rule_ids, proposals_by_id),
            _render_deprecations(rules.deprecated_rule_ids, proposals_by_id),
            _render_pending_questions(intents),
        ]
        return "\n\n".join(sections).rstrip() + "\n"

    async def _fetch_proposals(
        self,
        rules: RuleLearningResult,
    ) -> dict[str, RuleProposal]:
        """Return proposals keyed by id for the cycle's referenced ids.

        Failures to read the proposal store are swallowed and logged:
        a degraded report is preferable to no report at all, since the
        caller may be a notifier that fails closed on empty payloads.
        """
        if self._proposal_store is None:
            return {}
        wanted = set(rules.proposed_rule_ids) | set(rules.deprecated_rule_ids)
        if not wanted:
            return {}
        try:
            pending = await self._proposal_store.list_pending()
        except (OSError, RuntimeError):
            # Narrow-but-broad: covers in-memory ledger errors and the
            # network/IO failure modes of any real store adapter
            # (SurrealDB, filesystem-backed, etc.) without swallowing
            # programming bugs (TypeError/AttributeError/...).
            # A degraded report is preferable to no report at all,
            # since the caller may be a notifier that fails closed on
            # empty payloads.
            log.exception("reflection.report.proposal_lookup_failed")
            return {}
        return {p.id: p for p in pending if p.id in wanted}


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(window_start: datetime, window_end: datetime) -> str:
    return (
        f"{_TITLE}\nWeek of {window_start.strftime(_DATE_FMT)} → {window_end.strftime(_DATE_FMT)}"
    )


def _render_highlights(
    cycles: Sequence[ReasoningOutput],
    intents: Sequence[Intent],
    actions: Sequence[Action],
    feedback: FeedbackEvaluationResult,
) -> str:
    lines: list[str] = ["## Highlights"]

    if cycles:
        models = Counter(c.model_used for c in cycles)
        # Order by usage so the dominant model leads, matching the spec example.
        model_summary = ", ".join(f"{m}: {n}" for m, n in models.most_common())
        lines.append(f"- {len(cycles)} reasoning cycles executed ({model_summary})")
    else:
        lines.append("- 0 reasoning cycles executed this week")

    if intents:
        by_category = Counter(i.category for i in intents)
        ask = by_category.get("ask", 0)
        autonomous = by_category.get("safe", 0) + by_category.get("suggest", 0)
        lines.append(
            f"- {len(intents)} intents generated, "
            f"{autonomous} eligible for autonomous execution, "
            f"{ask} required approval"
        )
    else:
        lines.append("- 0 intents generated this week")

    if actions:
        lines.append(
            f"- {len(actions)} actions dispatched, {feedback.reversed} reversed by the user"
        )
    else:
        lines.append("- 0 actions dispatched this week")

    return "\n".join(lines)


def _render_predictions(
    predictions: PredictionEvaluationResult,
    calibration: CalibrationResult,
) -> str:
    lines: list[str] = ["## Predictions scoreboard"]
    if predictions.evaluated == 0:
        lines.append("- No predictions evaluated this week")
    else:
        lines.append(f"- {predictions.evaluated} predictions evaluated")
        lines.append(
            f"- {predictions.correct} correct, "
            f"{predictions.wrong} wrong, "
            f"{predictions.undetermined} undetermined"
        )
    if calibration.brier_score is None:
        lines.append(
            f"- Brier score: not yet available (samples so far: {calibration.sample_count})"
        )
    else:
        lines.append(
            f"- Brier score: {calibration.brier_score:.3f} "
            f"(over {calibration.sample_count} samples)"
        )
    return "\n".join(lines)


def _render_patterns(cycles: Sequence[ReasoningOutput]) -> str:
    lines: list[str] = ["## New patterns and anomalies"]
    patterns = list(_unique_patterns(c.patterns for c in cycles))
    anomalies = list(_unique_anomalies(c.anomalies for c in cycles))
    if not patterns and not anomalies:
        lines.append("- No new patterns or anomalies surfaced this week")
        return "\n".join(lines)
    for p in patterns:
        lines.append(f"- {p.description} (confidence {p.confidence:.0%})")
    for a in anomalies:
        lines.append(f"- Anomaly ({a.severity}): {a.description}")
    return "\n".join(lines)


def _render_proposed_rules(
    ids: Sequence[str],
    proposals_by_id: dict[str, RuleProposal],
) -> str:
    lines: list[str] = ["## Proposed new rules (awaiting your approval)"]
    if not ids:
        lines.append("- No new rule proposals this week")
        return "\n".join(lines)
    for idx, proposal_id in enumerate(ids, start=1):
        proposal = proposals_by_id.get(proposal_id)
        if proposal is None:
            lines.append(f"{idx}. {proposal_id} (details unavailable)")
        else:
            lines.append(f"{idx}. {proposal.description}")
    return "\n".join(lines)


def _render_deprecations(
    ids: Sequence[str],
    proposals_by_id: dict[str, RuleProposal],
) -> str:
    lines: list[str] = ["## Rules I should probably deprecate"]
    if not ids:
        lines.append("- No rules flagged for deprecation this week")
        return "\n".join(lines)
    for idx, proposal_id in enumerate(ids, start=1):
        proposal = proposals_by_id.get(proposal_id)
        if proposal is None:
            lines.append(f"{idx}. {proposal_id} (details unavailable)")
        else:
            lines.append(f"{idx}. {proposal.description}")
    return "\n".join(lines)


def _render_pending_questions(intents: Sequence[Intent]) -> str:
    lines: list[str] = ["## Things I want to ask"]
    # Surface anything the user may need to answer: every ask-class intent
    # (regardless of lifecycle) plus any non-ask intent gated on approval.
    # Internal-only states (pending/executing) on safe/suggest intents stay
    # out — their `question.text` is reasoning shorthand, not a user prompt.
    pending = [i for i in intents if i.category == "ask" or i.status == "pending_approval"]
    if not pending:
        lines.append("- No outstanding questions")
        return "\n".join(lines)
    for intent in pending:
        lines.append(f"- {intent.question.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unique_patterns(
    pattern_lists: Iterable[Sequence[Pattern]],
) -> Iterable[Pattern]:
    """Yield patterns deduplicated by ``id``, preserving first-seen order.

    Note: when later cycles in the window restate a pattern with updated
    confidence or description, the first occurrence wins. This is
    intentional for a weekly digest — the report is a high-level recap,
    not a live view; freshness lives in the dashboard (Task 4.6).
    """
    seen: set[str] = set()
    for batch in pattern_lists:
        for p in batch:
            if p.id in seen:
                continue
            seen.add(p.id)
            yield p


def _unique_anomalies(
    anomaly_lists: Iterable[Sequence[Anomaly]],
) -> Iterable[Anomaly]:
    """Yield anomalies deduplicated by ``id``, preserving first-seen order.

    See :func:`_unique_patterns` for the first-seen rationale.
    """
    seen: set[str] = set()
    for batch in anomaly_lists:
        for a in batch:
            if a.id in seen:
                continue
            seen.add(a.id)
            yield a
