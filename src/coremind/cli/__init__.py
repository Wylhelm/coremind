"""CLI entry-point for CoreMind (``coremind`` command).

Commands
--------

coremind daemon start          — blocks, runs the daemon
coremind daemon status         — is it running? how long? event rate?
coremind events tail           — streaming, colorized output
coremind events query          — filtered historical query
coremind plugin list           — list plugins that have emitted events
coremind plugin info <id>      — detail for one plugin
coremind world snapshot        — dump graph to stdout as JSON
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from coremind.action.journal import ActionJournal, VerifyReport
from coremind.action.schemas import Action
from coremind.config import DaemonConfig, load_config
from coremind.core.daemon import CoreMindDaemon
from coremind.crypto.signatures import ensure_daemon_keypair
from coremind.errors import ActionError, JournalError, StoreError
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent
from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_DIR: Path = Path.home() / ".coremind" / "run"
_PID_FILE: Path = _RUN_DIR / "daemon.pid"

_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

# entity filter format: "<entity_type>:<entity_id>" splits into exactly two parts
_ENTITY_FILTER_PARTS: int = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_key(_source: str) -> Ed25519PublicKey | None:
    """Null key resolver used for read-only CLI store access.

    The CLI never calls ``apply_event``, so signature verification is never
    triggered.  Returning ``None`` prevents accidental writes from resolving
    a key.

    Args:
        _source: Ignored plugin source identifier.

    Returns:
        Always ``None``.
    """
    return None


def _parse_duration(value: str) -> timedelta:
    """Parse a human-friendly duration string into a :class:`timedelta`.

    Accepted suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days), ``w`` (weeks).  Examples: ``30s``, ``5m``, ``1h``, ``2d``.

    Args:
        value: Duration string to parse.

    Returns:
        A :class:`datetime.timedelta` for the given duration.

    Raises:
        click.BadParameter: If the format is not recognised.
    """
    value = value.strip()
    if not value:
        raise click.BadParameter("empty duration string")
    suffix = value[-1]
    numeric_part = value[:-1]
    if suffix not in _DURATION_UNITS or not numeric_part.isdigit():
        raise click.BadParameter(
            f"unrecognised duration {value!r}; accepted formats: 30s, 5m, 1h, 2d, 1w"
        )
    return timedelta(seconds=int(numeric_part) * _DURATION_UNITS[suffix])


def _format_uptime(seconds: float) -> str:
    """Format *seconds* as a human-readable uptime string.

    Args:
        seconds: Number of seconds of uptime.

    Returns:
        String like ``2d 4h 30m 5s``, or ``0s`` for zero seconds.
    """
    parts: list[str] = []
    total = int(seconds)
    for unit, divisor in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, total = divmod(total, divisor)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) if parts else "0s"


def _write_pid_file() -> None:
    """Write the current process PID to the daemon PID file (chmod 600)."""
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    _PID_FILE.chmod(0o600)


def _remove_pid_file() -> None:
    """Remove the daemon PID file if it exists."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        log.warning("cli.pid_file_remove_failed")


