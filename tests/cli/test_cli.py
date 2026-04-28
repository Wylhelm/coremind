"""Tests for src/coremind/cli/__init__.py."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from coremind.cli import (
    _ENTITY_FILTER_PARTS,
    _format_uptime,
    _is_process_alive,
    _parse_duration,
    cli,
)
from coremind.errors import StoreError
from coremind.world.model import Entity, EntityRef, WorldEventRecord, WorldSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a Click test runner (stderr always separate in Click 8.2+)."""
    return CliRunner(mix_stderr=False)


@pytest.fixture()
def empty_snapshot() -> WorldSnapshot:
    """A WorldSnapshot with no entities, relationships, or events."""
    return WorldSnapshot(taken_at=datetime.now(UTC))


@pytest.fixture()
def snapshot_with_plugin() -> WorldSnapshot:
    """A WorldSnapshot with one entity attributed to a plugin."""
    return WorldSnapshot(
        taken_at=datetime.now(UTC),
        entities=[
            Entity(
                type="host",
                display_name="myhost",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                properties={"cpu_percent": 42.0},
                source_plugins=["plugin-systemstats"],
            )
        ],
    )


def _make_event(
    source: str = "plugin-systemstats",
    attribute: str = "cpu_percent",
    entity_type: str = "host",
    entity_id: str = "myhost",
) -> WorldEventRecord:
    """Build a minimal WorldEventRecord for testing."""
    return WorldEventRecord(
        id="evt-001",
        timestamp=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        source=source,
        source_version="0.1.0",
        signature="dummysig",
        entity=EntityRef(type=entity_type, id=entity_id),
        attribute=attribute,
        value=42.0,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("2d", 172800),
        ("1w", 604800),
    ],
)
def test_parse_duration_valid(value: str, expected_seconds: int) -> None:
    """Valid duration strings are parsed to the correct timedelta."""
    result = _parse_duration(value)

    assert result == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1x",
        "abc",
        "h",
        "-1h",
        "1.5h",
    ],
)
def test_parse_duration_invalid(value: str) -> None:
    """Invalid duration strings raise click.BadParameter."""
    with pytest.raises(click.BadParameter):
        _parse_duration(value)


# ---------------------------------------------------------------------------
# _format_uptime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (5, "5s"),
        (65, "1m 5s"),
        (3665, "1h 1m 5s"),
        (90065, "1d 1h 1m 5s"),
    ],
)
def test_format_uptime(seconds: float, expected: str) -> None:
    """Uptime seconds are formatted into human-readable strings."""
    assert _format_uptime(seconds) == expected


# ---------------------------------------------------------------------------
# _is_process_alive
# ---------------------------------------------------------------------------


def test_is_process_alive_with_current_process() -> None:
    """The current process reports as alive."""
    assert _is_process_alive(os.getpid()) is True


def test_is_process_alive_with_dead_pid() -> None:
    """A PID that cannot exist reports as not alive."""
    # PID 0 is not a valid user-space PID; kill(0, 0) sends to the process group.
    # Use a very large PID that is almost certainly not running.
    assert _is_process_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# ENTITY_FILTER_PARTS constant
# ---------------------------------------------------------------------------


def test_entity_filter_parts_constant() -> None:
    """ENTITY_FILTER_PARTS equals 2 (type and id)."""
    assert _ENTITY_FILTER_PARTS == 2


# ---------------------------------------------------------------------------
# daemon status — stopped
# ---------------------------------------------------------------------------


def test_daemon_status_stopped(runner: CliRunner) -> None:
    """daemon status prints 'stopped' when no PID file exists."""
    with patch("coremind.cli._read_pid", return_value=None):
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_daemon_status_not_alive(runner: CliRunner) -> None:
    """daemon status prints 'stopped' when PID exists but process is dead."""
    with (
        patch("coremind.cli._read_pid", return_value=999_999_999),
        patch("coremind.cli._is_process_alive", return_value=False),
    ):
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_daemon_status_running(runner: CliRunner, tmp_path: Path) -> None:
    """daemon status shows 'running' with PID and uptime when daemon is up."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[])

    with (
        patch("coremind.cli._read_pid", return_value=os.getpid()),
        patch("coremind.cli._is_process_alive", return_value=True),
        patch("coremind.cli._PID_FILE", pid_file),
        patch("coremind.cli._open_store", return_value=mock_store),
    ):
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "running" in result.output
    assert str(os.getpid()) in result.output
    assert "event rate: 0 events/min" in result.output


# ---------------------------------------------------------------------------
# events tail
# ---------------------------------------------------------------------------


def test_events_tail_displays_events(runner: CliRunner) -> None:
    """events tail prints one line per event and stops on CancelledError."""
    ev = _make_event()
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[ev])

    with (
        patch("coremind.cli._open_store", return_value=mock_store),
        patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
    ):
        result = runner.invoke(cli, ["events", "tail"])

    assert result.exit_code == 0
    assert "cpu_percent" in result.output
    assert "host:myhost" in result.output


def test_events_tail_connect_error_shows_message(runner: CliRunner) -> None:
    """events tail exits cleanly when SurrealDB is unreachable on connect."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock(side_effect=StoreError("connection refused"))
    mock_store.close = AsyncMock()

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["events", "tail"])

    assert result.exit_code == 0
    assert "cannot connect" in result.stderr


