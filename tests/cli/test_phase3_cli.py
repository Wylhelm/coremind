"""Phase 3 CLI command-group tests.

Covers intent / approvals / action / audit / notify / quiet-hours.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coremind.action.journal import ActionJournal
from coremind.action.schemas import Action, ActionResult
from coremind.cli import cli
from coremind.config import DaemonConfig, NotifyConfig, QuietHoursConfig, TelegramConfig
from coremind.crypto.signatures import ensure_daemon_keypair
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import ActionProposal, Intent, InternalQuestion


@pytest.fixture()
def runner() -> CliRunner:
    """Click test runner with stderr separated."""
    return CliRunner(mix_stderr=False)


@pytest.fixture()
def cli_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return (intent_store_path, audit_log_path) under a temp tree."""
    return tmp_path / "intents.jsonl", tmp_path / "audit.log"


@pytest.fixture()
def patched_config(
    tmp_path: Path,
    cli_paths: tuple[Path, Path],
) -> DaemonConfig:
    """Build a DaemonConfig pointed at temp paths and patch ``load_config``."""
    intent_path, audit_path = cli_paths
    cfg = DaemonConfig(
        intent_store_path=intent_path,
        audit_log_path=audit_path,
        notify=NotifyConfig(
            primary="dashboard",
            fallbacks=[],
            telegram=TelegramConfig(enabled=False, chat_id=""),
        ),
        quiet_hours=QuietHoursConfig(enabled=False),
    )
    with patch("coremind.cli.load_config", return_value=cfg):
        yield cfg  # type: ignore[misc]


def _make_intent(
    intent_id: str = "i1",
    status: str = "pending_approval",
    *,
    with_action: bool = True,
) -> Intent:
    """Build a frozen-shape intent for tests."""
    proposal = (
        ActionProposal(
            operation="homeassistant.turn_on",
            parameters={"entity_id": "light.kitchen"},
            expected_outcome="Kitchen on.",
            reversal="homeassistant.turn_off",
            action_class="light",
        )
        if with_action
        else None
    )
    return Intent(
        id=intent_id,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        question=InternalQuestion(id="q", text="Turn it on?"),
        proposed_action=proposal,
        salience=0.7,
        confidence=0.8,
        category="ask",
        status=status,  # type: ignore[arg-type]
    )


async def _seed_intent(path: Path, intent: Intent) -> None:
    """Persist an intent into a fresh store."""
    store = IntentStore(path)
    await store.save(intent)


async def _seed_action(audit: Path, action: Action) -> Action:
    """Persist a completed action with a result into a fresh journal."""
    private = ensure_daemon_keypair()
    journal = ActionJournal(audit, private, private.public_key())
    await journal.load()
    appended = await journal.append(action)
    appended.result = ActionResult(
        action_id=appended.id,
        status="ok",
        message="",
        completed_at=datetime.now(UTC),
        reversed_by_operation="homeassistant.turn_off",
        reversal_parameters={"entity_id": "light.kitchen"},
    )
    await journal.update_result(appended)
    return appended