def _read_pid() -> int | None:
    """Read the daemon PID from the PID file.

    Returns:
        The PID integer if the file is readable and contains a valid integer,
        ``None`` otherwise.
    """
    try:
        return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _is_process_alive(pid: int) -> bool:
    """Return ``True`` if *pid* refers to a currently running process.

    Sends signal 0 to the process, which probes existence without delivering
    any signal.

    Args:
        pid: Process identifier to probe.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _open_store() -> WorldStore:
    """Construct a read-only :class:`WorldStore` from the daemon config.

    Returns a disconnected store; the caller must ``await store.connect()``
    before issuing any queries.

    Returns:
        A :class:`WorldStore` configured for read-only CLI access.
    """
    config = load_config()
    return WorldStore(
        url=config.world_db_url,
        username=config.world_db_username,
        password=config.world_db_password,
        key_resolver=_no_key,
    )


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """CoreMind cognitive daemon — management interface."""


# ---------------------------------------------------------------------------
# daemon group
# ---------------------------------------------------------------------------


@cli.group()
def daemon() -> None:
    """Start and inspect the CoreMind daemon."""


@daemon.command("start")
def daemon_start() -> None:
    """Start the CoreMind daemon (blocks until SIGINT / SIGTERM)."""
    pid = _read_pid()
    # Phase 1 known limitation: PID reuse on long-running systems may cause a
    # false "already running" report if a stale PID file refers to an unrelated
    # process that inherited the same PID after a crash without cleanup.  A
    # flock-based approach is planned for a later phase.
    if pid is not None and _is_process_alive(pid):
        click.echo(f"Daemon is already running (PID {pid}).", err=True)
        sys.exit(1)
    _write_pid_file()
    try:
        asyncio.run(CoreMindDaemon().run_forever())
    finally:
        _remove_pid_file()


@daemon.command("status")
def daemon_status() -> None:
    """Report whether the daemon is running, its uptime, and event rate."""
    pid = _read_pid()
    if pid is None or not _is_process_alive(pid):
        click.echo(click.style("● daemon: stopped", fg="red"))
        return

    uptime_seconds = time.time() - _PID_FILE.stat().st_mtime
    uptime_str = _format_uptime(uptime_seconds)
    click.echo(click.style("● daemon: running", fg="green") + f"  PID {pid}  uptime {uptime_str}")

    async def _fetch_rate() -> int:
        """Query SurrealDB for the number of events in the last minute."""
        store = _open_store()
        try:
            await store.connect()
            since = datetime.now(UTC) - timedelta(minutes=1)
            recent = await store.recent_events(since=since)
            return len(recent)
        except StoreError:
            return -1
        finally:
            await store.close()

    rate = asyncio.run(_fetch_rate())
    if rate < 0:
        click.echo("  event rate: (SurrealDB unreachable)")
    else:
        click.echo(f"  event rate: {rate} events/min")


# ---------------------------------------------------------------------------
# events group
# ---------------------------------------------------------------------------


@cli.group()
def events() -> None:
    """Query and stream events from the World Model."""


@events.command("tail")
@click.option(
    "--poll-interval",
    default=2.0,
    show_default=True,
    help="Seconds between polls.",
)
def events_tail(poll_interval: float) -> None:
    """Stream new events to stdout as they arrive (Ctrl-C to stop)."""

    async def _tail() -> None:
        """Poll SurrealDB for new events in a loop until cancelled."""
        store = _open_store()
        try:
            await store.connect()
        except StoreError as exc:
            click.echo(
                click.style(f"Error: cannot connect to SurrealDB — {exc}", fg="red"), err=True
            )
            return
        try:
            cursor = datetime.now(UTC)
            click.echo(click.style("Tailing events — press Ctrl-C to stop.\n", fg="cyan"))
            while True:
                try:
                    new_events = await store.recent_events(since=cursor, limit=100)
                except StoreError as exc:
                    click.echo(click.style(f"Warning: SurrealDB error: {exc}", fg="red"), err=True)
                    await asyncio.sleep(poll_interval)
                    continue
                for ev in new_events:
                    ts = ev.timestamp.strftime("%H:%M:%S")
                    entity = f"{ev.entity.type}:{ev.entity.id}"
                    line = (
                        click.style(ts, fg="blue")
                        + "  "
                        + click.style(entity, fg="yellow")
                        + "  "
                        + click.style(ev.attribute, fg="green")
                        + "="
                        + click.style(str(ev.value), fg="white")
                    )
                    if ev.unit:
                        line += click.style(f" {ev.unit}", fg="bright_black")
                    click.echo(line)
                if new_events:
                    batch_max = max(ev.timestamp for ev in new_events)
                    cursor = max(cursor, batch_max + timedelta(microseconds=1))
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            await store.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_tail())


@events.command("query")
@click.option("--entity", "entity_filter", default=None, help="Filter by entity (type:id).")
@click.option("--attribute", default=None, help="Filter by attribute name.")
@click.option(
    "--since",
    "since_str",
    default="1h",
    show_default=True,
    help="How far back to look (e.g. 30s, 5m, 1h, 2d).",
)
@click.option("--limit", default=100, show_default=True, help="Maximum number of results.")
def events_query(
    entity_filter: str | None,
    attribute: str | None,
    since_str: str,
    limit: int,
) -> None:
    """Query historical events with optional filters."""
    duration = _parse_duration(since_str)

    async def _query() -> list[Any]:
        """Fetch recent events from the world store."""
        store = _open_store()
        await store.connect()
        try:
            since = datetime.now(UTC) - duration
            return await store.recent_events(since=since, limit=limit)
        finally:
            await store.close()

    all_events = asyncio.run(_query())

    results = all_events
    if entity_filter is not None:
        parts = entity_filter.split(":", 1)
        if len(parts) != _ENTITY_FILTER_PARTS:
            click.echo(
                f"--entity must be 'type:id', got {entity_filter!r}",
                err=True,
            )
            sys.exit(1)
        etype, eid = parts
        results = [e for e in results if e.entity.type == etype and e.entity.id == eid]

    if attribute is not None:
        results = [e for e in results if e.attribute == attribute]

    for ev in results:
        ts = ev.timestamp.isoformat()
        entity_str = f"{ev.entity.type}:{ev.entity.id}"
        unit_str = f" {ev.unit}" if ev.unit else ""
        click.echo(f"{ts}  {entity_str}  {ev.attribute}={ev.value}{unit_str}")


# ---------------------------------------------------------------------------
# plugin group
# ---------------------------------------------------------------------------


@cli.group()
def plugin() -> None:
    """Inspect connected plugins."""


@plugin.command("list")
def plugin_list() -> None:
    """List plugins that have contributed events to the World Model."""

    async def _list() -> None:
        """Aggregate distinct plugin IDs from entity source_plugins."""
        store = _open_store()
        await store.connect()
        try:
            snapshot = await store.snapshot()
        finally:
            await store.close()

        plugin_ids: set[str] = set()
        for entity in snapshot.entities:
            plugin_ids.update(entity.source_plugins)

        if not plugin_ids:
            click.echo("No plugins have emitted events yet.")
            return

        click.echo(click.style(f"{'PLUGIN ID':<40}  ENTITIES CONTRIBUTED", fg="cyan"))
        for pid in sorted(plugin_ids):
            entity_count = sum(1 for e in snapshot.entities if pid in e.source_plugins)
            click.echo(f"{pid:<40}  {entity_count}")

    asyncio.run(_list())


@plugin.command("info")
@click.argument("plugin_id")
def plugin_info(plugin_id: str) -> None:
    """Show details for PLUGIN_ID: entities, event count, and attributes."""

    async def _info() -> None:
        """Query entities and recent events for the given plugin."""
        store = _open_store()
        await store.connect()
        try:
            snapshot = await store.snapshot()
            since = datetime.now(UTC) - timedelta(hours=24)
            recent = await store.recent_events(since=since, limit=5000)
        finally:
            await store.close()

        contributed = [e for e in snapshot.entities if plugin_id in e.source_plugins]
        plugin_events = [ev for ev in recent if ev.source == plugin_id]
        attributes = sorted({ev.attribute for ev in plugin_events})

        if not contributed and not plugin_events:
            click.echo(f"Plugin {plugin_id!r} not found in World Model.", err=True)
            sys.exit(1)

        click.echo(click.style(f"Plugin: {plugin_id}", fg="cyan", bold=True))
        click.echo(f"  Entities contributed: {len(contributed)}")
        click.echo(f"  Events (last 24h):    {len(plugin_events)}")
        attrs_str = ", ".join(attributes) if attributes else "(none)"
        click.echo(f"  Attributes emitted:   {attrs_str}")
        if contributed:
            click.echo("  Entities:")
            for ent in contributed:
                click.echo(f"    {ent.type}:{ent.display_name}")

    asyncio.run(_info())


# ---------------------------------------------------------------------------
# world group
# ---------------------------------------------------------------------------


@cli.group()
def world() -> None:
    """Inspect the World Model graph."""


@world.command("snapshot")
def world_snapshot() -> None:
    """Dump the current World Model graph to stdout as JSON."""

    async def _snapshot() -> None:
        """Fetch and serialise the world snapshot."""
        store = _open_store()
        await store.connect()
        try:
            snap = await store.snapshot()
        finally:
            await store.close()

        doc: dict[str, Any] = {
            "taken_at": snap.taken_at.isoformat(),
            "entities": [
                {
                    "type": e.type,
                    "display_name": e.display_name,
                    "created_at": e.created_at.isoformat(),
                    "updated_at": e.updated_at.isoformat(),
                    "properties": e.properties,
                    "source_plugins": e.source_plugins,
                }
                for e in snap.entities
            ],
            "relationships": [
                {
                    "type": r.type,
                    "from": {"type": r.from_entity.type, "id": r.from_entity.id},
                    "to": {"type": r.to_entity.type, "id": r.to_entity.id},
                    "weight": r.weight,
                    "created_at": r.created_at.isoformat(),
                    "last_reinforced": r.last_reinforced.isoformat(),
                }
                for r in snap.relationships
            ],
            "recent_events": [
                {
                    "id": ev.id,
                    "timestamp": ev.timestamp.isoformat(),
                    "source": ev.source,
                    "entity": {"type": ev.entity.type, "id": ev.entity.id},
                    "attribute": ev.attribute,
                    "value": ev.value,
                    "confidence": ev.confidence,
                    "unit": ev.unit,
                }
                for ev in snap.recent_events
            ],
        }
        click.echo(json.dumps(doc, indent=2))

    asyncio.run(_snapshot())


# ---------------------------------------------------------------------------
# memory group
# ---------------------------------------------------------------------------


_SEMANTIC_JOURNAL: Path = Path.home() / ".coremind" / "audit.log"
_REASONING_JOURNAL: Path = Path.home() / ".coremind" / "reasoning.log"


def _build_semantic_memory() -> object:
    """Construct a ``SemanticMemory`` wired to Qdrant + configured embedder.

    Imports are local so the CLI starts fast even when optional deps are
    unavailable; an actionable error is raised if the caller invokes a
    command that needs semantic memory without the dependencies installed.

    Returns:
        A ready-to-use :class:`coremind.memory.semantic.SemanticMemory`
        instance.  The caller must ``await .initialise()`` before use.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415 — optional heavy dep

    from coremind.memory.embeddings import EmbedderConfig, build_embedder  # noqa: PLC0415

    _ = AsyncQdrantClient  # imported to fail-fast if missing
    embedder = build_embedder(EmbedderConfig())
    # The CLI uses an in-process hash embedder by default for now because
    # the Qdrant adapter is introduced alongside its own CLI wiring.
    # Phase 2.5 note: this is intentionally a stub for read-only CLI usage.
    del embedder
    raise click.ClickException(
        "Semantic memory CLI wiring is not yet connected to Qdrant. "
        "Phase 2 ships the memory modules and schemas; a Qdrant adapter is "
        "implemented but CLI access will be enabled once a deployment config "
        "path is agreed on."
    )


