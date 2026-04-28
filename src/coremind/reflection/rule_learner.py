"""Rule learner — Task 4.4.

Promotes and deprecates :class:`coremind.memory.procedural.Rule`
candidates from the outcomes of a reflection window.

Two policies are implemented:

* **Promotion** — when the same ``(action_class, operation)`` pair has
  been observed at least :attr:`RuleLearnerConfig.promotion_min_observations`
  times across all reflection windows seen so far *and* its empirical
  success rate is at least
  :attr:`RuleLearnerConfig.promotion_min_success_rate`, the learner
  emits a ``"promote"`` :class:`RuleProposal`.  The proposal carries a
  fully-formed :class:`Rule` preview, but activation is *never*
  automatic — proposals are queued for human approval, mirroring the
  agency contract for ``ask`` intents.

* **Deprecation** — for every active rule supplied by the
  :class:`RuleSource`, if its ``applied_count`` has reached
  :attr:`RuleLearnerConfig.deprecation_min_evaluations` *and* its
  ``success_rate`` has fallen below
  :attr:`RuleLearnerConfig.deprecation_max_success_rate`, the learner
  emits a ``"deprecate"`` :class:`RuleProposal`.  Again, this only
  flags the rule for user review; the procedural store is never
  mutated by this module.

Both kinds of proposals are persisted through the
:class:`RuleProposalStore` port.  Stable proposal ids
(``promote-<action_class>-<operation>`` /
``deprecate-<rule_id>``) make re-emission idempotent across cycles —
re-running a window must not duplicate proposals.

Cross-window state lives behind the :class:`CandidateLedger` port.
Single-cycle promotion would churn on small samples, so the ledger
accumulates per-key counts across windows and remembers which keys
already produced a proposal.

Per the project's "no DB writes outside ``store.py``" rule, the
SurrealDB-backed adapters live in :mod:`coremind.reflection.store`
(follow-up — same status as the prediction-evaluation store today).
The in-memory implementations shipped here keep the L7 loop runnable
in tests and during early Phase 4 wiring; they are **test / wiring
scaffolding only** and must not be used by a long-running daemon —
:class:`InMemoryCandidateLedger`'s deduplication set grows without
bound, and pending proposals do not survive a process restart.

Known v1 simplifications (tracked against
``docs/phases/PHASE_4_REFLECTION_ECOSYSTEM.md`` §4.4):

* The promotion key is the ``(action_class, operation)`` pair only;
  the upstream reasoning *cycle* and *intent* arguments are accepted
  for protocol compatibility but not yet folded into the candidate
  pattern.  Pattern-grounded promotion is a follow-up.
* The synthesised :class:`Rule.trigger` carries only an
  ``action_class`` precondition.  Promoted rules are emitted with
  :attr:`Rule.confidence` set to ``0.0`` and the description warns
  the user that the trigger must be hand-edited before activation —
  proposals never auto-activate, so this stays inside the agency
  contract.
* Procedural :class:`Rule` does not yet carry distinct
  ``last_evaluated_at`` / ``evaluation_count`` fields; deprecation
  uses :attr:`Rule.applied_count` as the proxy.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coremind.action.schemas import Action, ActionOutcome
from coremind.errors import ReflectionError
from coremind.intention.schemas import Intent
from coremind.memory.procedural import Rule
from coremind.reasoning.schemas import ReasoningOutput
from coremind.reflection.loop import RuleLearner
from coremind.reflection.schemas import RuleLearningResult
from coremind.world.model import JsonValue

log = structlog.get_logger(__name__)


type Clock = Callable[[], datetime]
type ProposalKind = Literal["promote", "deprecate"]


_SUCCESS_OUTCOMES: frozenset[ActionOutcome] = frozenset({"ok", "noop"})
"""Action outcomes counted as successes for promotion accounting.

