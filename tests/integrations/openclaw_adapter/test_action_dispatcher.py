"""Unit tests for the OpenClaw action dispatcher, including scope narrowing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coremind_plugin_openclaw._generated import adapter_pb2
from coremind_plugin_openclaw.action_dispatcher import (
    ActionDispatcher,
    InvalidParametersError,
    PermissionDeniedError,
    PermissionScope,
    UnknownOperationError,
)
from google.protobuf.timestamp_pb2 import Timestamp

SCHEMA_DIR = Path("integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/schemas")


class _FakeClient:
    """In-memory fake implementing the OpenClawClient protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def notify(self, request: adapter_pb2.NotifyRequest) -> adapter_pb2.NotifyResult:
        self.calls.append(("notify", request))
        return adapter_pb2.NotifyResult(delivered=True, message_id="msg-1", error="")

    async def request_approval(
        self, request: adapter_pb2.ApprovalRequest
    ) -> adapter_pb2.ApprovalResult:
        self.calls.append(("approval", request))
        ts = Timestamp()
        ts.FromDatetime(datetime.now(UTC))
        return adapter_pb2.ApprovalResult(
            outcome=adapter_pb2.APPROVAL_OUTCOME_APPROVED,
            approval_id=request.approval_id,
            feedback="ok",
            responded_at=ts,
        )

    async def invoke_skill(self, request: adapter_pb2.SkillInvocation) -> adapter_pb2.SkillResult:
        self.calls.append(("skill", request))
        return adapter_pb2.SkillResult(call_id=request.call_id, ok=True, error="")

    async def schedule_cron(
        self, request: adapter_pb2.CronScheduleRequest
    ) -> adapter_pb2.CronScheduleResult:
        self.calls.append(("cron_sched", request))
        return adapter_pb2.CronScheduleResult(scheduled=True, cron_id=request.cron_id, error="")

    async def cancel_cron(
        self, request: adapter_pb2.CronCancelRequest
    ) -> adapter_pb2.CronCancelResult:
        self.calls.append(("cron_cancel", request))
        return adapter_pb2.CronCancelResult(cancelled=True, error="")


def _dispatcher(scope: PermissionScope | None = None) -> tuple[ActionDispatcher, _FakeClient]:
    client = _FakeClient()
    dispatcher = ActionDispatcher(
        client=client,
        scope=scope or PermissionScope(),
        schema_dir=SCHEMA_DIR,
    )
    return dispatcher, client


@pytest.mark.asyncio
async def test_notify_happy_path() -> None:
    dispatcher, client = _dispatcher()
    out = await dispatcher.dispatch(
        "openclaw.notify",
        {"channel": "telegram", "target": "6394043863", "text": "hello"},
    )
    assert out["delivered"] is True
    assert out["message_id"] == "msg-1"
    assert len(client.calls) == 1 and client.calls[0][0] == "notify"


@pytest.mark.asyncio
async def test_unknown_operation_raises() -> None:
    dispatcher, _ = _dispatcher()
    with pytest.raises(UnknownOperationError):
        await dispatcher.dispatch("openclaw.self_destruct", {"x": 1})


@pytest.mark.asyncio
async def test_invalid_params_raises() -> None:
    dispatcher, _ = _dispatcher()
    with pytest.raises(InvalidParametersError):
        # Missing `text`.
        await dispatcher.dispatch(
            "openclaw.notify",
            {"channel": "telegram", "target": "x"},
        )


@pytest.mark.asyncio
async def test_scope_narrowing_rejects_disallowed_channel() -> None:
    dispatcher, client = _dispatcher(PermissionScope(allowed_channels=("telegram",)))
    with pytest.raises(PermissionDeniedError):
        await dispatcher.dispatch(
            "openclaw.notify",
            {"channel": "discord", "target": "x", "text": "hi"},
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_scope_narrowing_allows_matching_channel() -> None:
    dispatcher, client = _dispatcher(PermissionScope(allowed_channels=("telegram",)))
    await dispatcher.dispatch(
        "openclaw.notify",
        {"channel": "telegram", "target": "x", "text": "hi"},
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_scope_narrowing_rejects_disallowed_skill() -> None:
    dispatcher, _ = _dispatcher(PermissionScope(allowed_skills=("weather.*",)))
    with pytest.raises(PermissionDeniedError):
        await dispatcher.dispatch(
            "openclaw.invoke_skill",
            {"skill_name": "finance.transfer"},
        )


@pytest.mark.asyncio
async def test_invoke_skill_with_allowed_glob() -> None:
    dispatcher, client = _dispatcher(PermissionScope(allowed_skills=("weather.*",)))
    out = await dispatcher.dispatch(
        "openclaw.invoke_skill",
        {"skill_name": "weather.lookup", "parameters": {"city": "Ottawa"}, "call_id": "c1"},
    )
    assert out["ok"] is True
    assert client.calls[0][0] == "skill"


@pytest.mark.asyncio
async def test_cron_manage_disabled_blocks_schedule() -> None:
    dispatcher, _ = _dispatcher(PermissionScope(cron_manage=False))
    with pytest.raises(PermissionDeniedError):
        await dispatcher.dispatch(
            "openclaw.schedule_cron",
            {"cron_id": "c1", "expression": "0 * * * *", "skill_name": "x"},
        )


@pytest.mark.asyncio
async def test_approve_request_roundtrip() -> None:
    dispatcher, _ = _dispatcher()
    out = await dispatcher.dispatch(
        "openclaw.approve_request",
        {
            "approval_id": "appr-1",
            "channel": "telegram",
            "target": "6394043863",
            "prompt": "please approve",
        },
    )
    assert out["outcome"] == "approved"
    assert out["approval_id"] == "appr-1"


@pytest.mark.asyncio
async def test_schedule_and_cancel_cron() -> None:
    dispatcher, client = _dispatcher()
    out = await dispatcher.dispatch(
        "openclaw.schedule_cron",
        {
            "cron_id": "daily",
            "expression": "0 8 * * *",
            "skill_name": "briefing.generate",
            "parameters": {"locale": "en-CA"},
        },
    )
    assert out["scheduled"] is True
    out2 = await dispatcher.dispatch("openclaw.cancel_cron", {"cron_id": "daily"})
    assert out2["cancelled"] is True
    assert [c[0] for c in client.calls] == ["cron_sched", "cron_cancel"]
