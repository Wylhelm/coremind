"""Dashboard wiring helpers for the CoreMind daemon.

Extracted from :mod:`coremind.core.daemon` to keep the orchestrator lean.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from coremind.action.journal import ActionJournal
from coremind.config import DashboardConfig
from coremind.core.event_bus import EventBus
from coremind.dashboard import (
    DashboardAuth,
    DashboardDataSources,
    DashboardServer,
)
from coremind.intention.persistence import IntentStore
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import UserRef
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)

# Directory holding per-secret files (chmod 600).  Mirrors the convention
# already used for the daemon keypair under ``~/.coremind/``.
_SECRETS_DIR = Path.home() / ".coremind" / "secrets"

# Minimum length required for the dashboard's bearer token.  The
# :class:`DashboardAuth` model enforces the same lower bound; we mirror it
# here so the daemon refuses to start the dashboard with an obviously
# under-strength token rather than failing at request time.
_MIN_DASHBOARD_TOKEN_LENGTH = 16


def resolve_dashboard_secret(name: str) -> str | None:
    """Return the dashboard's bearer token, or ``None`` if not configured.

    Resolution order:

    1. ``COREMIND_DASHBOARD_API_TOKEN`` environment variable — operator-friendly
       for development; never written to disk.
    2. ``~/.coremind/secrets/<name>`` — the canonical, persistent location.
       The file is read in text mode and stripped; surrounding whitespace
       is tolerated.

    Args:
        name: The secret identifier (typically ``"dashboard_api_token"``).
    """
    env_value = os.environ.get("COREMIND_DASHBOARD_API_TOKEN")
    if env_value:
        return env_value.strip()
    path = _SECRETS_DIR / name
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        log.warning("dashboard.secret_read_failed", path=str(path), error=str(exc))
        return None


def build_dashboard_auth(config: DashboardConfig) -> DashboardAuth | None:
    """Construct a :class:`DashboardAuth` from config, or ``None`` if absent.

    A missing or under-length token disables approval submissions
    (the dashboard remains read-only).  The function logs a structured
    warning so operators can spot a misconfiguration in the journal.
    """
    token = resolve_dashboard_secret(config.api_token_secret)
    if not token or len(token) < _MIN_DASHBOARD_TOKEN_LENGTH:
        log.warning(
            "dashboard.auth_disabled",
            reason="missing_or_short_token",
            secret_name=config.api_token_secret,
        )
        return None
    # Default the allowed-origins list to the bind address when the
    # operator hasn't customised it; that matches the most common
    # deployment (browser hits ``http://127.0.0.1:9900`` directly).
    origins = config.allowed_origins or (f"http://{config.host}:{config.port}",)
    return DashboardAuth(
        api_token=token,
        operator=UserRef(
            id=config.operator_id,
            display_name=config.operator_display_name,
        ),
        allowed_origins=origins,
    )


async def start_dashboard(
    *,
    config: DashboardConfig,
    world_store: WorldStore,
    intents: IntentStore,
    journal: ActionJournal,
    dashboard_port: DashboardNotificationPort,
    event_bus: EventBus,
    reasoning_log: Path,
    reflection: object | None = None,
    meta_source: object | None = None,
) -> DashboardServer:
    """Construct and start the read-only web dashboard.

    Args:
        config: Validated dashboard configuration.
        world_store: World Model store; surfaces entities, relationships,
            and recent events.
        intents: Intent store; surfaces the pending/queued intents.
        journal: Action journal; surfaces audit entries via ``read_recent``.
        dashboard_port: Shared :class:`DashboardNotificationPort` instance —
            the notification router writes to it, the dashboard reads
            ``pending()`` from it.  Sharing one instance is what keeps the
            UI's pending-approval list in sync with reality.
        event_bus: In-process :class:`EventBus`; powers the SSE live tail.
        reasoning_log: Path to the JSONL reasoning-cycle log; surfaces the
            ``/reasoning`` page.
        reflection: Optional report store implementing
            ``list_reports(*, limit) -> list[StoredReflectionReport]``.
            When ``None`` the ``/reflection`` page renders an empty state.

    Returns:
        A started :class:`DashboardServer`.  Stopping is the caller's job.
    """
    sources = DashboardDataSources(
        world=world_store,
        cycles=JsonlCyclePersister(reasoning_log),
        intents=intents,
        journal=journal,
        reflection=reflection,  # type: ignore[arg-type]
        notifications=dashboard_port,
        events=event_bus,
        meta=meta_source,  # type: ignore[arg-type]
    )
    auth = build_dashboard_auth(config)
    server = DashboardServer(
        sources,
        auth=auth,
        host=config.host,
        port=config.port,
    )
    await server.start()
    log.info(
        "daemon.dashboard_started",
        host=config.host,
        port=config.port,
        auth_enabled=auth is not None,
    )
    return server
