"""SurrealDB adapters for the L7 reflection layer.

Hosts the only code path that writes to or reads from the reflection
SurrealDB tables (``prediction_evaluation``, ``calibration_diagram``).
Mirrors the structure of :mod:`coremind.world.store` so the project's
"no DB writes outside ``store.py`` adapters" and "one writer per stream"
rules continue to hold for L7 storage.

A single :class:`SurrealReflectionStore` owns the connection and the
schema, and exposes two thin views:

* :meth:`SurrealReflectionStore.predictions` —
  :class:`coremind.reflection.evaluator.PredictionEvaluationStore`
  implementation written by Task 4.2.
* :meth:`SurrealReflectionStore.calibration` —
  :class:`coremind.reflection.calibration.CalibrationStore`
  implementation written by Task 4.3.

Both views are idempotent on their natural key (``(cycle_id,
prediction_id)`` and ``(layer, model)`` respectively) so re-running the
same reflection window cannot duplicate rows.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from surrealdb import AsyncSurreal

from coremind.errors import StoreError
from coremind.reflection.calibration import (
    BUCKET_COUNT,
    CalibrationBucket,
    ReliabilityDiagram,
)
from coremind.reflection.evaluator import PredictionEvaluation, Verdict

log = structlog.get_logger(__name__)


type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


_NAMESPACE = "coremind"
_DATABASE = "reflection"


# ---------------------------------------------------------------------------
# SurrealReflectionStore
# ---------------------------------------------------------------------------


class SurrealReflectionStore:
    """SurrealDB adapter that backs the L7 reflection storage.

    All public methods are coroutines.  Callers must ``await connect()``
    before using any other method.

    Args:
        url: WebSocket URL of the SurrealDB instance
            (e.g. ``"ws://127.0.0.1:8000/rpc"``).
        username: SurrealDB username.
        password: SurrealDB password.
        namespace: SurrealDB namespace.  Defaults to ``"coremind"``.
        database: SurrealDB database.  Defaults to ``"reflection"`` so
            reflection storage lives in its own database, distinct from
            the world model's ``"world"`` database.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        namespace: str = _NAMESPACE,
        database: str = _DATABASE,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._namespace = namespace
        self._database = database
        # AsyncSurreal is a factory function returning a connection-typed Any.
        self._db: Any = None

    async def connect(self) -> None:
        """Open a connection to SurrealDB and select the reflection database.

        Raises:
            StoreError: If the connection or authentication fails.
        """
        try:
            self._db = AsyncSurreal(self._url)
            await self._db.connect(self._url)
            await self._db.signin({"username": self._username, "password": self._password})
            await self._db.use(self._namespace, self._database)
        except Exception as exc:
            raise StoreError(f"failed to connect to SurrealDB at {self._url!r}") from exc
        log.info(
            "reflection_store.connected",
            url=self._url,
            namespace=self._namespace,
            database=self._database,
        )

    async def close(self) -> None:
        """Close the SurrealDB connection.  Safe to call when never connected."""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                log.warning("reflection_store.close_error", exc_info=True)
            finally:
                self._db = None

    async def apply_schema(self) -> None:
        """Execute ``schema.surql`` against the connected database.

        Idempotent — safe to call on every daemon start.

        Raises:
            StoreError: If the schema cannot be applied or the store is
                not connected.
        """
        if self._db is None:
            raise StoreError("reflection store is not connected")
        schema_path = Path(__file__).parent / "schema.surql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        try:
            await self._db.query(schema_sql)
        except Exception as exc:
            raise StoreError("failed to apply reflection schema") from exc
        log.info("reflection_store.schema_applied")

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def predictions(self) -> SurrealPredictionEvaluationStore:
        """Return the prediction-evaluation view bound to this connection."""
        return SurrealPredictionEvaluationStore(self)

    def calibration(self, *, clock: Clock = _utc_now) -> SurrealCalibrationStore:
        """Return the calibration view bound to this connection.

        Args:
            clock: Injectable clock used to stamp ``updated_at`` on
                diagram rows; defaults to :func:`datetime.now(UTC)`.
        """
        return SurrealCalibrationStore(self, clock=clock)

    # ------------------------------------------------------------------
    # Internal — connection access for the views
    # ------------------------------------------------------------------

    def _require_db(self) -> Any:
        """Return the underlying client or raise :class:`StoreError`."""
        if self._db is None:
            raise StoreError("reflection store is not connected")
        return self._db


