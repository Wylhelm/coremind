"""Persistence adapter for autonomy slider change audit trail.

Records every slider change to the ``autonomy_change`` table in SurrealDB,
providing a full history of how trust levels evolved over time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)


class AutonomyChangeRecord(BaseModel):
    """A single slider change event persisted to the audit table."""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(min_length=1)
    old_slider: float = Field(ge=0.0, le=1.0)
    new_slider: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    changed_by: str = Field(min_length=1)
    changed_at: datetime


class AutonomyAuditPort(Protocol):
    """Port for recording and querying autonomy slider changes."""

    async def record_change(self, record: AutonomyChangeRecord) -> None:
        """Persist a slider change to the audit trail."""
        ...

    async def get_history(
        self,
        domain: str | None = None,
        *,
        limit: int = 50,
    ) -> list[AutonomyChangeRecord]:
        """Retrieve slider change history, optionally filtered by domain."""
        ...


class SurrealAutonomyAuditStore:
    """SurrealDB-backed implementation of :class:`AutonomyAuditPort`.

    Args:
        db: Connected SurrealDB async client.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def record_change(self, record: AutonomyChangeRecord) -> None:
        """Persist a slider change to the autonomy_change table."""
        await self._db.query(
            "CREATE autonomy_change SET "
            "domain = $domain, "
            "old_slider = $old_slider, "
            "new_slider = $new_slider, "
            "reason = $reason, "
            "changed_by = $changed_by, "
            "changed_at = $changed_at",
            {
                "domain": record.domain,
                "old_slider": record.old_slider,
                "new_slider": record.new_slider,
                "reason": record.reason,
                "changed_by": record.changed_by,
                "changed_at": record.changed_at.isoformat(),
            },
        )
        log.info(
            "autonomy_change_recorded",
            domain=record.domain,
            old=record.old_slider,
            new=record.new_slider,
            reason=record.reason,
        )

    async def get_history(
        self,
        domain: str | None = None,
        *,
        limit: int = 50,
    ) -> list[AutonomyChangeRecord]:
        """Retrieve slider change history from SurrealDB."""
        if domain is not None:
            result = await self._db.query(
                "SELECT * FROM autonomy_change "
                "WHERE domain = $domain "
                "ORDER BY changed_at DESC LIMIT $limit",
                {"domain": domain, "limit": limit},
            )
        else:
            result = await self._db.query(
                "SELECT * FROM autonomy_change ORDER BY changed_at DESC LIMIT $limit",
                {"limit": limit},
            )

        rows: list[dict[str, Any]] = result[0] if result else []
        records: list[AutonomyChangeRecord] = []
        for row in rows:
            records.append(
                AutonomyChangeRecord(
                    domain=row["domain"],
                    old_slider=row["old_slider"],
                    new_slider=row["new_slider"],
                    reason=row["reason"],
                    changed_by=row["changed_by"],
                    changed_at=datetime.fromisoformat(row["changed_at"]).replace(
                        tzinfo=UTC,
                    ),
                )
            )
        return records
