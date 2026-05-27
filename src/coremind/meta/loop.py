"""Meta-loop orchestrator for the meta-cognition layer (L8).

Runs periodically as an asyncio task: observe → evaluate → validate → adjust.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from datetime import UTC, datetime
from typing import Protocol

import structlog

from coremind.meta.adjuster import MetaAdjuster
from coremind.meta.protocols import ApprovalQueueProtocol, MetaStoreProtocol
from coremind.meta.schemas import (
    MetaConfig,
    MetaObservation,
    ProposedAdjustment,
    ValidationResult,
)


class _ObserverProtocol(Protocol):
    async def observe_all(self) -> list[MetaObservation]: ...


class _EvaluatorProtocol(Protocol):
    def evaluate(self, observations: list[MetaObservation]) -> list[ProposedAdjustment]: ...


class _ValidatorProtocol(Protocol):
    def validate(self, proposal: ProposedAdjustment) -> ValidationResult: ...


log = structlog.get_logger(__name__)

_RATE_LIMIT_WINDOW_SECONDS = 3600


class MetaLoop:
    """Orchestrates the meta-loop. Runs periodically as an asyncio task."""

    def __init__(
        self,
        observer: _ObserverProtocol,
        evaluator: _EvaluatorProtocol,
        validator: _ValidatorProtocol,
        adjuster: MetaAdjuster,
        meta_store: MetaStoreProtocol,
        approval_queue: ApprovalQueueProtocol,
        config: MetaConfig,
    ) -> None:
        self._observer = observer
        self._evaluator = evaluator
        self._validator = validator
        self._adjuster = adjuster
        self._meta_store = meta_store
        self._approval_queue = approval_queue
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Track adjustment timestamps for rate limiting
        self._recent_adjustments: deque[datetime] = deque()

    async def start(self) -> None:
        """Start the periodic loop. No-op if config.enabled is False."""
        if not self._config.enabled:
            log.info("meta.loop_disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="coremind.meta")
        log.info(
            "meta.loop_started",
            interval_seconds=self._config.observation_interval_seconds,
        )

    async def stop(self) -> None:
        """Cancel the running task."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        log.info("meta.loop_stopped")

    async def tick(self) -> None:
        """Run one loop iteration. Exposed for testing."""
        observations = await self._observer.observe_all()

        if self._config.log_observations and observations:
            await self._meta_store.save_observations(observations)

        proposals = self._evaluator.evaluate(observations)

        for proposal in proposals:
            result = self._validator.validate(proposal)
            if not result.valid:
                log.warning(
                    "meta.proposal_rejected",
                    policy=proposal.policy.name,
                    path=proposal.parameter_path,
                    reason=result.reason,
                )
                continue

            if proposal.policy.requires_user_approval:
                await self._approval_queue.add(proposal)
                log.info(
                    "meta.proposal_queued_for_approval",
                    policy=proposal.policy.name,
                    path=proposal.parameter_path,
                )
                continue

            if not self._within_rate_limit():
                log.warning(
                    "meta.rate_limit_exceeded",
                    policy=proposal.policy.name,
                    max_per_hour=self._config.max_adjustments_per_hour,
                )
                continue

            await self._adjuster.apply(proposal)
            self._recent_adjustments.append(datetime.now(UTC))

    def _within_rate_limit(self) -> bool:
        """Check whether we can apply another adjustment this hour."""
        now = datetime.now(UTC)
        # Purge entries older than 1 hour
        while (
            self._recent_adjustments
            and (now - self._recent_adjustments[0]).total_seconds() > _RATE_LIMIT_WINDOW_SECONDS
        ):
            self._recent_adjustments.popleft()
        return len(self._recent_adjustments) < self._config.max_adjustments_per_hour

    async def _run_forever(self) -> None:
        """Run cycles until stop() is called."""
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("meta.tick_failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.observation_interval_seconds,
                )
            except TimeoutError:
                continue