``"ok"`` and ``"noop"`` indicate the dispatched effector reported a
benign result; every other terminal status (``"transient_failure"``,
``"permanent_failure"``, ``"rejected_invalid_signature"``) is a
failure.  ``"dispatched"`` (no result yet) is excluded from the
denominator entirely so in-flight actions do not bias the stats."""


_RULE_ID_SLUG_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class CandidateKey(BaseModel):
    """Identity of a promotion candidate.

    A candidate is one ``(action_class, operation)`` pair observed
    across the reflection window.  The pair is the coarsest grouping
    that still preserves what the future :class:`Rule` would do (its
    :attr:`Rule.action`).
    """

    model_config = ConfigDict(frozen=True)

    action_class: str = Field(min_length=1)
    operation: str = Field(min_length=1)


class CandidateObservation(BaseModel):
    """One success / failure observation feeding a candidate.

    Attributes:
        key: The candidate this observation belongs to.
        success: ``True`` when the underlying :class:`Action` reported
            a success outcome (see :data:`_SUCCESS_OUTCOMES`).
        observed_at: When the action was dispatched.  Used by the
            ledger to set ``last_evaluated_at`` on the resulting
            :class:`CandidateStats`.
        action_id: The originating action id, captured for traceability
            in proposal rationales.
    """

    model_config = ConfigDict(frozen=True)

    key: CandidateKey
    success: bool
    observed_at: datetime
    action_id: str = Field(min_length=1)


class CandidateStats(BaseModel):
    """Cross-window running stats for a single candidate.

    Attributes:
        key: The candidate this row describes.
        evaluation_count: Total observations folded in across all
            windows.
        success_count: Subset of ``evaluation_count`` that reported a
            success outcome.
        last_evaluated_at: Timestamp of the most recent observation
            folded in.  ``None`` only when ``evaluation_count == 0``.
        proposal_emitted: ``True`` once the learner has emitted a
            promotion proposal for this candidate.  Prevents the same
            promotion from being re-proposed every cycle.
    """

    model_config = ConfigDict(frozen=True)

    key: CandidateKey
    evaluation_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    last_evaluated_at: datetime | None = None
    proposal_emitted: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.success_count > self.evaluation_count:
            msg = (
                f"success_count ({self.success_count}) > evaluation_count ({self.evaluation_count})"
            )
            raise ValueError(msg)
        if self.evaluation_count == 0 and self.last_evaluated_at is not None:
            raise ValueError("last_evaluated_at must be None when evaluation_count == 0")
        if self.evaluation_count > 0 and self.last_evaluated_at is None:
            raise ValueError("last_evaluated_at must be set when evaluation_count > 0")
        if self.last_evaluated_at is not None and self.last_evaluated_at.tzinfo is None:
            raise ValueError("last_evaluated_at must be timezone-aware")
        return self

    @property
    def success_rate(self) -> float:
        """Empirical success rate, or ``0.0`` when no observations exist."""
        if self.evaluation_count == 0:
            return 0.0
        return self.success_count / self.evaluation_count


class RuleProposal(BaseModel):
    """A promotion or deprecation proposal awaiting human approval.

    Attributes:
        id: Stable identifier — ``promote-<slug>`` or
            ``deprecate-<rule_id>`` — making proposal storage
            idempotent across re-evaluated windows.
        kind: ``"promote"`` for a new rule candidate, ``"deprecate"``
            for an existing rule whose performance has degraded.
        description: Human-readable rationale shown in the weekly
            report.
        proposed_rule: For ``"promote"`` proposals, a ready-to-activate
            :class:`Rule` preview (id, trigger, action, source set to
            ``"reflection"``).  ``None`` for ``"deprecate"`` proposals.
        target_rule_id: For ``"deprecate"`` proposals, the id of the
            existing rule to deprecate.  ``None`` for ``"promote"``
            proposals.
        observation_count: Number of observations underpinning the
            proposal.  For promotions this is the candidate's running
            ``evaluation_count``; for deprecations the rule's
            ``applied_count``.
        success_rate: Empirical success rate at proposal time.
        window_start: Start of the reflection window that triggered
            the proposal.
        window_end: End of the reflection window that triggered the
            proposal.
        created_at: When the proposal was emitted.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    kind: ProposalKind
    description: str = Field(min_length=1)
    proposed_rule: Rule | None = None
    target_rule_id: str | None = None
    observation_count: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    window_start: datetime
    window_end: datetime
    created_at: datetime

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.kind == "promote":
            if self.proposed_rule is None:
                raise ValueError("promote proposal requires proposed_rule")
            if self.target_rule_id is not None:
                raise ValueError("promote proposal must not set target_rule_id")
        else:
            if self.target_rule_id is None:
                raise ValueError("deprecate proposal requires target_rule_id")
            if self.proposed_rule is not None:
                raise ValueError("deprecate proposal must not set proposed_rule")
        for ts in (self.window_start, self.window_end, self.created_at):
            if ts.tzinfo is None:
                raise ValueError("timestamps must be timezone-aware")
        if self.window_end < self.window_start:
            raise ValueError("window_end must be >= window_start")
        return self


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RuleLearnerConfig(BaseModel):
    """Thresholds for the rule learner.

    Defaults are conservative — promotion requires at least three
    observations with at least an 80% success rate, and deprecation
    requires at least five evaluations with a success rate below 30%.
    These match the "≥ N times with ≥ M success rate" / "below
    threshold" wording in the phase doc.
    """

    model_config = ConfigDict(frozen=True)

    promotion_min_observations: int = Field(default=3, ge=1)
    promotion_min_success_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    deprecation_min_evaluations: int = Field(default=5, ge=1)
    deprecation_max_success_rate: float = Field(default=0.3, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class RuleSource(Protocol):
    """Read-only view onto active procedural-memory rules.

    Adapters typically wrap :class:`coremind.memory.procedural.ProceduralMemory`,
    but the port keeps the rule learner free of that import path so the
    procedural module remains a single-writer surface.
    """

    async def list_active_rules(self) -> list[Rule]:
        """Return all currently active (non-deprecated) rules."""
        ...


class CandidateLedger(Protocol):
    """Cross-window store of candidate stats.

    Implementations must be idempotent on :class:`CandidateKey`: adding
    the same observation twice (e.g. after a process restart that
    re-runs a window) must not double-count.  The in-memory ledger
    achieves this via the loop's non-overlapping-window invariant
    plus a per-action-id deduplication set.
    """

    async def update(self, observations: Sequence[CandidateObservation]) -> list[CandidateStats]:
        """Fold *observations* into the running stats and return the
        full updated set of stats touched by this batch.

        Returns one :class:`CandidateStats` row per distinct
        :class:`CandidateKey` present in *observations*.
        """
        ...

    async def list_all(self) -> list[CandidateStats]:
        """Return all stats currently held by the ledger."""
        ...

    async def mark_proposed(self, keys: Sequence[CandidateKey]) -> None:
        """Set ``proposal_emitted = True`` on every row matching *keys*."""
        ...


class RuleProposalStore(Protocol):
    """Persists :class:`RuleProposal` rows.

    Implementations must be idempotent on :attr:`RuleProposal.id`:
    re-emitting the same proposal must replace, not duplicate.
    """

    async def store(self, proposals: Sequence[RuleProposal]) -> None:
        """Persist *proposals*, replacing any rows sharing an ``id``."""
        ...

    async def list_pending(self) -> list[RuleProposal]:
        """Return all proposals currently held."""
        ...


# ---------------------------------------------------------------------------
# In-memory implementations
# ---------------------------------------------------------------------------


class InMemoryCandidateLedger:
    """Process-local :class:`CandidateLedger` implementation.

    Suitable for tests and Phase 4 wiring before the SurrealDB-backed
    ledger lands.  Idempotency on the same ``action_id`` is enforced
    explicitly so re-running a window cannot double-count.

    .. warning::
        The ``_seen_actions`` set grows without bound for the lifetime
        of the process.  This is acceptable for tests and short-lived
        bring-up runs, but a long-running daemon must use the
        SurrealDB-backed adapter (follow-up; see module docstring).
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], CandidateStats] = {}
        self._seen_actions: set[str] = set()

    async def update(self, observations: Sequence[CandidateObservation]) -> list[CandidateStats]:
        """Fold *observations* into running stats; return touched rows."""
        touched: dict[tuple[str, str], CandidateStats] = {}
        for obs in observations:
            if obs.action_id in self._seen_actions:
                continue
            self._seen_actions.add(obs.action_id)

            row_key = (obs.key.action_class, obs.key.operation)
            current = self._rows.get(row_key) or CandidateStats(key=obs.key)
            new_count = current.evaluation_count + 1
            new_success = current.success_count + (1 if obs.success else 0)
            new_last = current.last_evaluated_at
            if new_last is None or obs.observed_at > new_last:
                new_last = obs.observed_at
            updated = current.model_copy(
                update={
                    "evaluation_count": new_count,
                    "success_count": new_success,
                    "last_evaluated_at": new_last,
                }
            )
            self._rows[row_key] = updated
            touched[row_key] = updated

        return list(touched.values())

    async def list_all(self) -> list[CandidateStats]:
        """Return every stats row in stable key order."""
        return [self._rows[k] for k in sorted(self._rows)]

    async def mark_proposed(self, keys: Sequence[CandidateKey]) -> None:
        """Mark *keys* as already proposed so they are not re-emitted."""
        for key in keys:
            row_key = (key.action_class, key.operation)
            current = self._rows.get(row_key)
            if current is None:
                continue
            self._rows[row_key] = current.model_copy(update={"proposal_emitted": True})


class InMemoryRuleProposalStore:
    """Process-local :class:`RuleProposalStore` implementation.

    Idempotent on :attr:`RuleProposal.id`.
    """

    def __init__(self) -> None:
        self._rows: dict[str, RuleProposal] = {}

    async def store(self, proposals: Sequence[RuleProposal]) -> None:
        """Replace any rows sharing an ``id`` with the latest proposal."""
        for p in proposals:
            self._rows[p.id] = p

    async def list_pending(self) -> list[RuleProposal]:
        """Return proposals sorted by ``created_at`` ascending."""
        return sorted(self._rows.values(), key=lambda p: p.created_at)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    """Sanitise *value* into a stable rule-id slug.

    Lowercases, collapses any run of non ``[A-Za-z0-9._-]`` characters
    into a single ``-``, and strips leading / trailing separators.
    Used so proposal ids and proposed-rule ids stay deterministic and
    URL-safe regardless of operation naming.
    """
    cleaned = _RULE_ID_SLUG_RE.sub("-", value).strip("-._")
    return cleaned.lower() or "x"


def _classify_outcome(action: Action) -> tuple[bool, bool]:
    """Return ``(counts, success)`` for *action*.

    ``counts`` is ``False`` for actions whose result has not yet been
    settled (``status == "dispatched"`` or no result populated): they
    are excluded from both numerator and denominator so in-flight
    actions never bias the stats.  ``success`` is meaningful only when
    ``counts`` is ``True``.
    """
    if action.result is None or action.result.status == "dispatched":
        return False, False
    return True, action.result.status in _SUCCESS_OUTCOMES


def _candidate_observations(actions: Iterable[Action]) -> list[CandidateObservation]:
    """Convert settled *actions* into per-action observations."""
    out: list[CandidateObservation] = []
    for action in actions:
        counts, success = _classify_outcome(action)
        if not counts:
            continue
        out.append(
            CandidateObservation(
                key=CandidateKey(
                    action_class=action.action_class,
                    operation=action.operation,
                ),
                success=success,
                observed_at=action.timestamp,
                action_id=action.id,
            )
        )
    return out


def _existing_rule_operations(rules: Iterable[Rule]) -> set[str]:
    """Return operations already covered by an active rule.

    A :class:`Rule` stores its proposal as an opaque ``action`` dict;
    we treat the ``"operation"`` key as the canonical identifier so
    duplicate-suppression matches the ActionProposal contract.
    """
    out: set[str] = set()
    for rule in rules:
        op = rule.action.get("operation")
        if isinstance(op, str) and op:
            out.add(op)
    return out


# ---------------------------------------------------------------------------
# Concrete learner
# ---------------------------------------------------------------------------


class RuleLearnerImpl(RuleLearner):
    """Default implementation of the L7 :class:`RuleLearner` port.

    Args:
        rule_source: Read-only view onto active procedural rules.
        ledger: Cross-window candidate-stats store.
        proposal_store: Persistence for emitted proposals.
        config: Threshold configuration.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        rule_source: RuleSource,
        ledger: CandidateLedger,
        proposal_store: RuleProposalStore,
        *,
        config: RuleLearnerConfig | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._rule_source = rule_source
        self._ledger = ledger
        self._store = proposal_store
        self._config = config or RuleLearnerConfig()
        self._clock = clock

    async def learn(
        self,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
    ) -> RuleLearningResult:
        """Run promotion + deprecation analysis and persist proposals.

        ``cycles`` and ``intents`` are accepted by the port for future
        pattern-grounded promotion; the v1 policy keys only on action
        outcomes (see module docstring §"Known v1 simplifications").

        Proposals are persisted *before* candidates are marked as
        proposed.  If :meth:`CandidateLedger.mark_proposed` later
        raises, the next cycle re-runs promotion for the same key —
        but :class:`RuleProposalStore` is idempotent on
        :attr:`RuleProposal.id`, so the user-facing proposal queue is
        unaffected; only an extra log line is emitted.
        """
        _ = cycles, intents  # unused in v1 — see docstring.

        now = self._clock()
        window_start, window_end = _window_bounds(actions, now)

        try:
            observations = _candidate_observations(actions)
            updated_stats = await self._ledger.update(observations)
            all_stats = await self._ledger.list_all()
            existing_rules = await self._rule_source.list_active_rules()
        # Adapter boundary: ledger / rule-source backends can raise
        # implementation-specific errors (DB driver errors, etc.).
        except Exception as exc:
            raise ReflectionError("rule learner failed to load inputs") from exc

        existing_ops = _existing_rule_operations(existing_rules)
        promotions, promoted_keys = self._build_promotions(
            updated_stats=updated_stats,
            all_stats={(s.key.action_class, s.key.operation): s for s in all_stats},
            existing_ops=existing_ops,
            window_start=window_start,
            window_end=window_end,
            created_at=now,
        )
        deprecations = self._build_deprecations(
            rules=existing_rules,
            window_start=window_start,
            window_end=window_end,
            created_at=now,
        )

        proposals = promotions + deprecations
        if proposals:
            try:
                await self._store.store(proposals)
            # Adapter boundary: proposal-store backends raise
            # implementation-specific types; we re-raise with cause.
            except Exception as exc:
                raise ReflectionError("failed to persist rule proposals") from exc
        if promoted_keys:
            try:
                await self._ledger.mark_proposed(promoted_keys)
            # Adapter boundary: ledger backends raise
            # implementation-specific types; we re-raise with cause.
            except Exception as exc:
                raise ReflectionError("failed to mark candidates as proposed") from exc

        log.info(
            "reflection.rules.evaluated",
            actions=len(actions),
            observations=len(observations),
            promotions=len(promotions),
            deprecations=len(deprecations),
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

        return RuleLearningResult(
            proposed_rule_ids=[p.id for p in promotions],
            deprecated_rule_ids=[p.id for p in deprecations],
        )

    # ------------------------------------------------------------------
    # Internal — promotion
    # ------------------------------------------------------------------

    def _build_promotions(
        self,
        *,
        updated_stats: list[CandidateStats],
        all_stats: dict[tuple[str, str], CandidateStats],
        existing_ops: set[str],
        window_start: datetime,
        window_end: datetime,
        created_at: datetime,
    ) -> tuple[list[RuleProposal], list[CandidateKey]]:
        """Emit promotion proposals for candidates that crossed the
        thresholds in this window.

        Only candidates touched by *updated_stats* are eligible — a
        candidate that received no observation this window cannot
        suddenly cross the threshold, and re-checking the entire
        ledger would re-emit proposals for any candidate the user
        rejected (the ledger marks emitted candidates so we never
        re-propose, but the cycle's log noise drops too).
        """
        cfg = self._config
        proposals: list[RuleProposal] = []
        promoted_keys: list[CandidateKey] = []

        for touched in updated_stats:
            row_key = (touched.key.action_class, touched.key.operation)
            current = all_stats.get(row_key, touched)
            if current.proposal_emitted:
                continue
            if current.evaluation_count < cfg.promotion_min_observations:
                continue
            if current.success_rate < cfg.promotion_min_success_rate:
                continue
            if current.key.operation in existing_ops:
                log.info(
                    "reflection.rules.promotion_skipped_existing_operation",
                    action_class=current.key.action_class,
                    operation=current.key.operation,
                )
                continue

            rule = _build_promoted_rule(current, created_at=created_at)
            proposal_id = (
                f"promote-{_slug(current.key.action_class)}-{_slug(current.key.operation)}"
            )
            proposals.append(
                RuleProposal(
                    id=proposal_id,
                    kind="promote",
                    description=(
                        f"Operation {current.key.operation!r} succeeded "
                        f"{current.success_count}/{current.evaluation_count} times "
                        f"(action_class={current.key.action_class!r}); "
                        "propose codifying as a procedural rule."
                    ),
                    proposed_rule=rule,
                    target_rule_id=None,
                    observation_count=current.evaluation_count,
                    success_rate=current.success_rate,
                    window_start=window_start,
                    window_end=window_end,
                    created_at=created_at,
                )
            )
            promoted_keys.append(current.key)

        return proposals, promoted_keys

    # ------------------------------------------------------------------
    # Internal — deprecation
    # ------------------------------------------------------------------

    def _build_deprecations(
        self,
        *,
        rules: Sequence[Rule],
        window_start: datetime,
        window_end: datetime,
        created_at: datetime,
    ) -> list[RuleProposal]:
        """Emit deprecation proposals for under-performing active rules."""
        cfg = self._config
        proposals: list[RuleProposal] = []
        for rule in rules:
            if rule.applied_count < cfg.deprecation_min_evaluations:
                continue
            if rule.success_rate >= cfg.deprecation_max_success_rate:
                continue
            proposals.append(
                RuleProposal(
                    id=f"deprecate-{_slug(rule.id)}",
                    kind="deprecate",
                    description=(
                        f"Rule {rule.id!r} success rate fell to "
                        f"{rule.success_rate:.0%} after {rule.applied_count} "
                        "applications; propose deprecation."
                    ),
                    proposed_rule=None,
                    target_rule_id=rule.id,
                    observation_count=rule.applied_count,
                    success_rate=rule.success_rate,
                    window_start=window_start,
                    window_end=window_end,
                    created_at=created_at,
                )
            )
        return proposals


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _window_bounds(actions: Sequence[Action], fallback: datetime) -> tuple[datetime, datetime]:
    """Return ``(window_start, window_end)`` for the proposal records.

    The reflection loop already knows the canonical window; the rule
    learner is invoked without those bounds today, so we derive the
    range from the action timestamps.  When *actions* is empty the
    learner still emits deprecation proposals based on existing rule
    state, so we degenerate the window to ``[fallback, fallback]``.
    Future revisions of the :class:`RuleLearner` port may pass the
    bounds explicitly; this helper isolates the workaround.
    """
    if not actions:
        return fallback, fallback
    timestamps = [a.timestamp for a in actions]
    return min(timestamps), max(timestamps)


def _build_promoted_rule(stats: CandidateStats, *, created_at: datetime) -> Rule:
    """Construct the :class:`Rule` preview attached to a promotion.

    The synthesised trigger only matches ``action_class`` (see module
    docstring §"Known v1 simplifications"), so the preview is emitted
    with ``confidence=0.0`` and a description that explicitly warns
    the reviewer to refine the precondition before approval.  The
    proposal is *never* activated by this module; activation happens
    only after a human edits and approves the rule.
    """
    rule_id = f"reflect-{_slug(stats.key.action_class)}-{_slug(stats.key.operation)}"
    description = (
        f"Auto-proposed by reflection: {stats.key.operation} "
        f"(class {stats.key.action_class}) succeeded "
        f"{stats.success_count}/{stats.evaluation_count} times. "
        "WARNING: trigger only checks action_class — refine the "
        "precondition before approving."
    )
    trigger: dict[str, JsonValue] = {
        "conditions": [
            {
                "field": "action_class",
                "op": "eq",
                "value": stats.key.action_class,
            }
        ],
        "logic": "all",
    }
    action: dict[str, JsonValue] = {
        "operation": stats.key.operation,
        "action_class": stats.key.action_class,
    }
    return Rule(
        id=rule_id,
        created_at=created_at,
        description=description,
        trigger=trigger,
        action=action,
        # Preview confidence is 0.0 — the empirical success rate lives
        # on the proposal itself; a-priori confidence stays neutral
        # until the user edits the trigger and approves the rule.
        confidence=0.0,
        applied_count=0,
        success_rate=0.0,
        source="reflection",
    )