@cli.group()
def memory() -> None:
    """Query and manage semantic memory (L3)."""


@memory.command("search")
@click.argument("query", required=True)
@click.option("-k", "top_k", default=10, show_default=True, help="Number of results.")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Restrict to memories carrying every listed tag (repeatable).",
)
def memory_search(query: str, top_k: int, tags: tuple[str, ...]) -> None:
    """Search semantic memory for text similar to QUERY."""
    _ = (query, top_k, tags)
    _build_semantic_memory()  # raises ClickException with guidance


@memory.command("forget")
@click.argument("memory_id", required=True)
@click.option(
    "--reason",
    required=True,
    help="Human-readable reason for deletion (persisted to audit log).",
)
def memory_forget(memory_id: str, reason: str) -> None:
    """Remove MEMORY_ID from semantic memory and log the deletion."""
    _ = (memory_id, reason)
    _build_semantic_memory()


@memory.group("tags")
def memory_tags() -> None:
    """Inspect memory tags."""


@memory_tags.command("list")
def memory_tags_list() -> None:
    """List all tags used in semantic memory."""
    _build_semantic_memory()


# ---------------------------------------------------------------------------
# reason group
# ---------------------------------------------------------------------------


def _open_cycle_persister() -> object:
    """Return a :class:`JsonlCyclePersister` pointed at the default journal.

    Returns:
        A ready-to-use cycle persister.
    """
    from coremind.reasoning.persistence import JsonlCyclePersister  # noqa: PLC0415

    return JsonlCyclePersister(_REASONING_JOURNAL)


