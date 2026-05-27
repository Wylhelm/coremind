"""Concrete MetaSource adapter wiring the live meta-loop to the dashboard.

Instantiated by the daemon at startup and passed into
:class:`~coremind.dashboard.data.DashboardDataSources`.
"""

from __future__ import annotations

from datetime import datetime

from coremind.meta.adjuster import MetaAdjuster
from coremind.meta.schemas import (
    AdjustmentRecord,
    MetaConfig,
    MetaObservation,
    MetaStatus,
    ProposedAdjustment,
)
from coremind.meta.stores import InMemoryApprovalQueue, InMemoryMetaStore


class DaemonMetaSource:
    """Adapts live meta-loop stores to the dashboard MetaSource protocol."""

    def __init__(
        self,
        *,
        meta_config: MetaConfig,
        meta_store: InMemoryMetaStore,
        approval_queue: InMemoryApprovalQueue,
        adjuster: MetaAdjuster,
    ) -> None:
        self._config = meta_config
        self._store = meta_store
        self._queue = approval_queue
        self._adjuster = adjuster

    async def get_status(self) -> MetaStatus:
        """Return current meta-loop status summary."""
        observations = self._store._observations
        adjustments = self._store._adjustments
        proposals = self._queue.pending

        return MetaStatus(
            enabled=self._config.enabled,
            last_tick=None,  # Not tracked; could be enhanced later
            observations_count=len(observations),
            adjustments_count=len(adjustments),
            pending_proposals_count=len(proposals),
        )

    async def list_observations(
        self,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MetaObservation]:
        """Return observations filtered by kind and time window."""
        obs = list(self._store._observations)
        if kind:
            obs = [o for o in obs if o.kind == kind]
        if since:
            obs = [o for o in obs if o.observed_at >= since]
        # Newest first
        obs.sort(key=lambda o: o.observed_at, reverse=True)
        return obs[:limit]

    async def list_adjustments(
        self,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AdjustmentRecord]:
        """Return adjustment records, newest-first."""
        records = list(self._store._adjustments.values())
        if since:
            records = [r for r in records if r.applied_at >= since]
        records.sort(key=lambda r: r.applied_at, reverse=True)
        return records[:limit]

    async def list_proposals(self) -> list[ProposedAdjustment]:
        """Return pending proposals awaiting user approval."""
        return self._queue.pending

    async def approve_proposal(self, proposal_id: str) -> None:
        """Approve and apply a pending proposal by index.

        Args:
            proposal_id: String index into the proposal queue.

        Raises:
            ValueError: If no proposal exists at the given index.
        """
        try:
            idx = int(proposal_id)
        except ValueError:
            msg = f"Invalid proposal_id: '{proposal_id}'"
            raise ValueError(msg) from None

        pending = self._queue.pending
        if idx < 0 or idx >= len(pending):
            msg = f"No proposal at index {idx}"
            raise ValueError(msg)

        proposal = pending[idx]
        await self._adjuster.apply(proposal)
        # Remove from queue
        self._queue._queue.pop(idx)

    async def deny_proposal(self, proposal_id: str) -> None:
        """Deny and remove a pending proposal.

        Args:
            proposal_id: String index into the proposal queue.

        Raises:
            ValueError: If no proposal exists at the given index.
        """
        try:
            idx = int(proposal_id)
        except ValueError:
            msg = f"Invalid proposal_id: '{proposal_id}'"
            raise ValueError(msg) from None

        pending = self._queue.pending
        if idx < 0 or idx >= len(pending):
            msg = f"No proposal at index {idx}"
            raise ValueError(msg)

        self._queue._queue.pop(idx)

    async def rollback_adjustment(self, adjustment_id: str) -> None:
        """Rollback a previously applied adjustment.

        Args:
            adjustment_id: The adjustment ID to roll back.

        Raises:
            ValueError: If no adjustment with that ID exists.
        """
        await self._adjuster.rollback(adjustment_id)
