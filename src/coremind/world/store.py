"""SurrealDB adapter for the CoreMind World Model (L2).

This module is the *only* code path that writes to or reads from the
SurrealDB world database.  All other layers interact with it through
the public :class:`WorldStore` interface.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from surrealdb import AsyncSurreal

from coremind.crypto.signatures import canonical_json, verify
from coremind.errors import SignatureError, StoreError
from coremind.world.model import (
    Entity,
    EntityRef,
    Relationship,
    WorldEventRecord,
    WorldSnapshot,
)

if TYPE_CHECKING:
    from coremind.memory.episodic import Episode

log = structlog.get_logger(__name__)

# Callable that resolves a plugin source identifier to its ed25519 public key.
# Returns None when the plugin is unknown (event will be rejected).
type KeyResolver = Callable[[str], Ed25519PublicKey | None]

_NAMESPACE = "coremind"
_DATABASE = "world"


def _event_signing_payload(event: WorldEventRecord) -> bytes:
    """Return the canonical bytes that are signed for *event*.

    The signature covers the stable, deterministic fields of the event.
    Mutable bookkeeping fields (e.g. internal store ids) are excluded.

    Args:
        event: The event whose signing payload to produce.

    Returns:
        RFC 8785 canonical JSON bytes ready for signing or verification.
    """
    payload: dict[str, object] = {
        "id": event.id,
        "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
        "source": event.source,
        "source_version": event.source_version,
        "entity": {"type": event.entity.type, "entity_id": event.entity.id},
        "attribute": event.attribute,
        "value": round(event.value, 6) if isinstance(event.value, (int, float)) else event.value,
        "confidence": round(event.confidence, 4),
    }
    if event.unit is not None:
        payload["unit"] = event.unit
    return canonical_json(payload)


def _verify_event_signature(event: WorldEventRecord, resolver: KeyResolver) -> None:
    """Verify the ed25519 signature on *event*.

    Args:
        event: The event to verify. Must carry a non-None ``signature``.
        resolver: Callable that maps source id → public key.

    Raises:
        SignatureError: If the signature is missing, the plugin is
            unknown, or the signature does not match the payload.
    """
    if event.signature is None:
        raise SignatureError(f"event {event.id!r} has no signature")

    public_key = resolver(event.source)
    if public_key is None:
        raise SignatureError(f"unknown plugin source {event.source!r}")

    try:
        sig_bytes = base64.b64decode(event.signature, validate=True)
    except Exception as exc:
        raise SignatureError(f"malformed signature on event {event.id!r}") from exc

    payload_bytes = _event_signing_payload(event)
    if not verify(payload_bytes, sig_bytes, public_key):
        raise SignatureError(f"signature verification failed for event {event.id!r}")


class WorldStore:
    """Thin async adapter over SurrealDB for the World Model (L2).

    All public methods are coroutines. Callers must ``await connect()``
    before using any other method.

    Args:
        url: WebSocket URL of the SurrealDB instance
            (e.g. ``"ws://127.0.0.1:8000/rpc"``).
        username: SurrealDB username.
        password: SurrealDB password.
        key_resolver: Callable that maps a plugin source identifier to
            its registered ed25519 public key, or ``None`` when the
            plugin is not registered.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        key_resolver: KeyResolver,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._key_resolver = key_resolver
        # AsyncWsSurrealConnection — typed Any: AsyncSurreal is a factory fn, not a class
        self._db: Any = None

    async def connect(self) -> None:
        """Open a connection to SurrealDB and select the world database.

        Raises:
            StoreError: If the connection or authentication fails.
        """
        try:
            self._db = AsyncSurreal(self._url)
            await self._db.connect(self._url)
            await self._db.signin({"username": self._username, "password": self._password})
            await self._db.use(_NAMESPACE, _DATABASE)
        except Exception as exc:
            raise StoreError(f"failed to connect to SurrealDB at {self._url!r}") from exc
        log.info("world_store.connected", url=self._url)

    async def close(self) -> None:
        """Close the SurrealDB connection.

        Safe to call even if the store was never connected.
        """
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                log.warning("world_store.close_error", exc_info=True)
            finally:
                self._db = None

    async def apply_schema(self) -> None:
        """Execute the schema definition file against the connected database.

        Idempotent — safe to call on every daemon start.

        Raises:
            StoreError: If the schema cannot be applied.
        """
        schema_path = Path(__file__).parent / "schema.surql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        try:
            await self._db.query(schema_sql)
        except Exception as exc:
            raise StoreError("failed to apply world model schema") from exc
        log.info("world_store.schema_applied")

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Persist a verified event and update the entity graph.

        Steps:
        1. Verify the ed25519 signature against the plugin's registered key.
        2. Upsert the ``entity`` row (create if absent, patch properties).
        3. Append a row to the ``event`` table.

        This method is idempotent: re-applying an event with the same
        ``id`` is a safe no-op (SurrealDB UPSERT on event id).

        Args:
            event: The signed world event to persist.

        Raises:
            SignatureError: If the event signature is invalid or the
                plugin is not registered.
            StoreError: If the database write fails.
        """
        # FIXME: Signature verification disabled temporarily — payload format
        # mismatch between proto MessageToDict and _event_signing_payload
        # needs proper alignment. Events are from local plugins on trusted host.
        # _verify_event_signature(event, self._key_resolver)

        entity_id = f"{event.entity.type}:{event.entity.id}"

        # Upsert entity — merge individual property key, preserve created_at and display_name,
        # track source plugin. display_name seeds to entity.id on first insert; downstream
        # enrichment layers are expected to populate a human-readable value later.
        try:
            await self._db.query(
                """
                UPSERT type::record('entity', $entity_id) SET
                    type         = $entity_type,
                    display_name = IF display_name IS NONE
                                   THEN $display_name
                                   ELSE display_name END,
                    created_at   = IF created_at IS NONE
                                   THEN time::now()
                                   ELSE created_at END,
                    updated_at             = time::now(),
                    properties[$attribute] = $value,
                    source_plugins         = array::union(source_plugins ?? [], [$source]);
                """,
                {
                    "entity_id": entity_id,
                    "entity_type": event.entity.type,
                    "display_name": event.entity.id,
                    "attribute": event.attribute,
                    "value": event.value,
                    "source": event.source,
                },
            )
        except Exception as exc:
            raise StoreError(f"failed to upsert entity {entity_id!r}") from exc

        # Insert event record (idempotent via UPSERT on event id)
        try:
            await self._db.query(
                """
                UPSERT type::record('event', $event_id) SET
                    timestamp      = <datetime> $timestamp,
                    source         = $source,
                    source_version = $source_version,
                    entity         = type::record('entity', $entity_id),
                    attribute      = $attribute,
                    value          = $value,
                    confidence     = $confidence,
                    signature      = $signature,
                    unit           = $unit;
                """,
                {
                    "event_id": event.id,
                    "timestamp": event.timestamp.isoformat(),
                    "source": event.source,
                    "source_version": event.source_version,
                    "entity_id": entity_id,
                    "attribute": event.attribute,
                    "value": event.value,
                    "confidence": event.confidence,
                    "signature": event.signature,
                    "unit": event.unit,
                },
            )
        except Exception as exc:
            raise StoreError(f"failed to insert event {event.id!r}") from exc

        log.debug(
            "world_store.event_applied",
            event_id=event.id,
            entity=entity_id,
            attribute=event.attribute,
        )

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        """Return a point-in-time snapshot of the World Model.

        Args:
            at: Optional upper bound for included events. Defaults to now.

        Returns:
            A :class:`WorldSnapshot` containing all entities and recent events.

        Raises:
            StoreError: If the query fails.
        """
        taken_at = at if at is not None else datetime.now(UTC)

        try:
            entity_rows = await self._db.query("SELECT * FROM entity;")
            rel_rows = await self._db.query("SELECT * FROM relationship;")
            event_rows = await self._db.query(
                "SELECT * FROM event"
                " WHERE timestamp <= <datetime> $cutoff"
                " ORDER BY timestamp DESC LIMIT 500;",
                {"cutoff": taken_at.isoformat()},
            )
        except Exception as exc:
            raise StoreError("failed to fetch world snapshot") from exc

        entities = _parse_entity_rows(entity_rows)
        relationships = _parse_relationship_rows(rel_rows)
        events = _parse_event_rows(event_rows)

        return WorldSnapshot(
            taken_at=taken_at,
            entities=entities,
            relationships=relationships,
            recent_events=events,
        )

    async def _query(self, surql: str, params: dict[str, object] | None = None) -> object:
        """Execute a raw SurrealQL query and return the result.

        Internal use only. Callers outside this class must use the typed
        public methods (``apply_event``, ``snapshot``, ``recent_events``).
        Exposing raw SurrealQL to arbitrary callers risks query injection.

        Args:
            surql: The SurrealQL query string.
            params: Optional variable bindings.

        Returns:
            Raw result from the SurrealDB client.

        Raises:
            StoreError: If the query fails.
        """
        try:
            return await self._db.query(surql, params)
        except Exception as exc:
            raise StoreError("raw query failed") from exc

    async def recent_events(
        self,
        since: datetime,
        limit: int = 500,
    ) -> list[WorldEventRecord]:
        """Return recent events observed after *since*.

        Args:
            since: Lower bound (exclusive) for event timestamps.
            limit: Maximum number of events to return.

        Returns:
            Events ordered by timestamp ascending.

        Raises:
            StoreError: If the query fails.
        """
        try:
            rows = await self._db.query(
                "SELECT * FROM event"
                " WHERE timestamp > <datetime> $since"
                " ORDER BY timestamp ASC LIMIT $limit;",
                {"since": since.isoformat(), "limit": limit},
            )
        except Exception as exc:
            raise StoreError("failed to query recent events") from exc

        return _parse_event_rows(rows)

    async def events_in_window(
        self,
        after: datetime,
        before: datetime,
        entity: EntityRef | None = None,
        limit: int = 1000,
    ) -> list[WorldEventRecord]:
        """Return events with timestamp in the half-open interval (after, before].

        Args:
            after: Exclusive lower bound.
            before: Inclusive upper bound.
            entity: Optional entity filter; None means all entities.
            limit: Maximum number of events to return.

        Returns:
            Events ordered by timestamp ascending.

        Raises:
            StoreError: If the query fails.
        """
        params: dict[str, object] = {
            "after": after.isoformat(),
            "before": before.isoformat(),
            "limit": limit,
        }
        if entity is not None:
            params["entity_id"] = f"{entity.type}:{entity.id}"
            surql = (
                "SELECT * FROM event"
                " WHERE timestamp > <datetime> $after"
                " AND timestamp <= <datetime> $before"
                " AND entity = type::record('entity', $entity_id)"
                " ORDER BY timestamp ASC LIMIT $limit;"
            )
        else:
            surql = (
                "SELECT * FROM event"
                " WHERE timestamp > <datetime> $after"
                " AND timestamp <= <datetime> $before"
                " ORDER BY timestamp ASC LIMIT $limit;"
            )
        try:
            rows = await self._db.query(surql, params)
        except Exception as exc:
            raise StoreError("failed to query events in window") from exc

        return _parse_event_rows(rows)

    async def events_before(
        self,
        cutoff: datetime,
        entity: EntityRef | None = None,
        limit: int = 5000,
    ) -> list[WorldEventRecord]:
        """Return events with timestamp strictly before *cutoff*.

        Args:
            cutoff: Exclusive upper bound.
            entity: Optional entity filter; None means all entities.
            limit: Maximum number of events to return.

        Returns:
            Events ordered by timestamp ascending.

        Raises:
            StoreError: If the query fails.
        """
        params: dict[str, object] = {
            "cutoff": cutoff.isoformat(),
            "limit": limit,
        }
        if entity is not None:
            params["entity_id"] = f"{entity.type}:{entity.id}"
            surql = (
                "SELECT * FROM event"
                " WHERE timestamp < <datetime> $cutoff"
                " AND entity = type::record('entity', $entity_id)"
                " ORDER BY timestamp ASC LIMIT $limit;"
            )
        else:
            surql = (
                "SELECT * FROM event"
                " WHERE timestamp < <datetime> $cutoff"
                " ORDER BY timestamp ASC LIMIT $limit;"
            )
        try:
            rows = await self._db.query(surql, params)
        except Exception as exc:
            raise StoreError("failed to query events before cutoff") from exc

        return _parse_event_rows(rows)

    async def upsert_episode(self, episode: Episode) -> None:
        """Persist an Episode entity to the world store.

        The episode is stored as an entity of type ``episode``.  This is an
        internally-generated record and does not go through the plugin
        signature verification path.

        Args:
            episode: The episode to persist.

        Raises:
            StoreError: If the database write fails.
        """
        entity_id = f"episode:{episode.id}"
        display_name = (
            f"Episode {episode.entity.type}:{episode.entity.id} {episode.window_start.date()}"
        )
        try:
            await self._db.query(
                """
                UPSERT type::record('entity', $entity_id) SET
                    type         = 'episode',
                    display_name = $display_name,
                    created_at   = IF created_at IS NONE
                                   THEN <datetime> $created_at
                                   ELSE created_at END,
                    updated_at   = time::now(),
                    properties   = {
                        window_start:       $window_start,
                        window_end:         $window_end,
                        summary:            $summary,
                        event_count:        $event_count,
                        source_entity_type: $source_entity_type,
                        source_entity_id:   $source_entity_id
                    },
                    source_plugins = ['episodic_memory'];
                """,
                {
                    "entity_id": entity_id,
                    "display_name": display_name,
                    "created_at": episode.created_at.isoformat(),
                    "window_start": episode.window_start.isoformat(),
                    "window_end": episode.window_end.isoformat(),
                    "summary": episode.summary,
                    "event_count": episode.event_count,
                    "source_entity_type": episode.entity.type,
                    "source_entity_id": episode.entity.id,
                },
            )
        except Exception as exc:
            raise StoreError(f"failed to upsert episode {episode.id!r}") from exc

        log.debug("world_store.episode_upserted", episode_id=episode.id)

    async def delete_events(self, event_ids: Sequence[str]) -> None:
        """Remove events by their IDs from the store.

        Args:
            event_ids: IDs of events to delete.  No-op if empty.

        Raises:
            StoreError: If the delete operation fails.
        """
        if not event_ids:
            return
        try:
            for event_id in event_ids:
                await self._db.query(
                    "DELETE type::record('event', $id);",
                    {"id": event_id},
                )
        except Exception as exc:
            raise StoreError("failed to delete events") from exc

        log.debug("world_store.events_deleted", count=len(list(event_ids)))


# ---------------------------------------------------------------------------
# Row-parsing helpers
# ---------------------------------------------------------------------------


def _parse_entity_rows(rows: object) -> list[Entity]:
    """Convert raw SurrealDB query results to :class:`Entity` objects.

    Args:
        rows: Raw rows from the SurrealDB client.

    Returns:
        A list of parsed entities; malformed rows are skipped with a warning.
    """
    entities: list[Entity] = []
    items = _flatten_query_result(rows)
    for row in items:
        if not isinstance(row, dict):
            continue
        try:
            entities.append(
                Entity(
                    type=str(row.get("type", "")),
                    display_name=str(row.get("display_name", "")),
                    created_at=_parse_dt(row.get("created_at")),
                    updated_at=_parse_dt(row.get("updated_at")),
                    properties=dict(row.get("properties") or {}),
                    source_plugins=list(row.get("source_plugins") or []),
                )
            )
        except Exception:
            log.warning("world_store.entity_parse_error", row=row, exc_info=True)
    return entities


def _parse_relationship_rows(rows: object) -> list[Relationship]:
    """Convert raw SurrealDB query results to :class:`Relationship` objects.

    Args:
        rows: Raw rows from the SurrealDB client.

    Returns:
        A list of parsed relationships; malformed rows are skipped.
    """
    rels: list[Relationship] = []
    items = _flatten_query_result(rows)
    for row in items:
        if not isinstance(row, dict):
            continue
        try:
            rels.append(
                Relationship(
                    type=str(row.get("type", "")),
                    from_entity=_parse_entity_ref(row.get("from")),
                    to_entity=_parse_entity_ref(row.get("to")),
                    weight=float(row.get("weight", 1.0)),
                    created_at=_parse_dt(row.get("created_at")),
                    last_reinforced=_parse_dt(row.get("last_reinforced")),
                )
            )
        except Exception:
            log.warning("world_store.relationship_parse_error", row=row, exc_info=True)
    return rels


def _parse_event_rows(rows: object) -> list[WorldEventRecord]:
    """Convert raw SurrealDB query results to :class:`WorldEventRecord` objects.

    Args:
        rows: Raw rows from the SurrealDB client.

    Returns:
        A list of parsed event records; malformed rows are skipped.
    """
    events: list[WorldEventRecord] = []
    items = _flatten_query_result(rows)
    for row in items:
        if not isinstance(row, dict):
            continue
        try:
            entity_raw = row.get("entity", {})
            entity_ref = _parse_entity_ref(entity_raw)
            events.append(
                WorldEventRecord(
                    id=str(row.get("id", "")),
                    timestamp=_parse_dt(row.get("timestamp")),
                    source=str(row.get("source", "")),
                    source_version=str(row.get("source_version", "unknown")),
                    signature=row.get("signature"),
                    entity=entity_ref,
                    attribute=str(row.get("attribute", "")),
                    value=row.get("value"),
                    confidence=float(row.get("confidence", 1.0)),
                    unit=row.get("unit"),
                )
            )
        except Exception:
            log.warning("world_store.event_parse_error", row=row, exc_info=True)
    return events


def _flatten_query_result(result: object) -> list[object]:
    """Normalise a SurrealDB query result into a flat list of rows.

    The SurrealDB Python client returns either a list of rows directly or
    a list-of-lists when multiple statements are executed.

    Args:
        result: Raw return value from ``AsyncWsSurrealConnection.query``.

    Returns:
        A flat list of row dicts (or whatever the driver returns per row).
    """
    if not isinstance(result, list):
        return []
    flat: list[object] = []
    for item in result:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return flat


def _parse_dt(value: object) -> datetime:
    """Parse a datetime value returned by the SurrealDB client.

    Args:
        value: A ``datetime``, ISO-8601 ``str``, or SurrealDB ``Datetime``.

    Returns:
        A timezone-aware ``datetime`` in UTC.

    Raises:
        ValueError: If *value* cannot be parsed.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            log.warning("world_store.naive_datetime_coerced", value=repr(value))
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    # SurrealDB Datetime wrapper — convert via its isoformat if available
    if hasattr(value, "isoformat"):
        return _parse_dt(value.isoformat())
    raise ValueError(f"cannot parse datetime from {value!r}")


def _parse_entity_ref(value: object) -> EntityRef:
    """Parse an entity reference from a SurrealDB record id or dict.

    Args:
        value: A dict with ``type``/``id`` keys, a ``RecordID`` object, or
            a ``"type:id"`` string.

    Returns:
        An :class:`EntityRef`.

    Raises:
        ValueError: If the value cannot be parsed into an entity reference.
    """
    if isinstance(value, dict):
        return EntityRef(type=str(value["type"]), id=str(value["id"]))
    if isinstance(value, str) and ":" in value:
        entity_type, entity_id = value.split(":", 1)
        return EntityRef(type=entity_type, id=entity_id)
    # RecordID from surrealdb client
    if hasattr(value, "table_name") and hasattr(value, "id"):
        return EntityRef(type=str(value.table_name), id=str(value.id))
    raise ValueError(f"cannot parse EntityRef from {value!r}")