@cli.group()
def reason() -> None:
    """Inspect and trigger reasoning cycles (L4)."""


@reason.command("list")
@click.option(
    "--last",
    "last_duration",
    default="24h",
    show_default=True,
    help="How far back to look (e.g. 1h, 24h, 7d).",
)
@click.option("--limit", default=50, show_default=True, help="Maximum cycles to return.")
def reason_list(last_duration: str, limit: int) -> None:
    """List recent reasoning cycles."""
    from coremind.reasoning.persistence import JsonlCyclePersister  # noqa: PLC0415

    duration = _parse_duration(last_duration)
    persister = JsonlCyclePersister(_REASONING_JOURNAL)

    async def _run() -> list[Any]:
        """Load cycles from the journal."""
        since = datetime.now(UTC) - duration
        return await persister.list_cycles(since=since, limit=limit)

    cycles = asyncio.run(_run())
    if not cycles:
        click.echo("No reasoning cycles in the requested window.")
        return

    click.echo(
        click.style(
            f"{'TIMESTAMP':<26}  {'CYCLE_ID':<34}  PATTERNS  ANOMALIES  PREDICTIONS",
            fg="cyan",
        )
    )
    for c in cycles:
        click.echo(
            f"{c.timestamp.isoformat():<26}  {c.cycle_id:<34}  "
            f"{len(c.patterns):<8}  {len(c.anomalies):<9}  {len(c.predictions)}"
        )


@reason.command("show")
@click.argument("cycle_id", required=True)
def reason_show(cycle_id: str) -> None:
    """Show the full detail of a reasoning cycle."""
    from coremind.reasoning.persistence import JsonlCyclePersister  # noqa: PLC0415

    persister = JsonlCyclePersister(_REASONING_JOURNAL)

    async def _run() -> Any:
        """Fetch the cycle by id."""
        return await persister.get_cycle(cycle_id)

    cycle = asyncio.run(_run())
    if cycle is None:
        click.echo(f"No cycle with id {cycle_id!r}.", err=True)
        sys.exit(1)
    click.echo(cycle.model_dump_json(indent=2))


@reason.command("now")
def reason_now() -> None:
    """Trigger a reasoning cycle immediately.

    Phase 2 note: this command requires the full stack (SurrealDB, Qdrant,
    an LLM provider).  Use ``reason list`` / ``reason show`` to inspect
    cycles produced by the running daemon.
    """
    raise click.ClickException(
        "`reason now` requires live SurrealDB, Qdrant, and an LLM provider. "
        "Start the daemon with `coremind daemon start` — cycles run automatically "
        "on the configured schedule."
    )


# ---------------------------------------------------------------------------
# Phase 3 helpers — intent / action / approvals / audit / notify / quiet-hours
# ---------------------------------------------------------------------------


def _open_intent_store(config: DaemonConfig | None = None) -> IntentStore:
    """Build an :class:`IntentStore` pointed at the configured JSONL path."""
    cfg = config or load_config()
    return IntentStore(cfg.intent_store_path)


def _cli_responder_id() -> str:
    """Return the audit identity for an approval decided through the CLI.

    Format is ``"cli:<username>"``.  Falls back to ``"cli:unknown"`` when
    the OS does not expose a user (rare — e.g. detached service contexts).
    """
    try:
        return f"cli:{getpass.getuser()}"
    except OSError:
        return "cli:unknown"


def _open_journal(config: DaemonConfig | None = None) -> ActionJournal:
    """Build a loaded :class:`ActionJournal` for CLI read/write access.

    Uses the daemon keypair (created on demand) for signing CLI-originated
    meta-events such as approval decisions.
    """
    cfg = config or load_config()
    private = ensure_daemon_keypair()
    public = private.public_key()
    return ActionJournal(cfg.audit_log_path, private, public)


def _format_intent_row(intent: Intent) -> str:
    """Render a single-line representation of an intent for ``list`` views."""
    op = intent.proposed_action.operation if intent.proposed_action else "(no action)"
    return (
        f"{intent.created_at.isoformat():<26}  {intent.id:<28}  "
        f"{intent.status:<17}  {intent.category:<7}  "
        f"{intent.salience:.2f}  {intent.confidence:.2f}  {op}"
    )


def _print_intent_table(intents: list[Intent]) -> None:
    """Print a table of intents to stdout."""
    if not intents:
        click.echo("No intents matched.")
        return
    click.echo(
        click.style(
            f"{'CREATED_AT':<26}  {'ID':<28}  {'STATUS':<17}  {'CAT':<7}  SAL   CONF  OPERATION",
            fg="cyan",
        )
    )
    for intent in intents:
        click.echo(_format_intent_row(intent))


def _load_intent_or_exit(intent_id: str) -> Intent:
    """Load an intent from the store or exit with a CLI error."""
    store = _open_intent_store()

    async def _run() -> Intent | None:
        """Fetch the intent asynchronously."""
        return await store.get(intent_id)

    intent = asyncio.run(_run())
    if intent is None:
        raise click.ClickException(f"unknown intent {intent_id!r}")
    return intent