# ---------------------------------------------------------------------------
# Prediction-evaluation view
# ---------------------------------------------------------------------------


class SurrealPredictionEvaluationStore:
    """SurrealDB-backed :class:`PredictionEvaluationStore` implementation."""

    def __init__(self, parent: SurrealReflectionStore) -> None:
        self._parent = parent

    async def store(self, evaluations: Sequence[PredictionEvaluation]) -> None:
        """Persist *evaluations*, replacing any existing rows that share
        the same ``(cycle_id, prediction_id)`` key.

        Raises:
            StoreError: If the database write fails.
        """
        if not evaluations:
            return
        db = self._parent._require_db()
        for ev in evaluations:
            row_id = _evaluation_row_id(ev.cycle_id, ev.prediction_id)
            try:
                await db.query(
                    """
                    UPSERT type::thing('prediction_evaluation', $row_id) SET
                        cycle_id             = $cycle_id,
                        prediction_id        = $prediction_id,
                        hypothesis           = $hypothesis,
                        falsifiable_by       = $falsifiable_by,
                        prediction_timestamp = <datetime> $prediction_timestamp,
                        horizon_end          = <datetime> $horizon_end,
                        confidence           = $confidence,
                        verdict              = $verdict,
                        rationale            = $rationale,
                        evaluated_at         = <datetime> $evaluated_at;
                    """,
                    {
                        "row_id": row_id,
                        "cycle_id": ev.cycle_id,
                        "prediction_id": ev.prediction_id,
                        "hypothesis": ev.hypothesis,
                        "falsifiable_by": ev.falsifiable_by,
                        "prediction_timestamp": ev.prediction_timestamp.isoformat(),
                        "horizon_end": ev.horizon_end.isoformat(),
                        "confidence": ev.confidence,
                        "verdict": ev.verdict,
                        "rationale": ev.rationale,
                        "evaluated_at": ev.evaluated_at.isoformat(),
                    },
                )
            except Exception as exc:
                raise StoreError(
                    f"failed to upsert prediction evaluation "
                    f"({ev.cycle_id!r}, {ev.prediction_id!r})",
                ) from exc

        log.debug(
            "reflection_store.predictions_upserted",
            count=len(list(evaluations)),
        )

    async def list_since(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[PredictionEvaluation]:
        """Return evaluations whose ``evaluated_at`` lies in
        ``[since, until)`` (or ``[since, ∞)`` when *until* is ``None``),
        sorted ascending by ``evaluated_at``.

        Raises:
            StoreError: If the query fails.
        """
        db = self._parent._require_db()
        params: dict[str, object] = {"since": since.isoformat()}
        if until is None:
            surql = (
                "SELECT * FROM prediction_evaluation"
                " WHERE evaluated_at >= <datetime> $since"
                " ORDER BY evaluated_at ASC;"
            )
        else:
            surql = (
                "SELECT * FROM prediction_evaluation"
                " WHERE evaluated_at >= <datetime> $since"
                " AND evaluated_at < <datetime> $until"
                " ORDER BY evaluated_at ASC;"
            )
            params["until"] = until.isoformat()

        try:
            rows = await db.query(surql, params)
        except Exception as exc:
            raise StoreError("failed to query prediction evaluations") from exc

        return _parse_prediction_evaluation_rows(rows)


# ---------------------------------------------------------------------------
# Calibration view
# ---------------------------------------------------------------------------


class SurrealCalibrationStore:
    """SurrealDB-backed :class:`CalibrationStore` implementation."""

    def __init__(self, parent: SurrealReflectionStore, *, clock: Clock = _utc_now) -> None:
        self._parent = parent
        self._clock = clock

    async def get(self, layer: str, model: str) -> ReliabilityDiagram | None:
        """Return the diagram for ``(layer, model)`` or ``None`` if
        nothing has been stored yet.

        Raises:
            StoreError: If the query fails.
        """
        db = self._parent._require_db()
        row_id = _calibration_row_id(layer, model)
        try:
            rows = await db.query(
                "SELECT * FROM type::thing('calibration_diagram', $row_id);",
                {"row_id": row_id},
            )
        except Exception as exc:
            raise StoreError(
                f"failed to load calibration diagram for ({layer!r}, {model!r})",
            ) from exc

        items = _flatten_query_result(rows)
        if not items:
            return None
        row = items[0]
        if not isinstance(row, dict):
            return None
        return _parse_calibration_diagram_row(row)

    async def put(self, diagram: ReliabilityDiagram) -> None:
        """Replace the stored diagram for
        ``(diagram.layer, diagram.model)``.

        Raises:
            StoreError: If the write fails.
        """
        db = self._parent._require_db()
        row_id = _calibration_row_id(diagram.layer, diagram.model)
        try:
            await db.query(
                """
                UPSERT type::thing('calibration_diagram', $row_id) SET
                    layer                   = $layer,
                    model                   = $model,
                    buckets                 = $buckets,
                    total_samples           = $total_samples,
                    brier_sum_squared_error = $brier_sum_squared_error,
                    last_evaluated_at       = $last_evaluated_at,
                    updated_at              = <datetime> $updated_at;
                """,
                {
                    "row_id": row_id,
                    "layer": diagram.layer,
                    "model": diagram.model,
                    "buckets": [b.model_dump() for b in diagram.buckets],
                    "total_samples": diagram.total_samples,
                    "brier_sum_squared_error": diagram.brier_sum_squared_error,
                    "last_evaluated_at": (
                        diagram.last_evaluated_at.isoformat()
                        if diagram.last_evaluated_at is not None
                        else None
                    ),
                    "updated_at": self._clock().isoformat(),
                },
            )
        except Exception as exc:
            raise StoreError(
                f"failed to persist calibration diagram for ({diagram.layer!r}, {diagram.model!r})",
            ) from exc
        log.debug(
            "reflection_store.calibration_upserted",
            layer=diagram.layer,
            model=diagram.model,
        )

    async def list_all(self) -> list[ReliabilityDiagram]:
        """Return every stored diagram, sorted by ``(layer, model)``.

        Raises:
            StoreError: If the query fails.
        """
        db = self._parent._require_db()
        try:
            rows = await db.query("SELECT * FROM calibration_diagram;")
        except Exception as exc:
            raise StoreError("failed to list calibration diagrams") from exc

        diagrams: list[ReliabilityDiagram] = []
        for row in _flatten_query_result(rows):
            if not isinstance(row, dict):
                continue
            try:
                diagrams.append(_parse_calibration_diagram_row(row))
            except Exception:
                log.warning(
                    "reflection_store.calibration_parse_error",
                    exc_info=True,
                )
        diagrams.sort(key=lambda d: (d.layer, d.model))
        return diagrams


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------


def _evaluation_row_id(cycle_id: str, prediction_id: str) -> str:
    """Compose the SurrealDB row id for a prediction evaluation.

    ``cycle_id`` and ``prediction_id`` come from L4 outputs which are
    plain ASCII / URL-safe strings, but we still escape ``:`` to keep
    the composed id round-trippable when surfaced as
    ``"cycle_id:prediction_id"`` elsewhere.
    """
    return f"{cycle_id.replace(':', '_')}__{prediction_id.replace(':', '_')}"


def _calibration_row_id(layer: str, model: str) -> str:
    """Compose the SurrealDB row id for a calibration diagram."""
    return f"{layer.replace(':', '_')}__{model.replace(':', '_').replace('/', '_')}"


def _parse_prediction_evaluation_rows(rows: object) -> list[PredictionEvaluation]:
    """Convert raw SurrealDB rows into :class:`PredictionEvaluation` objects.

    Malformed rows are skipped with a warning rather than failing the
    whole query: a single corrupt row must not blind the calibrator to
    every other evaluation in the window.
    """
    out: list[PredictionEvaluation] = []
    for row in _flatten_query_result(rows):
        if not isinstance(row, dict):
            continue
        try:
            verdict = str(row["verdict"])
            if verdict not in ("correct", "wrong", "undetermined"):
                raise ValueError(f"unknown verdict {verdict!r}")
            out.append(
                PredictionEvaluation(
                    cycle_id=str(row["cycle_id"]),
                    prediction_id=str(row["prediction_id"]),
                    hypothesis=str(row["hypothesis"]),
                    falsifiable_by=str(row["falsifiable_by"]),
                    prediction_timestamp=_parse_dt(row["prediction_timestamp"]),
                    horizon_end=_parse_dt(row["horizon_end"]),
                    confidence=float(row["confidence"]),
                    verdict=_cast_verdict(verdict),
                    rationale=str(row.get("rationale") or ""),
                    evaluated_at=_parse_dt(row["evaluated_at"]),
                )
            )
        except Exception:
            log.warning(
                "reflection_store.prediction_evaluation_parse_error",
                exc_info=True,
            )
    out.sort(key=lambda ev: ev.evaluated_at)
    return out


def _cast_verdict(value: str) -> Verdict:
    """Narrow a validated string into the :data:`Verdict` literal."""
    if value == "correct":
        return "correct"
    if value == "wrong":
        return "wrong"
    return "undetermined"


def _parse_calibration_diagram_row(row: dict[str, object]) -> ReliabilityDiagram:
    """Convert a single SurrealDB row into a :class:`ReliabilityDiagram`.

    Raises:
        ValueError: If the row is missing fields or has the wrong number
            of buckets.
    """
    raw_buckets = row.get("buckets") or []
    if not isinstance(raw_buckets, list):
        raise ValueError("buckets field is not an array")
    if len(raw_buckets) != BUCKET_COUNT:
        raise ValueError(
            f"calibration diagram has {len(raw_buckets)} buckets, expected {BUCKET_COUNT}"
        )
    buckets: list[CalibrationBucket] = []
    for raw in raw_buckets:
        if not isinstance(raw, dict):
            raise ValueError("bucket row is not a mapping")
        buckets.append(
            CalibrationBucket(
                lower=float(raw["lower"]),
                upper=float(raw["upper"]),
                sample_count=int(raw.get("sample_count", 0)),
                success_count=int(raw.get("success_count", 0)),
            )
        )
    return ReliabilityDiagram(
        layer=str(row["layer"]),
        model=str(row["model"]),
        buckets=buckets,
        total_samples=_as_int(row.get("total_samples", 0)),
        brier_sum_squared_error=_as_float(row.get("brier_sum_squared_error", 0.0)),
        last_evaluated_at=(
            _parse_dt(row["last_evaluated_at"])
            if row.get("last_evaluated_at") is not None
            else None
        ),
    )


def _as_int(value: object) -> int:
    """Coerce a SurrealDB scalar into ``int``.

    SurrealDB rows arrive as ``dict[str, object]`` so plain ``int(...)``
    fails mypy; this helper narrows numeric-like values without losing
    the static guarantees.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"cannot coerce {value!r} to int")


def _as_float(value: object) -> float:
    """Coerce a SurrealDB scalar into ``float`` (see :func:`_as_int`)."""
    if isinstance(value, bool | int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"cannot coerce {value!r} to float")


def _flatten_query_result(result: object) -> list[object]:
    """Normalise a SurrealDB query result into a flat list of rows.

    The SurrealDB Python client returns either a list of rows directly
    or a list-of-lists when multiple statements are executed; this
    helper hides the difference, mirroring
    :func:`coremind.world.store._flatten_query_result`.
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

    Accepts ``datetime``, ISO-8601 ``str``, or any object with an
    ``isoformat`` method.  Naive datetimes are coerced to UTC.

    Raises:
        ValueError: If *value* cannot be parsed.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    if hasattr(value, "isoformat"):
        return _parse_dt(value.isoformat())
    raise ValueError(f"cannot parse datetime from {value!r}")
