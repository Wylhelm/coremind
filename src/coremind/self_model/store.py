"""SurrealDB adapter for the Self-Model.

This module is the only code path that writes to or reads from the
self-model tables in SurrealDB.  All other self-model components interact
through the public :class:`SelfModelStore` interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from coremind.self_model.entities import (
    JsonValue,
    SelfFact,
    SelfModelEntityType,
)
from coremind.self_model.errors import SelfModelStoreError

log = structlog.get_logger(__name__)

_DEACTIVATION_THRESHOLD: float = 0.3


class SelfModelStore:
    """SurrealDB persistence adapter for self-model facts.

    Manages CRUD operations on ``self_fact`` records with support for
    versioning (supersession), deduplication, and confidence-based queries.
    """

    def __init__(self, db: Any) -> None:
        """Initialize the store with a connected SurrealDB client.

        Args:
            db: An already-connected AsyncSurreal instance.
        """
        self._db = db

    async def initialize(self) -> None:
        """Create self-model tables and indexes if they don't exist.

        Idempotent — safe to call on every daemon startup.
        """
        try:
            await self._db.query(
                """
                DEFINE TABLE IF NOT EXISTS self_fact SCHEMAFULL;
                DEFINE FIELD IF NOT EXISTS id ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS entity_type ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS entity_id ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS attribute ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS value ON self_fact TYPE any;
                DEFINE FIELD IF NOT EXISTS confidence ON self_fact TYPE float;
                DEFINE FIELD IF NOT EXISTS method ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS source ON self_fact TYPE string;
                DEFINE FIELD IF NOT EXISTS evidence ON self_fact TYPE array;
                DEFINE FIELD IF NOT EXISTS created_at ON self_fact TYPE datetime;
                DEFINE FIELD IF NOT EXISTS updated_at ON self_fact TYPE datetime;
                DEFINE FIELD IF NOT EXISTS superseded_by ON self_fact TYPE option<string>;
                DEFINE FIELD IF NOT EXISTS active ON self_fact TYPE bool;
                DEFINE INDEX IF NOT EXISTS idx_self_fact_entity
                    ON self_fact FIELDS entity_type, entity_id;
                DEFINE INDEX IF NOT EXISTS idx_self_fact_active
                    ON self_fact FIELDS active, entity_type;
                DEFINE INDEX IF NOT EXISTS idx_self_fact_lookup
                    ON self_fact FIELDS entity_type, entity_id, attribute, active;
                """
            )
            log.info("self_model.store_initialized")
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to initialize self-model schema: {exc}") from exc

    async def upsert_fact(self, fact: SelfFact) -> SelfFact:
        """Insert a new fact or supersede an existing one.

        If an active fact with the same (entity_type, entity_id, attribute)
        already exists:
        - If new confidence >= existing, supersede the old fact.
        - If new confidence < existing, skip (return existing).

        Args:
            fact: The fact to persist.

        Returns:
            The persisted fact (may be the existing one if skipped).

        Raises:
            SelfModelStoreError: On database failure.
        """
        try:
            existing = await self._find_active_fact(
                fact.entity_type, fact.entity_id, fact.attribute
            )

            if existing is not None:
                if fact.confidence < existing.confidence:
                    log.debug(
                        "self_model.fact_skipped_lower_confidence",
                        entity=f"{fact.entity_type}:{fact.entity_id}",
                        attribute=fact.attribute,
                        new_confidence=fact.confidence,
                        existing_confidence=existing.confidence,
                    )
                    return existing

                # Supersede the old fact
                await self._db.query(
                    "UPDATE self_fact SET superseded_by = $new_id, active = false, "
                    "updated_at = $now WHERE id = $old_id",
                    params={
                        "new_id": fact.id,
                        "old_id": existing.id,
                        "now": datetime.now(UTC).isoformat(),
                    },
                )

            # Insert the new fact
            await self._db.query(
                "CREATE self_fact CONTENT $fact",
                params={"fact": fact.model_dump(mode="json")},
            )
            log.info(
                "self_model.fact_upserted",
                entity=f"{fact.entity_type}:{fact.entity_id}",
                attribute=fact.attribute,
                confidence=fact.confidence,
                method=fact.method,
            )
            return fact

        except SelfModelStoreError:
            raise
        except Exception as exc:
            raise SelfModelStoreError(
                f"Failed to upsert fact {fact.entity_type}:{fact.entity_id}.{fact.attribute}: {exc}"
            ) from exc

    async def get_fact(self, fact_id: str) -> SelfFact | None:
        """Retrieve a single fact by ID.

        Args:
            fact_id: The ULID of the fact to retrieve.

        Returns:
            The fact if found, None otherwise.
        """
        try:
            result = await self._db.query(
                "SELECT * FROM self_fact WHERE id = $id LIMIT 1",
                params={"id": fact_id},
            )
            rows = _extract_rows(result)
            if not rows:
                return None
            return SelfFact.model_validate(rows[0])
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to get fact {fact_id}: {exc}") from exc

    async def list_facts(
        self,
        *,
        entity_type: SelfModelEntityType | None = None,
        entity_id: str | None = None,
        active_only: bool = True,
        min_confidence: float = 0.0,
        limit: int = 100,
    ) -> Sequence[SelfFact]:
        """Query facts with optional filters.

        Args:
            entity_type: Filter by entity type.
            entity_id: Filter by specific entity ID (requires entity_type).
            active_only: Only return active (non-superseded) facts.
            min_confidence: Minimum confidence threshold.
            limit: Maximum number of results.

        Returns:
            Matching facts ordered by updated_at descending.
        """
        conditions: list[str] = []
        params: dict[str, object] = {"limit": limit, "min_conf": min_confidence}

        if active_only:
            conditions.append("active = true")
        if entity_type is not None:
            conditions.append("entity_type = $entity_type")
            params["entity_type"] = entity_type
        if entity_id is not None:
            conditions.append("entity_id = $entity_id")
            params["entity_id"] = entity_id
        conditions.append("confidence >= $min_conf")

        where_clause = " AND ".join(conditions) if conditions else "true"
        query = (
            f"SELECT * FROM self_fact WHERE {where_clause} "  # noqa: S608 — SurrealQL parameterized
            "ORDER BY updated_at DESC LIMIT $limit"
        )

        try:
            result = await self._db.query(query, params=params)
            rows = _extract_rows(result)
            return [SelfFact.model_validate(row) for row in rows]
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to list facts: {exc}") from exc

    async def deactivate_fact(self, fact_id: str, *, reason: str = "") -> None:
        """Mark a fact as inactive (soft-delete).

        Args:
            fact_id: The ULID of the fact to deactivate.
            reason: Optional reason for deactivation (logged).
        """
        try:
            await self._db.query(
                "UPDATE self_fact SET active = false, updated_at = $now WHERE id = $id",
                params={"id": fact_id, "now": datetime.now(UTC).isoformat()},
            )
            log.info("self_model.fact_deactivated", fact_id=fact_id, reason=reason)
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to deactivate fact {fact_id}: {exc}") from exc

    async def apply_confidence_decay(
        self, decay_per_week: float, stale_threshold_days: int = 7
    ) -> int:
        """Decay confidence on observed facts not refreshed recently.

        Facts whose ``updated_at`` is older than ``stale_threshold_days``
        have their confidence reduced by ``decay_per_week``.  Facts that
        drop below 0.3 are deactivated.

        Args:
            decay_per_week: Amount to subtract from confidence.
            stale_threshold_days: Days of inactivity before decay applies.

        Returns:
            Number of facts that were decayed or deactivated.
        """
        try:
            cutoff = datetime.now(UTC).isoformat()
            # Select stale observed/synthesized facts
            result = await self._db.query(
                "SELECT * FROM self_fact WHERE active = true "
                "AND method IN ['observed', 'synthesized'] "
                "AND updated_at < time::sub($now, $threshold) ",
                params={
                    "now": cutoff,
                    "threshold": f"{stale_threshold_days}d",
                },
            )
            rows = _extract_rows(result)
            count = 0
            for row in rows:
                fact = SelfFact.model_validate(row)
                new_confidence = fact.confidence - decay_per_week
                if new_confidence < _DEACTIVATION_THRESHOLD:
                    await self.deactivate_fact(fact.id, reason="confidence_decayed_below_threshold")
                else:
                    await self._db.query(
                        "UPDATE self_fact SET confidence = $conf, updated_at = $now WHERE id = $id",
                        params={
                            "id": fact.id,
                            "conf": round(new_confidence, 4),
                            "now": datetime.now(UTC).isoformat(),
                        },
                    )
                count += 1

            if count > 0:
                log.info("self_model.confidence_decay_applied", affected=count)
            return count

        except SelfModelStoreError:
            raise
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to apply confidence decay: {exc}") from exc

    async def entity_summary(
        self, entity_type: SelfModelEntityType, entity_id: str
    ) -> dict[str, JsonValue]:
        """Build a key-value summary of all active facts for an entity.

        Args:
            entity_type: The entity type.
            entity_id: The entity ID.

        Returns:
            Dictionary mapping attribute names to their current values.
        """
        facts = await self.list_facts(
            entity_type=entity_type, entity_id=entity_id, active_only=True
        )
        return {fact.attribute: fact.value for fact in facts}

    async def count_by_type(self) -> dict[SelfModelEntityType, int]:
        """Count active facts grouped by entity type.

        Returns:
            Dictionary mapping entity types to their active fact count.
        """
        try:
            result = await self._db.query(
                "SELECT entity_type, count() AS cnt FROM self_fact "
                "WHERE active = true GROUP BY entity_type"
            )
            rows = _extract_rows(result)
            counts: dict[SelfModelEntityType, int] = {}
            for row in rows:
                et = row["entity_type"]
                cnt = row["cnt"]
                if isinstance(et, str) and isinstance(cnt, int):  # pragma: no branch
                    counts[cast(SelfModelEntityType, et)] = cnt
            return counts
        except Exception as exc:
            raise SelfModelStoreError(f"Failed to count by type: {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _find_active_fact(
        self,
        entity_type: SelfModelEntityType,
        entity_id: str,
        attribute: str,
    ) -> SelfFact | None:
        """Find the currently active fact for a specific entity+attribute."""
        result = await self._db.query(
            "SELECT * FROM self_fact "
            "WHERE entity_type = $et AND entity_id = $eid "
            "AND attribute = $attr AND active = true LIMIT 1",
            params={"et": entity_type, "eid": entity_id, "attr": attribute},
        )
        rows = _extract_rows(result)
        if not rows:
            return None
        return SelfFact.model_validate(rows[0])


def _extract_rows(result: object) -> list[dict[str, object]]:
    """Extract row dicts from SurrealDB query results.

    SurrealDB returns results in various wrapper formats depending on
    the driver version.  This normalizes them to a flat list of dicts.
    """
    if isinstance(result, list):
        # Driver may return [{"result": [...], "status": "OK"}] or [[...]]
        if result and isinstance(result[0], dict) and "result" in result[0]:
            inner = result[0]["result"]
            return inner if isinstance(inner, list) else []
        if result and isinstance(result[0], list):
            return result[0]
        # Direct list of row dicts — driver returns untyped list[dict]
        if result and isinstance(result[0], dict):
            return [dict(r) for r in result]
    return []