def _make_action(action_id: str = "a1") -> Action:
    """Build an unsigned action for tests."""
    return Action(
        id=action_id,
        intent_id="i1",
        timestamp=datetime.now(UTC),
        category="safe",
        operation="homeassistant.turn_on",
        parameters={"entity_id": "light.kitchen"},
        action_class="light",
        expected_outcome="on",
        reversal="homeassistant.turn_off",
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# intent
# ---------------------------------------------------------------------------


def test_intent_list_empty(runner: CliRunner, patched_config: DaemonConfig) -> None:
    """``intent list`` reports a friendly message when the store is empty."""
    _ = patched_config
    result = runner.invoke(cli, ["intent", "list"])

    assert result.exit_code == 0, result.stderr
    assert "No intents matched." in result.stdout


def test_intent_list_filters_by_status(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``intent list --status pending`` returns only pending intents."""

    asyncio.run(_seed_intent(patched_config.intent_store_path, _make_intent("i1", "pending")))
    asyncio.run(_seed_intent(patched_config.intent_store_path, _make_intent("i2", "done")))

    result = runner.invoke(cli, ["intent", "list", "--status", "pending"])

    assert result.exit_code == 0, result.stderr
    assert "i1" in result.stdout
    assert "i2" not in result.stdout


def test_intent_show_unknown(runner: CliRunner, patched_config: DaemonConfig) -> None:
    """``intent show`` exits non-zero on an unknown id."""
    _ = patched_config
    result = runner.invoke(cli, ["intent", "show", "nope"])

    assert result.exit_code != 0
    assert "unknown intent" in result.stderr


def test_intent_approve_marks_status(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``intent approve`` mutates status to ``approved`` and journals it."""

    asyncio.run(
        _seed_intent(
            patched_config.intent_store_path,
            _make_intent("i1", "pending_approval"),
        )
    )

    result = runner.invoke(cli, ["intent", "approve", "i1", "--note", "ok"])

    assert result.exit_code == 0, result.stderr
    intent = asyncio.run(IntentStore(patched_config.intent_store_path).get("i1"))
    assert intent is not None
    assert intent.status == "approved"
    assert intent.human_feedback == "ok"


def test_intent_reject_marks_status(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``intent reject`` mutates status to ``rejected``."""

    asyncio.run(
        _seed_intent(
            patched_config.intent_store_path,
            _make_intent("i1", "pending_approval"),
        )
    )

    result = runner.invoke(cli, ["intent", "reject", "i1"])

    assert result.exit_code == 0, result.stderr
    intent = asyncio.run(IntentStore(patched_config.intent_store_path).get("i1"))
    assert intent is not None
    assert intent.status == "rejected"


def test_intent_snooze_increments_count(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``intent snooze --for 1h`` schedules a new TTL and bumps snooze_count."""

    asyncio.run(
        _seed_intent(
            patched_config.intent_store_path,
            _make_intent("i1", "pending_approval"),
        )
    )

    result = runner.invoke(cli, ["intent", "snooze", "i1", "--for", "1h"])

    assert result.exit_code == 0, result.stderr
    intent = asyncio.run(IntentStore(patched_config.intent_store_path).get("i1"))
    assert intent is not None
    assert intent.snooze_count == 1
    assert intent.expires_at is not None


def test_intent_snooze_refuses_second_attempt(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """A second snooze is refused per Phase 3 semantics."""

    intent = _make_intent("i1", "pending_approval")
    intent.snooze_count = 1
    asyncio.run(_seed_intent(patched_config.intent_store_path, intent))

    result = runner.invoke(cli, ["intent", "snooze", "i1", "--for", "1h"])

    assert result.exit_code != 0
    assert "already been snoozed" in result.stderr


def test_intent_decision_rejects_terminal_state(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """Decisions on already-resolved intents fail loudly."""

    asyncio.run(_seed_intent(patched_config.intent_store_path, _make_intent("i1", "approved")))

    result = runner.invoke(cli, ["intent", "approve", "i1"])

    assert result.exit_code != 0
    assert "not awaiting a decision" in result.stderr


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


def test_approvals_pending_lists_only_pending_approval(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``approvals pending`` filters the store on ``pending_approval``."""

    asyncio.run(
        _seed_intent(
            patched_config.intent_store_path,
            _make_intent("i1", "pending_approval"),
        )
    )
    asyncio.run(_seed_intent(patched_config.intent_store_path, _make_intent("i2", "approved")))

    result = runner.invoke(cli, ["approvals", "pending"])

    assert result.exit_code == 0, result.stderr
    assert "i1" in result.stdout
    assert "i2" not in result.stdout


def test_approvals_approve_alias(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``approvals approve`` marks the intent approved."""

    asyncio.run(
        _seed_intent(
            patched_config.intent_store_path,
            _make_intent("i1", "pending_approval"),
        )
    )

    result = runner.invoke(cli, ["approvals", "approve", "i1"])

    assert result.exit_code == 0, result.stderr
    intent = asyncio.run(IntentStore(patched_config.intent_store_path).get("i1"))
    assert intent is not None
    assert intent.status == "approved"


# ---------------------------------------------------------------------------
# action
# ---------------------------------------------------------------------------


def test_action_list_empty(runner: CliRunner, patched_config: DaemonConfig) -> None:
    """``action list`` is friendly when the journal is empty."""
    _ = patched_config
    result = runner.invoke(cli, ["action", "list"])

    assert result.exit_code == 0, result.stderr
    assert "No actions" in result.stdout


def test_action_list_renders_recent_action(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``action list`` includes a journaled action."""

    asyncio.run(_seed_action(patched_config.audit_log_path, _make_action("a1")))

    result = runner.invoke(cli, ["action", "list", "--last", "1h"])

    assert result.exit_code == 0, result.stderr
    assert "a1" in result.stdout
    assert "homeassistant.turn_on" in result.stdout


def test_action_show_renders_full_json(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``action show`` returns the full action as JSON."""

    asyncio.run(_seed_action(patched_config.audit_log_path, _make_action("a1")))

    result = runner.invoke(cli, ["action", "show", "a1"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == "a1"
    assert payload["operation"] == "homeassistant.turn_on"


def test_action_reverse_requires_running_daemon(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``action reverse`` rejects CLI-side reversal in Phase 3."""

    asyncio.run(_seed_action(patched_config.audit_log_path, _make_action("a1")))

    result = runner.invoke(cli, ["action", "reverse", "a1"])

    assert result.exit_code != 0
    assert "daemon" in result.stderr


def test_action_reverse_unknown_id(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``action reverse`` errors clearly on an unknown action id."""
    _ = patched_config
    result = runner.invoke(cli, ["action", "reverse", "nope"])

    assert result.exit_code != 0
    assert "unknown action" in result.stderr


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def test_audit_verify_clean_journal(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``audit verify`` reports OK when the journal chain is intact."""

    asyncio.run(_seed_action(patched_config.audit_log_path, _make_action("a1")))

    result = runner.invoke(cli, ["audit", "verify"])

    assert result.exit_code == 0, result.stderr
    assert "journal ok" in result.stdout


def test_audit_tail_prints_entries(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``audit tail`` prints recent journal entries."""

    asyncio.run(_seed_action(patched_config.audit_log_path, _make_action("a1")))

    result = runner.invoke(cli, ["audit", "tail"])

    assert result.exit_code == 0, result.stderr
    assert "a1" in result.stdout


# ---------------------------------------------------------------------------
# notify / quiet-hours
# ---------------------------------------------------------------------------


def test_notify_test_dashboard(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``notify test`` succeeds via the in-process dashboard port."""
    _ = patched_config
    result = runner.invoke(cli, ["notify", "test"])

    assert result.exit_code == 0, result.stderr
    assert "test notification sent" in result.stdout


def test_notify_test_unsupported_port(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """Other ports refuse cleanly in Phase 3."""
    _ = patched_config
    result = runner.invoke(cli, ["notify", "test", "--port", "telegram"])

    assert result.exit_code != 0
    assert "dashboard" in result.stderr


def test_notify_status_renders_config(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``notify status`` reflects the configured router state."""
    _ = patched_config
    result = runner.invoke(cli, ["notify", "status"])

    assert result.exit_code == 0, result.stderr
    assert "primary" in result.stdout
    assert "telegram" in result.stdout


def test_quiet_hours_show(
    runner: CliRunner,
    patched_config: DaemonConfig,
) -> None:
    """``quiet-hours show`` prints the configured policy."""
    _ = patched_config
    result = runner.invoke(cli, ["quiet-hours", "show"])

    assert result.exit_code == 0, result.stderr
    assert "Quiet hours" in result.stdout
    assert "UTC" in result.stdout
