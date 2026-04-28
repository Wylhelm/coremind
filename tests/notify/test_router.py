"""NotificationRouter tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time

import pytest

from coremind.errors import NotificationError
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import (
    ApprovalAction,
    ApprovalResponse,
    NotificationCategory,
    NotificationReceipt,
    UserRef,
)
from coremind.notify.quiet_hours import QuietHoursFilter, QuietHoursPolicy
from coremind.notify.router import DeferredNotificationError, NotificationRouter


class _FailingPort:
    id = "failing"
    supports_callbacks = False

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        raise NotificationError("network down")

    def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]:
        async def _empty() -> AsyncIterator[ApprovalResponse]:
            if False:
                yield ApprovalResponse(  # pragma: no cover
                    intent_id="x",
                    decision="approve",
                    responder=UserRef(id="x"),
                )

        return _empty()


def _allowing_policy() -> QuietHoursPolicy:
    return QuietHoursPolicy(timezone="UTC", quiet_start=time(0, 0), quiet_end=time(0, 0))


async def test_primary_success() -> None:
    primary = DashboardNotificationPort()
    quiet = QuietHoursFilter(_allowing_policy())
    router = NotificationRouter(primary, [], quiet)

    receipt = await router.notify(message="hi", category="info", actions=None, intent_id="i1")
    assert receipt.port_id == "dashboard"
    assert len(primary.history) == 1


async def test_fallback_used_on_primary_failure() -> None:
    primary = _FailingPort()
    fallback = DashboardNotificationPort()
    quiet = QuietHoursFilter(_allowing_policy())
    router = NotificationRouter(primary, [fallback], quiet)

    receipt = await router.notify(message="hi", category="info", actions=None, intent_id="i1")
    assert receipt.port_id == "dashboard"
    assert len(fallback.history) == 1


async def test_all_ports_failing_raises() -> None:
    quiet = QuietHoursFilter(_allowing_policy())
    router = NotificationRouter(_FailingPort(), [_FailingPort()], quiet)

    with pytest.raises(NotificationError):
        await router.notify(message="hi", category="info", actions=None, intent_id="i1")


async def test_quiet_hours_defers() -> None:
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(0, 0), quiet_end=time(23, 59))
    quiet = QuietHoursFilter(policy, clock=lambda: datetime(2025, 1, 1, 10, 0, tzinfo=UTC))
    primary = DashboardNotificationPort()
    router = NotificationRouter(primary, [], quiet)
    with pytest.raises(DeferredNotificationError):
        await router.notify(message="x", category="info", actions=None, intent_id="i1")
    assert primary.history == []
