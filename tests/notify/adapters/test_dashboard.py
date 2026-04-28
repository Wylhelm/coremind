"""Dashboard notification port tests."""

from __future__ import annotations

from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import ApprovalResponse, UserRef


async def test_notify_records_history() -> None:
    p = DashboardNotificationPort()
    r = await p.notify(message="hi", category="info", actions=None, intent_id="i1")
    assert r.port_id == "dashboard"
    assert len(p.history) == 1
    assert p.history[0].message == "hi"


async def test_submit_and_subscribe_response() -> None:
    p = DashboardNotificationPort()
    resp = ApprovalResponse(intent_id="i1", decision="approve", responder=UserRef(id="u"))
    await p.submit_response(resp)
    it = p.subscribe_responses()
    got = await it.__anext__()
    assert got.intent_id == "i1"
    assert got.decision == "approve"
