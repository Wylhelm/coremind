"""Meta-adjuster for the meta-cognition layer (L8).

Applies validated adjustments to configuration, persists records, and
publishes events. All methods are async (side-effecting).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from coremind.meta.protocols import (
    ConfigStoreProtocol,
    MetaEventBusProtocol,
    MetaStoreProtocol,
)
from coremind.meta.schemas import AdjustmentRecord, ProposedAdjustment

log = structlog.get_logger(__name__)


class MetaAdjuster:
    """Applies adjustments and propagates them to the running system."""

    def __init__(
        self,
        config_store: ConfigStoreProtocol,
        meta_store: MetaStoreProtocol,
        event_bus: MetaEventBusProtocol,
    ) -> None:
        self._config_store = config_store
        self._meta_store = meta_store
        self._event_bus = event_bus

    async def apply(self, proposal: ProposedAdjustment) -> AdjustmentRecord:
        """Apply an adjustment: persist record, update config, publish event.

        Args:
            proposal: The validated proposal to apply.

        Returns:
            The persisted adjustment record.
        """
        record = AdjustmentRecord(
            policy_name=proposal.policy.name,
            parameter_path=proposal.parameter_path,
            old_value=proposal.old_value,
            new_value=proposal.new_value,
            reason=(
                f"Policy '{proposal.policy.name}' triggered by observation "
                f"{proposal.observation.observation_id} "
                f"({proposal.observation.kind}={proposal.observation.value})"
            ),
            triggered_by_observation_id=proposal.observation.observation_id,
        )

        await self._meta_store.save_adjustment(record)
        await self._config_store.set(proposal.parameter_path, proposal.new_value)
        await self._event_bus.publish(
            "meta.adjustment.applied",
            {
                "adjustment_id": record.adjustment_id,
                "policy_name": record.policy_name,
                "parameter_path": record.parameter_path,
                "old_value": record.old_value,
                "new_value": record.new_value,
            },
        )

        log.info(
            "meta.adjustment_applied",
            adjustment_id=record.adjustment_id,
            policy=record.policy_name,
            path=record.parameter_path,
            old=record.old_value,
            new=record.new_value,
        )

        return record

    async def rollback(self, adjustment_id: str) -> None:
        """Revert an adjustment by restoring its old_value.

        Args:
            adjustment_id: The ID of the adjustment to roll back.

        Raises:
            ValueError: If no adjustment with *adjustment_id* exists.
        """
        record = await self._meta_store.get_adjustment(adjustment_id)
        if record is None:
            msg = f"No adjustment found with id '{adjustment_id}'"
            raise ValueError(msg)

        await self._config_store.set(record.parameter_path, record.old_value)

        updated = record.model_copy(update={"rollback_at": datetime.now(UTC)})
        await self._meta_store.update_adjustment(updated)

        await self._event_bus.publish(
            "meta.adjustment.rolled_back",
            {
                "adjustment_id": record.adjustment_id,
                "parameter_path": record.parameter_path,
                "restored_value": record.old_value,
            },
        )

        log.info(
            "meta.adjustment_rolled_back",
            adjustment_id=record.adjustment_id,
            path=record.parameter_path,
            restored=record.old_value,
        )
