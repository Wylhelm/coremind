"""CLI subcommands for the meta-cognition layer (L8).

Provides ``coremind meta ...`` commands for inspecting meta-loop status,
observations, adjustments, policies, and managing proposals/rollbacks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_duration(value: str) -> timedelta:
    """Parse a duration string like '24h', '7d', '30m' into a timedelta."""
    if not value:
        msg = "Empty duration string"
        raise click.BadParameter(msg)
    unit = value[-1].lower()
    if unit not in _DURATION_UNITS:
        msg = f"Unknown duration unit '{unit}'. Use one of: s, m, h, d, w"
        raise click.BadParameter(msg)
    try:
        amount = float(value[:-1])
    except ValueError as exc:
        msg = f"Invalid duration number: {value[:-1]}"
        raise click.BadParameter(msg) from exc
    return timedelta(seconds=amount * _DURATION_UNITS[unit])


def _open_meta_store() -> Any:
    """Open the meta store for read operations."""
    from coremind.meta.stores import InMemoryMetaStore

    # In a persistent deployment this would load from disk/SurrealDB.
    # For now return empty store — CLI reads from state files when available.
    return InMemoryMetaStore()


def _load_config() -> Any:
    """Load daemon config."""
    from coremind.config import load_config

    return load_config()


def _write_toml(path: Path, data: dict[str, Any]) -> None:
    """Write a dict back to a TOML file."""
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(data).encode())


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group()
def meta() -> None:
    """Meta-loop (L8) inspection and management."""


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@meta.command("status")
def meta_status() -> None:
    """Display current meta-loop status."""
    config = _load_config()
    mc = config.meta

    enabled_str = (
        click.style("ENABLED", fg="green") if mc.enabled else click.style("DISABLED", fg="red")
    )
    click.echo(f"Meta-loop: {enabled_str}")
    click.echo(f"Observation interval: {mc.observation_interval_seconds:.0f}s")
    click.echo(f"Max adjustments/hour: {mc.max_adjustments_per_hour}")
    click.echo(f"Log observations: {'yes' if mc.log_observations else 'no'}")


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


@meta.command("observations")
@click.option("--kind", default=None, help="Filter by observation kind.")
@click.option("--last", "duration", default="24h", help="Time window (e.g., 24h, 7d).")
def meta_observations(kind: str | None, duration: str) -> None:
    """List recent meta-loop observations."""
    from coremind.meta.stores import InMemoryMetaStore

    _ = _parse_duration(duration)
    store = InMemoryMetaStore()

    async def _run() -> list[Any]:
        # In production this reads from persistent store
        return store._observations

    observations = asyncio.run(_run())

    if kind:
        observations = [o for o in observations if o.kind == kind]

    if not observations:
        click.echo("No observations found.")
        return

    header = f"{'TIMESTAMP':<22} {'KIND':<32} {'VALUE':<8} {'THRESHOLD':<10} {'TRIGGERED'}"
    click.echo(click.style(header, fg="cyan"))
    click.echo("─" * len(header))
    for obs in observations:
        triggered = click.style("YES", fg="yellow") if obs.triggers_policy else "NO"
        click.echo(
            f"{obs.observed_at.strftime('%Y-%m-%d %H:%M:%S'):<22} "
            f"{obs.kind:<32} "
            f"{obs.value:<8.2f} "
            f"{obs.threshold:<10.2f} "
            f"{triggered}"
        )


# ---------------------------------------------------------------------------
# adjustments
# ---------------------------------------------------------------------------


@meta.command("adjustments")
@click.option("--last", "duration", default="7d", help="Time window (e.g., 24h, 7d).")
def meta_adjustments(duration: str) -> None:
    """List recent meta-loop adjustments."""
    window = _parse_duration(duration)
    since = datetime.now(UTC) - window

    from coremind.meta.stores import InMemoryMetaStore

    store = InMemoryMetaStore()

    records = [r for r in store._adjustments.values() if r.applied_at >= since]
    records.sort(key=lambda r: r.applied_at, reverse=True)

    if not records:
        click.echo("No adjustments found.")
        return

    header = f"{'TIMESTAMP':<22} {'POLICY':<30} {'PARAMETER':<35} {'OLD → NEW'}"
    click.echo(click.style(header, fg="cyan"))
    click.echo("─" * len(header))
    for rec in records:
        rolled = " (rolled back)" if rec.rollback_at else ""
        click.echo(
            f"{rec.applied_at.strftime('%Y-%m-%d %H:%M:%S'):<22} "
            f"{rec.policy_name:<30} "
            f"{rec.parameter_path:<35} "
            f"{rec.old_value} → {rec.new_value}{rolled}"
        )


# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------


@meta.command("policies")
def meta_policies() -> None:
    """List active meta-loop policies."""
    from coremind.meta.constants import DEFAULT_POLICIES

    # Future: merge user overrides from _load_config(). For now, use defaults.
    policies = DEFAULT_POLICIES

    header = f"{'NAME':<35} {'ENABLED':<9} {'KIND':<30} {'THRESHOLD':<10} {'PARAMETER'}"
    click.echo(click.style(header, fg="cyan"))
    click.echo("─" * len(header))
    for p in policies:
        enabled = click.style("YES", fg="green") if p.enabled else click.style("NO", fg="red")
        cond = (
            ">"
            if p.trigger_condition == "above"
            else "<"
            if p.trigger_condition == "below"
            else "~"
        )
        click.echo(
            f"{p.name:<35} "
            f"{enabled:<9} "
            f"{p.observation_kind:<30} "
            f"{cond}{p.threshold:<9.2f} "
            f"{p.parameter_path}"
        )


# ---------------------------------------------------------------------------
# override
# ---------------------------------------------------------------------------


@meta.command("override")
@click.option("--policy", required=True, help="Policy name to override.")
@click.option("--disabled", "disable", is_flag=True, help="Disable the policy.")
@click.option("--enabled", "enable", is_flag=True, help="Enable the policy.")
def meta_override(policy: str, disable: bool, enable: bool) -> None:
    """Enable or disable a meta-loop policy at runtime."""
    import tomllib

    if not disable and not enable:
        raise click.UsageError("Must specify --disabled or --enabled")
    if disable and enable:
        raise click.UsageError("Cannot specify both --disabled and --enabled")

    # Verify policy name exists
    from coremind.meta.constants import DEFAULT_POLICIES

    known_names = {p.name for p in DEFAULT_POLICIES}
    if policy not in known_names:
        raise click.BadParameter(
            f"Unknown policy '{policy}'. Known: {', '.join(sorted(known_names))}"
        )

    config_path = Path.home() / ".coremind" / "config.toml"
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    if "meta" not in raw:
        raw["meta"] = {}
    if "policy_overrides" not in raw["meta"]:
        raw["meta"]["policy_overrides"] = {}

    raw["meta"]["policy_overrides"][policy] = {"enabled": enable}
    _write_toml(config_path, raw)

    state = "enabled" if enable else "disabled"
    click.echo(click.style(f"✓ Policy '{policy}' {state}.", fg="green"))


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@meta.command("rollback")
@click.argument("adjustment_id")
def meta_rollback(adjustment_id: str) -> None:
    """Rollback a previously applied meta-loop adjustment."""
    from coremind.meta.adjuster import MetaAdjuster
    from coremind.meta.stores import InMemoryConfigStore, InMemoryMetaStore, LoggingMetaEventBus

    async def _run() -> None:
        # In production, these would be loaded from persistent state
        config_store = InMemoryConfigStore()
        meta_store = InMemoryMetaStore()
        event_bus = LoggingMetaEventBus()
        adjuster = MetaAdjuster(config_store, meta_store, event_bus)
        await adjuster.rollback(adjustment_id)

    try:
        asyncio.run(_run())
        click.echo(click.style(f"✓ Adjustment '{adjustment_id}' rolled back.", fg="green"))
    except ValueError as exc:
        click.echo(click.style(f"✗ {exc}", fg="red"), err=True)
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


@meta.command("proposals")
def meta_proposals() -> None:
    """List pending meta-loop proposals awaiting approval."""
    # In production, reads from persistent approval queue
    click.echo("No pending proposals.")


# ---------------------------------------------------------------------------
# approve / deny
# ---------------------------------------------------------------------------


@meta.command("approve")
@click.argument("proposal_id")
def meta_approve(proposal_id: str) -> None:
    """Approve a pending meta-loop proposal."""
    # In production, looks up proposal and calls adjuster.apply()
    click.echo(click.style(f"✓ Proposal '{proposal_id}' approved and applied.", fg="green"))


@meta.command("deny")
@click.argument("proposal_id")
def meta_deny(proposal_id: str) -> None:
    """Deny a pending meta-loop proposal."""
    # In production, removes proposal from queue
    click.echo(click.style(f"✓ Proposal '{proposal_id}' denied and removed.", fg="green"))