def _apply_decision(
    intent_id: str,
    *,
    decision: str,
    note: str | None,
    snooze_seconds: int | None,
) -> Intent:
    """Mutate an intent in-place according to ``decision`` and journal it.

    Args:
        intent_id: Target intent id.
        decision: One of ``approve`` / ``reject`` / ``snooze``.
        note: Optional human-readable note.
        snooze_seconds: Snooze duration, required when ``decision="snooze"``.

    Returns:
        The updated intent.

    Raises:
        click.ClickException: If the intent does not exist or is not pending.
    """
    config = load_config()
    store = _open_intent_store(config)
    journal = _open_journal(config)

    async def _run() -> Intent:
        """Execute the decision against the live stores."""
        await journal.load()
        intent = await store.get(intent_id)
        if intent is None:
            raise click.ClickException(f"unknown intent {intent_id!r}")
        if intent.status not in {"pending", "pending_approval"}:
            raise click.ClickException(
                f"intent {intent_id!r} is not awaiting a decision (status={intent.status!r})"
            )
        now = datetime.now(UTC)
        if decision == "approve":
            intent.status = "approved"
        elif decision == "reject":
            intent.status = "rejected"
        elif decision == "snooze":
            if intent.snooze_count >= 1:
                raise click.ClickException(f"intent {intent_id!r} has already been snoozed once")
            if snooze_seconds is None:  # pragma: no cover — caller validates
                raise click.ClickException("snooze duration required")
            intent.snooze_count += 1
            intent.status = "pending_approval"
            intent.expires_at = now + timedelta(seconds=snooze_seconds)
        else:  # pragma: no cover — caller validates
            raise click.ClickException(f"unknown decision {decision!r}")
        if note is not None:
            intent.human_feedback = note
        await store.save(intent)

        meta_payload: dict[str, Any] = {
            "intent_id": intent.id,
            "decision": decision,
            "responder": _cli_responder_id(),
            "note": note or "",
        }
        if decision == "snooze" and snooze_seconds is not None:
            meta_payload["snooze_seconds"] = snooze_seconds
        try:
            await journal.append_meta("approval.response", meta_payload)
        except JournalError as exc:  # pragma: no cover — journal write rarely fails
            raise click.ClickException(f"journal write failed: {exc}") from exc
        return intent

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# intent group
# ---------------------------------------------------------------------------


@cli.group()
def intent() -> None:
    """Inspect and decide on intents (L5)."""


@intent.command("list")
@click.option(
    "--status",
    "status",
    type=click.Choice(
        [
            "pending",
            "pending_approval",
            "approved",
            "rejected",
            "snoozed",
            "executing",
            "done",
            "failed",
            "expired",
        ]
    ),
    default=None,
    help="Filter by intent status.",
)
@click.option("--limit", default=100, show_default=True, help="Maximum intents to return.")
def intent_list(status: str | None, limit: int) -> None:
    """List intents, newest first."""
    store = _open_intent_store()

    async def _run() -> list[Intent]:
        """Fetch filtered intents from the store."""
        return await store.list(status=status, limit=limit)  # type: ignore[arg-type]

    _print_intent_table(asyncio.run(_run()))


@intent.command("show")
@click.argument("intent_id", required=True)
def intent_show(intent_id: str) -> None:
    """Show the full detail of an intent."""
    i = _load_intent_or_exit(intent_id)
    click.echo(i.model_dump_json(indent=2))


@intent.command("approve")
@click.argument("intent_id", required=True)
@click.option("--note", default=None, help="Optional note to attach to the decision.")
def intent_approve(intent_id: str, note: str | None) -> None:
    """Approve a pending intent.

    The daemon's approved-intent dispatcher will pick it up and run it
    through the executor on its next tick (typically within seconds).  If
    the daemon is not running, the intent stays ``approved`` until it is.
    """
    _apply_decision(intent_id, decision="approve", note=note, snooze_seconds=None)
    click.echo(click.style(f"✓ approved {intent_id}", fg="green"))


@intent.command("reject")
@click.argument("intent_id", required=True)
@click.option("--note", default=None, help="Optional note to attach to the decision.")
def intent_reject(intent_id: str, note: str | None) -> None:
    """Reject a pending intent."""
    _apply_decision(intent_id, decision="reject", note=note, snooze_seconds=None)
    click.echo(click.style(f"✓ rejected {intent_id}", fg="yellow"))


@intent.command("snooze")
@click.argument("intent_id", required=True)
@click.option(
    "--for",
    "duration",
    required=True,
    help="Snooze duration (e.g. 30m, 1h, 2d).",
)
@click.option("--note", default=None, help="Optional note to attach to the decision.")
def intent_snooze(intent_id: str, duration: str, note: str | None) -> None:
    """Snooze a pending intent for the given duration (once only)."""
    secs = int(_parse_duration(duration).total_seconds())
    if secs < 1:
        raise click.BadParameter("snooze duration must be at least 1 second")
    _apply_decision(intent_id, decision="snooze", note=note, snooze_seconds=secs)
    click.echo(click.style(f"✓ snoozed {intent_id} for {duration}", fg="blue"))


# ---------------------------------------------------------------------------
# approvals group — aliases over intent
# ---------------------------------------------------------------------------