def test_events_tail_store_error_during_poll_shows_warning(runner: CliRunner) -> None:
    """events tail shows a warning on a mid-tail StoreError and keeps running."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(side_effect=StoreError("query failed"))

    with (
        patch("coremind.cli._open_store", return_value=mock_store),
        patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
    ):
        result = runner.invoke(cli, ["events", "tail"])

    assert result.exit_code == 0
    assert "SurrealDB error" in result.stderr


# ---------------------------------------------------------------------------
# events query
# ---------------------------------------------------------------------------


def test_events_query_invalid_since(runner: CliRunner) -> None:
    """events query exits with an error for an unrecognised --since value."""
    result = runner.invoke(cli, ["events", "query", "--since", "bad"])

    assert result.exit_code != 0
    assert "unrecognised duration" in result.stderr


def test_events_query_invalid_entity_filter(runner: CliRunner) -> None:
    """events query exits when --entity does not contain exactly one colon."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["events", "query", "--entity", "nocolon"])

    assert result.exit_code != 0
    assert "type:id" in result.stderr


def test_events_query_no_results(runner: CliRunner) -> None:
    """events query produces no output when no events match."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["events", "query"])

    assert result.exit_code == 0
    assert result.output == ""


def test_events_query_prints_matching_events(runner: CliRunner) -> None:
    """events query prints one line per matching event."""
    ev = _make_event()
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[ev])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["events", "query", "--since", "1h"])

    assert result.exit_code == 0
    assert "cpu_percent" in result.output
    assert "host:myhost" in result.output


def test_events_query_entity_filter_excludes_other_entities(runner: CliRunner) -> None:
    """events query --entity filters out events for other entities."""
    ev_match = _make_event(entity_type="host", entity_id="myhost")
    ev_other = _make_event(entity_type="host", entity_id="otherhost")

    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[ev_match, ev_other])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(
            cli,
            ["events", "query", "--entity", "host:myhost"],
        )

    assert result.exit_code == 0
    assert "myhost" in result.output
    assert "otherhost" not in result.output


def test_events_query_attribute_filter(runner: CliRunner) -> None:
    """events query --attribute filters to only matching attribute names."""
    ev_cpu = _make_event(attribute="cpu_percent")
    ev_mem = _make_event(attribute="memory_percent")

    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.recent_events = AsyncMock(return_value=[ev_cpu, ev_mem])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(
            cli,
            ["events", "query", "--attribute", "cpu_percent"],
        )

    assert result.exit_code == 0
    assert "cpu_percent" in result.output
    assert "memory_percent" not in result.output


# ---------------------------------------------------------------------------
# plugin list
# ---------------------------------------------------------------------------


def test_plugin_list_no_plugins(runner: CliRunner, empty_snapshot: WorldSnapshot) -> None:
    """plugin list reports no plugins when the snapshot has no entities."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=empty_snapshot)

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["plugin", "list"])

    assert result.exit_code == 0
    assert "No plugins" in result.output


def test_plugin_list_shows_registered_plugins(
    runner: CliRunner, snapshot_with_plugin: WorldSnapshot
) -> None:
    """plugin list shows each distinct plugin ID and entity count."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=snapshot_with_plugin)

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["plugin", "list"])

    assert result.exit_code == 0
    assert "plugin-systemstats" in result.output
    assert "1" in result.output  # one entity contributed


# ---------------------------------------------------------------------------
# plugin info
# ---------------------------------------------------------------------------


def test_plugin_info_not_found(runner: CliRunner, empty_snapshot: WorldSnapshot) -> None:
    """plugin info exits non-zero when the plugin is not in the World Model."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=empty_snapshot)
    mock_store.recent_events = AsyncMock(return_value=[])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["plugin", "info", "no-such-plugin"])

    assert result.exit_code != 0
    assert "not found" in result.stderr


def test_plugin_info_shows_details(runner: CliRunner, snapshot_with_plugin: WorldSnapshot) -> None:
    """plugin info shows entity count, event count, and attributes."""
    ev = _make_event(source="plugin-systemstats", attribute="cpu_percent")
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=snapshot_with_plugin)
    mock_store.recent_events = AsyncMock(return_value=[ev])

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["plugin", "info", "plugin-systemstats"])

    assert result.exit_code == 0
    assert "plugin-systemstats" in result.output
    assert "Entities contributed: 1" in result.output
    assert "Events (last 24h):    1" in result.output
    assert "cpu_percent" in result.output


# ---------------------------------------------------------------------------
# world snapshot
# ---------------------------------------------------------------------------


def test_world_snapshot_produces_valid_json(
    runner: CliRunner, snapshot_with_plugin: WorldSnapshot
) -> None:
    """world snapshot emits valid JSON with expected top-level keys."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=snapshot_with_plugin)

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["world", "snapshot"])

    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert "taken_at" in doc
    assert "entities" in doc
    assert "relationships" in doc
    assert "recent_events" in doc


def test_world_snapshot_includes_entity_data(
    runner: CliRunner, snapshot_with_plugin: WorldSnapshot
) -> None:
    """world snapshot JSON contains the entity type and display_name."""
    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.snapshot = AsyncMock(return_value=snapshot_with_plugin)

    with patch("coremind.cli._open_store", return_value=mock_store):
        result = runner.invoke(cli, ["world", "snapshot"])

    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert len(doc["entities"]) == 1
    assert doc["entities"][0]["type"] == "host"
    assert doc["entities"][0]["display_name"] == "myhost"
