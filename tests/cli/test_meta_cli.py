"""Tests for the meta-loop CLI commands."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from coremind.cli import cli


def test_meta_status_shows_enabled() -> None:
    """status command prints ENABLED when meta-loop is enabled."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "status"])

    assert result.exit_code == 0
    assert "ENABLED" in result.output


def test_meta_status_shows_interval() -> None:
    """status command prints observation interval."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "status"])

    assert result.exit_code == 0
    assert "300" in result.output


def test_meta_observations_empty() -> None:
    """observations command prints message when no observations exist."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "observations"])

    assert result.exit_code == 0
    assert "No observations found" in result.output


def test_meta_adjustments_empty() -> None:
    """adjustments command prints message when no adjustments exist."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "adjustments"])

    assert result.exit_code == 0
    assert "No adjustments found" in result.output


def test_meta_policies_lists_defaults() -> None:
    """policies command lists all default policies."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "policies"])

    assert result.exit_code == 0
    assert "lower_salience_when_quiet" in result.output
    assert "raise_salience_when_noisy" in result.output
    assert "throttle_failing_plugin" in result.output
    assert "propose_slider_promotion" in result.output


def test_meta_override_requires_flag() -> None:
    """override command fails without --disabled or --enabled."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "override", "--policy", "lower_salience_when_quiet"])

    assert result.exit_code != 0
    assert "Must specify" in result.output


def test_meta_override_rejects_unknown_policy() -> None:
    """override command rejects unknown policy names."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "override", "--policy", "nonexistent", "--disabled"])

    assert result.exit_code != 0
    assert "Unknown policy" in result.output


def test_meta_override_disables_policy(tmp_path: object) -> None:
    """override --disabled writes to config file."""
    import tempfile
    from pathlib import Path

    with patch("coremind.cli.meta.Path.home") as mock_home:
        tmp = Path(tempfile.mkdtemp())
        mock_home.return_value = tmp
        (tmp / ".coremind").mkdir(parents=True)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["meta", "override", "--policy", "lower_salience_when_quiet", "--disabled"]
        )

    assert result.exit_code == 0
    assert "disabled" in result.output


def test_meta_rollback_missing_adjustment() -> None:
    """rollback command fails for nonexistent adjustment ID."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "rollback", "nonexistent-id"])

    assert result.exit_code == 1


def test_meta_proposals_empty() -> None:
    """proposals command prints message when no proposals exist."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "proposals"])

    assert result.exit_code == 0
    assert "No pending proposals" in result.output


def test_meta_approve_succeeds() -> None:
    """approve command prints confirmation."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "approve", "test-proposal-id"])

    assert result.exit_code == 0
    assert "approved" in result.output


def test_meta_deny_succeeds() -> None:
    """deny command prints confirmation."""
    runner = CliRunner()
    result = runner.invoke(cli, ["meta", "deny", "test-proposal-id"])

    assert result.exit_code == 0
    assert "denied" in result.output