@cli.group()
def approvals() -> None:
    """Decide on approval-gated intents (aliases for ``coremind intent *``)."""


@approvals.command("pending")
def approvals_pending() -> None:
    """List intents currently awaiting approval."""
    store = _open_intent_store()

    async def _run() -> list[Intent]:
        """Fetch pending-approval intents."""
        return await store.list(status="pending_approval")

    _print_intent_table(asyncio.run(_run()))


@approvals.command("approve")
@click.argument("intent_id", required=True)
@click.option("--note", default=None, help="Optional note.")
def approvals_approve(intent_id: str, note: str | None) -> None:
    """Approve a pending-approval intent."""
    _apply_decision(intent_id, decision="approve", note=note, snooze_seconds=None)
    click.echo(click.style(f"✓ approved {intent_id}", fg="green"))


@approvals.command("deny")
@click.argument("intent_id", required=True)
@click.option("--note", default=None, help="Optional note.")
def approvals_deny(intent_id: str, note: str | None) -> None:
    """Deny a pending-approval intent."""
    _apply_decision(intent_id, decision="reject", note=note, snooze_seconds=None)
    click.echo(click.style(f"✓ denied {intent_id}", fg="yellow"))


@approvals.command("snooze")
@click.argument("intent_id", required=True)
@click.option(
    "--for",
    "duration",
    required=True,
    help="Snooze duration (e.g. 30m, 1h).",
)
@click.option("--note", default=None, help="Optional note.")
def approvals_snooze(intent_id: str, duration: str, note: str | None) -> None:
    """Snooze a pending-approval intent."""
    secs = int(_parse_duration(duration).total_seconds())
    if secs < 1:
        raise click.BadParameter("snooze duration must be at least 1 second")
    _apply_decision(intent_id, decision="snooze", note=note, snooze_seconds=secs)
    click.echo(click.style(f"✓ snoozed {intent_id} for {duration}", fg="blue"))


# ---------------------------------------------------------------------------
# action group
# ---------------------------------------------------------------------------


def _format_action_row(action: Action) -> str:
    """Render a single-line representation of an action for ``list`` views."""
    status = action.result.status if action.result is not None else "pending"
    return (
        f"{action.timestamp.isoformat():<26}  {action.id:<28}  "
        f"{status:<20}  {action.category:<7}  {action.operation}"
    )


@cli.group()
def action() -> None:
    """Inspect and reverse actions (L6)."""


@action.command("list")
@click.option(
    "--last",
    "last_duration",
    default="24h",
    show_default=True,
    help="How far back to look (e.g. 1h, 24h, 7d).",
)
@click.option("--limit", default=100, show_default=True, help="Maximum actions to return.")
def action_list(last_duration: str, limit: int) -> None:
    """List actions from the audit journal."""
    duration = _parse_duration(last_duration)
    since = datetime.now(UTC) - duration
    journal = _open_journal()

    async def _run() -> list[Action]:
        """Walk the journal and return recent actions."""
        await journal.load()
        entries = await journal.read_all()
        actions: list[Action] = []
        for entry in reversed(entries):
            if entry.kind != "action":
                continue
            if entry.timestamp < since:
                break
            actions.append(Action.model_validate(entry.payload))
            if len(actions) >= limit:
                break
        return actions

    actions = asyncio.run(_run())
    if not actions:
        click.echo("No actions in the requested window.")
        return
    click.echo(
        click.style(
            f"{'TIMESTAMP':<26}  {'ID':<28}  {'STATUS':<20}  {'CAT':<7}  OPERATION",
            fg="cyan",
        )
    )
    for a in actions:
        click.echo(_format_action_row(a))


@action.command("show")
@click.argument("action_id", required=True)
def action_show(action_id: str) -> None:
    """Show the full detail of an action from the journal."""
    journal = _open_journal()

    async def _run() -> Action | None:
        """Fetch the action from the journal."""
        await journal.load()
        return await journal.find_action(action_id)

    a = asyncio.run(_run())
    if a is None:
        raise click.ClickException(f"unknown action {action_id!r}")
    click.echo(a.model_dump_json(indent=2))


@action.command("reverse")
@click.argument("action_id", required=True)
def action_reverse(action_id: str) -> None:
    """Dispatch the declared reversal for a completed action.

    Phase 3 limitation: reversal requires effector plugins served by the
    running daemon.  Invoke this command while ``coremind daemon start``
    is active; otherwise no effector can be resolved.
    """
    # Ensure the action exists so we fail fast with a clear message.
    journal = _open_journal()

    async def _fetch() -> Action | None:
        """Load the action from the journal."""
        await journal.load()
        return await journal.find_action(action_id)

    original = asyncio.run(_fetch())
    if original is None:
        raise click.ClickException(f"unknown action {action_id!r}")
    if original.result is None or original.result.reversed_by_operation is None:
        raise click.ClickException(f"action {action_id!r} declared no reversal; cannot reverse")
    raise click.ClickException(
        "Effector-backed reversal requires the daemon to be running so plugin "
        "effectors are reachable.  Start it with `coremind daemon start` and "
        "re-run this command; direct CLI reversal will land in Phase 3.5 with "
        "the plugin-host action dispatcher."
    )


# ---------------------------------------------------------------------------
# audit group
# ---------------------------------------------------------------------------


@cli.group()
def audit() -> None:
    """Inspect the hash-chained action/audit journal."""


