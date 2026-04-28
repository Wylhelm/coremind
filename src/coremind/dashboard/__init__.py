"""Read-only web dashboard (Phase 4, Task 4.6).

The dashboard is an aiohttp application that surfaces the daemon's internal
state — events, world snapshot, reasoning cycles, intents, the action
journal, and reflection reports — on ``http://127.0.0.1:9900`` by default.

Beyond pure read-only views, the dashboard also acts as the UI surface for
the Phase 3 :class:`~coremind.notify.adapters.dashboard.DashboardNotificationPort`:
in-app approval buttons forward responses through the port's existing
``submit_response`` channel, which downstream signs and journals every
decision exactly like any other notification adapter.

The dashboard NEVER writes directly to any store.
"""

from __future__ import annotations

from coremind.dashboard.auth import DashboardAuth
from coremind.dashboard.data import (
    CycleSource,
    DashboardDataSources,
    EventSubscriber,
    IntentSource,
    JournalEntryView,
    JournalSource,
    ReflectionReportSource,
    StoredReflectionReport,
    WorldSource,
)
from coremind.dashboard.server import (
    DASHBOARD_DEFAULT_HOST,
    DASHBOARD_DEFAULT_PORT,
    DashboardServer,
    create_app,
)

__all__ = [
    "DASHBOARD_DEFAULT_HOST",
    "DASHBOARD_DEFAULT_PORT",
    "CycleSource",
    "DashboardAuth",
    "DashboardDataSources",
    "DashboardServer",
    "EventSubscriber",
    "IntentSource",
    "JournalEntryView",
    "JournalSource",
    "ReflectionReportSource",
    "StoredReflectionReport",
    "WorldSource",
    "create_app",
]
