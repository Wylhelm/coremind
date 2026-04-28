"""Dashboard HTTP server lifecycle.

The dashboard binds to ``127.0.0.1:9900`` by default — explicitly loopback
so the daemon never accidentally exposes private state on a public NIC.
``DashboardServer.start`` returns once the server is listening and
``stop`` performs a graceful shutdown.
"""

from __future__ import annotations

import structlog
from aiohttp import web

from coremind.dashboard.auth import DashboardAuth
from coremind.dashboard.data import DashboardDataSources
from coremind.dashboard.views import (
    AUTH_KEY,
    DATA_SOURCES_KEY,
    actions_page,
    events_page,
    events_stream,
    graph_json,
    graph_page,
    intents_page,
    overview,
    reasoning_page,
    reflection_page,
    security_headers_middleware,
    submit_approval,
)

log = structlog.get_logger(__name__)


DASHBOARD_DEFAULT_HOST: str = "127.0.0.1"
DASHBOARD_DEFAULT_PORT: int = 9900


def create_app(
    data_sources: DashboardDataSources,
    *,
    auth: DashboardAuth | None = None,
) -> web.Application:
    """Construct the dashboard :class:`aiohttp.web.Application`.

    Args:
        data_sources: Read ports the handlers consume.  Any field on
            :class:`DashboardDataSources` may be ``None`` — the relevant
            page will simply render an empty state.
        auth: Authentication policy for the ``/api/approvals`` endpoint.
            When ``None``, approval submissions are rejected with a 503
            (fail-closed) — the dashboard refuses to authorize a
            state-changing call without an explicit operator identity.

    Returns:
        A fully wired aiohttp application.  Caller is responsible for
        running it (see :class:`DashboardServer`).
    """
    app = web.Application(middlewares=[security_headers_middleware])
    app[DATA_SOURCES_KEY] = data_sources
    app[AUTH_KEY] = auth
    app.router.add_get("/", overview)
    app.router.add_get("/events", events_page)
    app.router.add_get("/api/events/stream", events_stream)
    app.router.add_get("/graph", graph_page)
    app.router.add_get("/api/graph", graph_json)
    app.router.add_get("/reasoning", reasoning_page)
    app.router.add_get("/intents", intents_page)
    app.router.add_get("/actions", actions_page)
    app.router.add_get("/reflection", reflection_page)
    app.router.add_post("/api/approvals", submit_approval)
    return app


class DashboardServer:
    """Manage the lifecycle of the dashboard HTTP server.

    Args:
        data_sources: Read ports the handlers consume.
        auth: Auth policy for ``/api/approvals``.  ``None`` (the default)
            disables approval submissions with a 503 — the safe fallback
            when no operator identity is configured yet.
        host: Interface to bind to.  Defaults to loopback.
        port: TCP port to bind.  Defaults to 9900.
    """

    def __init__(
        self,
        data_sources: DashboardDataSources,
        *,
        auth: DashboardAuth | None = None,
        host: str = DASHBOARD_DEFAULT_HOST,
        port: int = DASHBOARD_DEFAULT_PORT,
    ) -> None:
        self._data_sources = data_sources
        self._auth = auth
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def host(self) -> str:
        """Return the bound host."""
        return self._host

    @property
    def port(self) -> int:
        """Return the bound port."""
        return self._port

    @property
    def url(self) -> str:
        """Return the base URL the dashboard is served on."""
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        """Start listening; idempotent — re-calling is a no-op."""
        if self._runner is not None:
            return
        app = create_app(self._data_sources, auth=self._auth)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        log.info("dashboard.started", host=self._host, port=self._port)

    async def stop(self) -> None:
        """Gracefully shut down; idempotent."""
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        self._site = None
        log.info("dashboard.stopped")


__all__ = [
    "DASHBOARD_DEFAULT_HOST",
    "DASHBOARD_DEFAULT_PORT",
    "DashboardAuth",
    "DashboardServer",
    "create_app",
]