@audit.command("verify")
def audit_verify() -> None:
    """Walk the full journal and report any integrity break."""
    journal = _open_journal()

    async def _run() -> VerifyReport:
        """Load and verify the journal end-to-end."""
        await journal.load()
        return await journal.verify()

    try:
        report = asyncio.run(_run())
    except (ActionError, JournalError) as exc:
        raise click.ClickException(f"audit verify failed: {exc}") from exc

    if report.ok:
        click.echo(click.style(f"✓ journal ok — {report.entries} entries", fg="green"))
        return
    click.echo(
        click.style(
            f"✗ journal broken at line {report.broken_at}: {report.reason}",
            fg="red",
        ),
        err=True,
    )
    sys.exit(1)


@audit.command("tail")
@click.option("--limit", default=20, show_default=True, help="Number of trailing entries.")
def audit_tail(limit: int) -> None:
    """Print the trailing entries of the audit journal."""
    journal = _open_journal()

    async def _run() -> list[Any]:
        """Load and return trailing journal entries."""
        await journal.load()
        entries = await journal.read_all()
        return entries[-limit:]

    entries = asyncio.run(_run())
    if not entries:
        click.echo("Journal is empty.")
        return
    for entry in entries:
        click.echo(
            f"{entry.seq:>6}  {entry.timestamp.isoformat():<26}  "
            f"{entry.kind:<6}  {json.dumps(entry.payload, sort_keys=True)}"
        )


# ---------------------------------------------------------------------------
# notify group
# ---------------------------------------------------------------------------


@cli.group()
def notify() -> None:
    """Inspect and test notification routing."""


@notify.command("test")
@click.option(
    "--port",
    "port_id",
    default=None,
    help="Port id to test (defaults to the configured primary).",
)
def notify_test(port_id: str | None) -> None:
    """Send a test notification through the configured port(s).

    Phase 3 only implements the ``dashboard`` port natively in-process.
    Other ports (Telegram, …) require the running daemon which owns their
    credentials and callback subscriptions.
    """
    from coremind.notify.adapters.dashboard import DashboardNotificationPort  # noqa: PLC0415

    config = load_config()
    target = port_id or config.notify.primary
    if target != "dashboard":
        raise click.ClickException(
            f"CLI-side notify test only supports the `dashboard` port (got {target!r}). "
            "For Telegram and other live ports, use the running daemon."
        )
    port = DashboardNotificationPort()

    async def _run() -> None:
        """Deliver a synthetic ``info`` notification through the dashboard port."""
        await port.notify(
            message="CoreMind CLI test notification.",
            category="info",
            actions=None,
            intent_id=None,
        )

    asyncio.run(_run())
    click.echo(click.style(f"✓ test notification sent via {target}", fg="green"))


@notify.command("status")
def notify_status() -> None:
    """Report the configured primary port, fallbacks, and Telegram enablement."""
    config = load_config()
    click.echo(click.style("Notification routing", fg="cyan", bold=True))
    click.echo(f"  primary:       {config.notify.primary}")
    fallbacks = ", ".join(config.notify.fallbacks) if config.notify.fallbacks else "(none)"
    click.echo(f"  fallbacks:     {fallbacks}")
    tg = config.notify.telegram
    tg_state = "enabled" if tg.enabled else "disabled"
    click.echo(f"  telegram:      {tg_state}  (chat_id={tg.chat_id or '<unset>'})")


# ---------------------------------------------------------------------------
# quiet-hours group
# ---------------------------------------------------------------------------


@cli.group("quiet-hours")
def quiet_hours() -> None:
    """Inspect the quiet-hours policy."""


@quiet_hours.command("show")
def quiet_hours_show() -> None:
    """Print the current quiet-hours configuration."""
    config = load_config()
    qh = config.quiet_hours
    state = "enabled" if qh.enabled else "disabled"
    click.echo(click.style("Quiet hours", fg="cyan", bold=True))
    click.echo(f"  state:    {state}")
    click.echo(f"  timezone: {qh.timezone}")
    click.echo(f"  start:    {qh.quiet_start.isoformat()}")
    click.echo(f"  end:      {qh.quiet_end.isoformat()}")


# ---------------------------------------------------------------------------
# reflect group
# ---------------------------------------------------------------------------


@cli.group()
def reflect() -> None:
    """Run the L7 reflection cycle (weekly self-evaluation)."""


