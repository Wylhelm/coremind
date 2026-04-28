"""Tests for the SurrealDB-backed reflection store (Tasks 4.2 + 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from coremind.errors import StoreError
from coremind.reflection.calibration import (
    BUCKET_COUNT,
    empty_diagram,
)
from coremind.reflection.evaluator import PredictionEvaluation
from coremind.reflection.store import (
    SurrealReflectionStore,
    _calibration_row_id,
    _evaluation_row_id,
    _parse_calibration_diagram_row,
    _parse_dt,
    _parse_prediction_evaluation_rows,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def store() -> SurrealReflectionStore:
    return SurrealReflectionStore(
        url="ws://127.0.0.1:8000/rpc",
        username="root",
        password="root",  # noqa: S106
    )


def _eval_row(
    cycle_id: str = "c1",
    prediction_id: str = "p1",
    *,
    confidence: float = 0.7,
    verdict: str = "correct",
) -> dict[str, object]:
    return {
        "cycle_id": cycle_id,
        "prediction_id": prediction_id,
        "hypothesis": "h",
        "falsifiable_by": "obs",
        "prediction_timestamp": _NOW - timedelta(days=1),
        "horizon_end": _NOW,
        "confidence": confidence,
        "verdict": verdict,
        "rationale": "r",
        "evaluated_at": _NOW,
    }


def _evaluation(
    cycle_id: str = "c1",
    prediction_id: str = "p1",
) -> PredictionEvaluation:
    return PredictionEvaluation(
        cycle_id=cycle_id,
        prediction_id=prediction_id,
        hypothesis="h",
        falsifiable_by="obs",
        prediction_timestamp=_NOW - timedelta(days=1),
        horizon_end=_NOW,
        confidence=0.7,
        verdict="correct",
        rationale="r",
        evaluated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_raises_store_error_on_failure(store: SurrealReflectionStore) -> None:
    with patch("coremind.reflection.store.AsyncSurreal") as mock_cls:
        mock_conn = AsyncMock()
        mock_conn.connect.side_effect = ConnectionRefusedError("refused")
        mock_cls.return_value = mock_conn

        with pytest.raises(StoreError, match="failed to connect"):
            await store.connect()


@pytest.mark.asyncio
async def test_apply_schema_runs_schema_file(store: SurrealReflectionStore) -> None:
    mock_db = AsyncMock()
    store._db = mock_db

    await store.apply_schema()

    assert mock_db.query.await_count == 1
    sql = mock_db.query.await_args.args[0]
    assert "DEFINE TABLE prediction_evaluation" in sql
    assert "DEFINE TABLE calibration_diagram" in sql


@pytest.mark.asyncio
async def test_apply_schema_requires_connection(store: SurrealReflectionStore) -> None:
    with pytest.raises(StoreError, match="not connected"):
        await store.apply_schema()


@pytest.mark.asyncio
async def test_apply_schema_wraps_db_failures(store: SurrealReflectionStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("syntax")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to apply"):
        await store.apply_schema()


@pytest.mark.asyncio
async def test_close_is_safe_when_never_connected(store: SurrealReflectionStore) -> None:
    await store.close()
    await store.close()  # idempotent


# ---------------------------------------------------------------------------
# Prediction evaluation view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predictions_store_skips_when_empty(store: SurrealReflectionStore) -> None:
    mock_db = AsyncMock()
    store._db = mock_db

    await store.predictions().store([])

    mock_db.query.assert_not_called()


@pytest.mark.asyncio
async def test_predictions_store_upserts_each_evaluation(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    store._db = mock_db

    evaluations = [_evaluation("c1", "p1"), _evaluation("c1", "p2")]
    await store.predictions().store(evaluations)

    assert mock_db.query.await_count == 2
    first_call = mock_db.query.await_args_list[0]
    sql = first_call.args[0]
    params = first_call.args[1]
    assert "UPSERT type::thing('prediction_evaluation'" in sql
    assert params["row_id"] == _evaluation_row_id("c1", "p1")
    assert params["verdict"] == "correct"
    assert params["confidence"] == 0.7
    # timestamps are serialised as ISO-8601 strings.
    assert isinstance(params["evaluated_at"], str)


@pytest.mark.asyncio
async def test_predictions_store_wraps_db_failures(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("conn lost")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to upsert"):
        await store.predictions().store([_evaluation()])


@pytest.mark.asyncio
async def test_predictions_store_requires_connection(
    store: SurrealReflectionStore,
) -> None:
    with pytest.raises(StoreError, match="not connected"):
        await store.predictions().store([_evaluation()])


@pytest.mark.asyncio
async def test_predictions_list_since_unbounded_query(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = [[_eval_row(), _eval_row("c1", "p2")]]
    store._db = mock_db

    out = await store.predictions().list_since(_NOW - timedelta(days=7))

    assert [(ev.cycle_id, ev.prediction_id) for ev in out] == [("c1", "p1"), ("c1", "p2")]
    sql = mock_db.query.await_args.args[0]
    assert "evaluated_at >= <datetime> $since" in sql
    assert "evaluated_at < <datetime> $until" not in sql


@pytest.mark.asyncio
async def test_predictions_list_since_bounded_query(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = [[]]
    store._db = mock_db

    until = _NOW + timedelta(hours=1)
    await store.predictions().list_since(_NOW - timedelta(days=7), until)

    sql = mock_db.query.await_args.args[0]
    params = mock_db.query.await_args.args[1]
    assert "evaluated_at < <datetime> $until" in sql
    assert params["until"] == until.isoformat()


@pytest.mark.asyncio
async def test_predictions_list_since_wraps_db_failures(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("boom")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to query"):
        await store.predictions().list_since(_NOW - timedelta(days=7))


def test_parse_prediction_evaluation_rows_skips_unknown_verdict() -> None:
    good = _eval_row("c1", "p1")
    bad = _eval_row("c1", "p2")
    bad["verdict"] = "maybe"  # not in the allowed set

    out = _parse_prediction_evaluation_rows([[good, bad]])

    assert len(out) == 1
    assert out[0].prediction_id == "p1"


def test_parse_prediction_evaluation_rows_returns_sorted_by_evaluated_at() -> None:
    older = _eval_row("c1", "p1")
    older["evaluated_at"] = _NOW - timedelta(hours=2)
    newer = _eval_row("c1", "p2")

    out = _parse_prediction_evaluation_rows([[newer, older]])

    assert [ev.prediction_id for ev in out] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# Calibration view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibration_get_returns_none_when_missing(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = [[]]
    store._db = mock_db

    result = await store.calibration().get("reasoning", "m1")

    assert result is None


@pytest.mark.asyncio
async def test_calibration_get_parses_row(store: SurrealReflectionStore) -> None:
    diagram = empty_diagram("reasoning", "m1")
    row = {
        "layer": "reasoning",
        "model": "m1",
        "buckets": [b.model_dump() for b in diagram.buckets],
        "total_samples": 0,
        "brier_sum_squared_error": 0.0,
    }
    mock_db = AsyncMock()
    mock_db.query.return_value = [[row]]
    store._db = mock_db

    result = await store.calibration().get("reasoning", "m1")

    assert result is not None
    assert result.layer == "reasoning"
    assert result.model == "m1"
    assert len(result.buckets) == BUCKET_COUNT


@pytest.mark.asyncio
async def test_calibration_get_wraps_db_failures(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("nope")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to load"):
        await store.calibration().get("reasoning", "m1")


@pytest.mark.asyncio
async def test_calibration_put_serialises_buckets(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    store._db = mock_db
    diagram = empty_diagram("reasoning", "anthropic/claude-opus-4-7")

    fixed_now = _NOW

    cal_view = store.calibration(clock=lambda: fixed_now)
    await cal_view.put(diagram)

    sql = mock_db.query.await_args.args[0]
    params = mock_db.query.await_args.args[1]
    assert "UPSERT type::thing('calibration_diagram'" in sql
    assert params["layer"] == "reasoning"
    assert params["model"] == "anthropic/claude-opus-4-7"
    assert isinstance(params["buckets"], list)
    assert len(params["buckets"]) == BUCKET_COUNT
    # Row id must not contain '/' or ':' so SurrealDB record ids stay valid.
    assert ":" not in params["row_id"]
    assert "/" not in params["row_id"]
    assert params["updated_at"] == fixed_now.isoformat()


@pytest.mark.asyncio
async def test_calibration_put_wraps_db_failures(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("denied")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to persist"):
        await store.calibration().put(empty_diagram("reasoning", "m1"))


@pytest.mark.asyncio
async def test_calibration_list_all_sorts_results(
    store: SurrealReflectionStore,
) -> None:
    diagram_a = empty_diagram("reasoning", "z-model")
    diagram_b = empty_diagram("reasoning", "a-model")
    rows = [
        {
            "layer": d.layer,
            "model": d.model,
            "buckets": [b.model_dump() for b in d.buckets],
            "total_samples": 0,
            "brier_sum_squared_error": 0.0,
        }
        for d in (diagram_a, diagram_b)
    ]
    mock_db = AsyncMock()
    mock_db.query.return_value = [rows]
    store._db = mock_db

    out = await store.calibration().list_all()

    assert [d.model for d in out] == ["a-model", "z-model"]


@pytest.mark.asyncio
async def test_calibration_list_all_wraps_db_failures(
    store: SurrealReflectionStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("boom")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to list"):
        await store.calibration().list_all()


def test_parse_calibration_diagram_row_rejects_wrong_bucket_count() -> None:
    bad_row = {
        "layer": "reasoning",
        "model": "m1",
        "buckets": [{"lower": 0.0, "upper": 1.0}],  # only one bucket
        "total_samples": 0,
        "brier_sum_squared_error": 0.0,
    }
    with pytest.raises(ValueError, match="buckets"):
        _parse_calibration_diagram_row(bad_row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_evaluation_row_id_is_stable_and_safe() -> None:
    rid = _evaluation_row_id("c:1", "p:1")
    assert ":" not in rid
    # Determinism: same inputs always produce the same id.
    assert _evaluation_row_id("c:1", "p:1") == rid


def test_calibration_row_id_strips_path_and_namespace_separators() -> None:
    rid = _calibration_row_id("reasoning", "anthropic/claude:opus")
    assert ":" not in rid
    assert "/" not in rid


def test_parse_dt_handles_string_and_naive_datetime() -> None:
    parsed = _parse_dt("2026-04-20T12:00:00")
    assert parsed.tzinfo is UTC
    assert parsed == datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