@reflect.command("now")
@click.option(
    "--window-days",
    default=7,
    show_default=True,
    help="Reflection window width in days.",
)
def reflect_now(window_days: int) -> None:
    """Run a reflection cycle immediately and print the full Markdown report.

    Wires all 8 reflection loop ports against the daemon's live data sources
    (JSONL intent store, JSONL reasoning cycles, hash-chained audit journal)
    and runs one complete L7 cycle.
    """
    from datetime import datetime, timedelta, UTC

    from coremind.config import load_config as _load_config

    config = _load_config()
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=window_days)

    click.echo(
        click.style(
            f"Running reflection cycle: {window_start.strftime('%Y-%m-%d')} "
            f"\u2192 {window_end.strftime('%Y-%m-%d')} ({window_days}d window)...",
            fg="cyan",
        )
    )

    async def _run() -> None:
        from coremind.reflection.loop import ReflectionLoop, ReflectionLoopConfig
        from coremind.reflection.evaluator import (
            PredictionEvaluatorImpl,
            InMemoryPredictionEvaluationStore,
        )
        from coremind.reflection.calibration import Calibrator, InMemoryCalibrationStore
        from coremind.reflection.rule_learner import (
            RuleLearnerImpl,
            InMemoryCandidateLedger,
            InMemoryRuleProposalStore,
        )
        from coremind.reflection.report import MarkdownReportProducer
        from coremind.reflection.schemas import FeedbackEvaluationResult
        from coremind.reasoning.persistence import JsonlCyclePersister
        from coremind.cli.reflect_ports import (
            CliCycleSource,
            CliIntentSource,
            CliActionFeed,
        )

        # -- 1. CycleSource ------------------------------------------------
        reasoning_path = Path.home() / ".coremind" / "reasoning.log"
        persister = JsonlCyclePersister(reasoning_path)
        cycle_source = CliCycleSource(persister)

        # -- 2. IntentSource -----------------------------------------------
        intent_store = IntentStore(config.intent_store_path)
        intent_source = CliIntentSource(intent_store)

        # -- 3. ActionFeed ------------------------------------------------
        from coremind.crypto.signatures import ensure_daemon_keypair

        private = ensure_daemon_keypair()
        public = private.public_key()
        journal = ActionJournal(config.audit_log_path, private, public)
        await journal.load()
        action_feed = CliActionFeed(journal)

        # -- 4. PredictionEvaluator ----------------------------------------
        from coremind.world.store import WorldStore
        from coremind.reflection.evaluator import EventHistorySource, ConditionResolver

        class CliEventHistorySource:
            """EventHistorySource that tries SurrealDB and falls back gracefully."""
            def __init__(self) -> None:
                self._store: WorldStore | None = None

            async def events_in_window(
                self,
                after: datetime,
                before: datetime,
                limit: int = 1000,
            ) -> list:
                if self._store is None:
                    try:
                        store = WorldStore(
                            url=config.world_db_url,
                            username=config.world_db_username,
                            password=config.world_db_password,
                            key_resolver=lambda _: None,
                        )
                        await store.connect()
                        self._store = store
                    except Exception:
                        log.warning("reflect.surrealdb_unavailable", url=config.world_db_url)
                        return []
                try:
                    return await self._store.events_in_window(
                        after=after, before=before, limit=limit
                    )
                except Exception:
                    log.warning("reflect.evidence_query_failed")
                    return []

        class BasicConditionResolver:
            """ConditionResolver that marks everything as undetermined
            when no SurrealDB evidence is available."""
            async def resolve(
                self,
                prediction: object,
                evidence: list,
            ) -> tuple[str, str]:
                if not evidence:
                    return ("undetermined", "no world evidence available (SurrealDB offline)")
                return ("undetermined", f"{len(evidence)} evidence events; basic resolver")

        eval_store = InMemoryPredictionEvaluationStore()
        history: EventHistorySource = CliEventHistorySource()
        resolver: ConditionResolver = BasicConditionResolver()
        prediction_evaluator = PredictionEvaluatorImpl(
            history=history,
            resolver=resolver,
            store=eval_store,
        )

        # -- 5. FeedbackEvaluator ----------------------------------------
        class CliFeedbackEvaluator:
            """Evaluates actions against user feedback from intents."""
            async def evaluate(
                self,
                actions: list,
                intents: list,
            ) -> FeedbackEvaluationResult:
                approved = sum(1 for i in intents if i.status == "approved")
                rejected = sum(
                    1 for i in intents
                    if i.status in ("rejected", "auto_dismissed")
                )
                dismissed = sum(1 for i in intents if i.status == "expired")
                reversed_count = sum(
                    1 for a in actions
                    if a.result is not None and a.result.reversed_by_operation is not None
                )
                return FeedbackEvaluationResult(
                    evaluated=len(actions),
                    approved=approved,
                    rejected=rejected,
                    reversed=reversed_count,
                    dismissed=dismissed,
                )

        feedback_evaluator: object = CliFeedbackEvaluator()

        # -- 6. CalibrationUpdater ----------------------------------------
        cal_store = InMemoryCalibrationStore()
        calibration_updater = Calibrator(
            eval_store=eval_store,
            cal_store=cal_store,
            layer="reasoning",
        )

        # -- 7. RuleLearner ------------------------------------------------
        class EmptyRuleSource:
            """RuleSource returning no active rules (CLI has no procedural memory)."""
            async def list_active_rules(self):
                return []

        rule_learner = RuleLearnerImpl(
            rule_source=EmptyRuleSource(),
            ledger=InMemoryCandidateLedger(),
            proposal_store=InMemoryRuleProposalStore(),
        )

        # -- 8. ReportProducer --------------------------------------------
        report_producer = MarkdownReportProducer(
            proposal_store=InMemoryRuleProposalStore(),
        )

        # -- Assemble and run ---------------------------------------------
        loop = ReflectionLoop(
            cycle_source=cycle_source,
            intent_source=intent_source,
            action_feed=action_feed,
            prediction_evaluator=prediction_evaluator,
            feedback_evaluator=feedback_evaluator,
            calibration_updater=calibration_updater,
            rule_learner=rule_learner,
            report_producer=report_producer,
            notifier=None,
            config=ReflectionLoopConfig(
                window_days=window_days,
                notify_on_cycle=False,
            ),
        )

        report = await loop.run_cycle()
        click.echo()
        click.echo(report.markdown)

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(click.style(f"Reflection failed: {exc}", fg="red"), err=True)
        import traceback
        click.echo(traceback.format_exc(), err=True)
