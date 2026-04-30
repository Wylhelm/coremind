#!/usr/bin/env python3
"""OpenClawHalf bridge — Python gRPC server replacing the TypeScript extension.

Writes notifications to a JSONL queue consumed by G-Bot's heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import grpc
import grpc.aio
import structlog
from google.protobuf import empty_pb2, timestamp_pb2

from coremind_plugin_openclaw._generated import adapter_pb2, adapter_pb2_grpc

log = structlog.get_logger(__name__)

SOCKET_PATH = Path.home() / ".coremind" / "run" / "openclaw-adapter.sock"
NOTIFY_QUEUE = Path.home() / ".coremind" / "run" / "notify_queue.jsonl"

CHANNELS = ["telegram"]
SKILLS = [
    "weather.current", "weather.forecast",
    "gog.gmail", "gog.calendar", "goplaces.search",
    "notion.page", "notion.database",
    "tapo-cam.snapshot", "tapo-cam.clip",
    "sonoscli.play", "sonoscli.status",
    "openhue.lights", "openhue.scenes",
]


class OpenClawHalfServicer(adapter_pb2_grpc.OpenClawHalfServicer):

    async def Notify(self, request, context):
        try:
            entry = {
                "channel": request.channel,
                "target": request.target,
                "text": request.text,
                "metadata": dict(request.metadata),
                "queued_at": datetime.now(UTC).isoformat(),
            }
            with open(NOTIFY_QUEUE, "a") as f:
                f.write(json.dumps(entry) + "\n")
            log.info("bridge.notify_queued", target=request.target, channel=request.channel)
            return adapter_pb2.NotifyResult(delivered=True, message_id="queued", error="")
        except Exception as exc:
            return adapter_pb2.NotifyResult(delivered=False, error=str(exc))

    async def RequestApproval(self, request, context):
        now = timestamp_pb2.Timestamp()
        now.GetCurrentTime()
        return adapter_pb2.ApprovalResult(
            outcome=adapter_pb2.APPROVAL_OUTCOME_TIMEOUT,
            approval_id=request.approval_id,
            feedback="Not implemented yet",
            responded_at=now,
        )

    async def InvokeSkill(self, request, context):
        return adapter_pb2.SkillResult(call_id=request.call_id, ok=False, error="Not implemented yet")

    async def ScheduleCron(self, request, context):
        return adapter_pb2.CronScheduleResult(scheduled=False, cron_id=request.cron_id, error="Not implemented yet")

    async def CancelCron(self, request, context):
        return adapter_pb2.CronCancelResult(cancelled=False, error="Not implemented yet")

    async def Mem0Query(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return adapter_pb2.Mem0QueryResult()

    async def Mem0Store(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return adapter_pb2.Mem0StoreResult()

    async def ListChannels(self, request, context):
        return adapter_pb2.ChannelList(channels=CHANNELS)

    async def ListSkills(self, request, context):
        return adapter_pb2.SkillList(skills=SKILLS)

    async def HealthCheck(self, request, context):
        now = timestamp_pb2.Timestamp()
        now.GetCurrentTime()
        return adapter_pb2.Health(
            state=adapter_pb2.HEALTH_STATE_OK,
            message="bridge healthy",
            as_of=now,
        )


async def serve() -> None:
    server = grpc.aio.server()
    adapter_pb2_grpc.add_OpenClawHalfServicer_to_server(OpenClawHalfServicer(), server)

    addr = f"unix://{SOCKET_PATH}"
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

    server.add_insecure_port(addr)
    await server.start()
    log.info("openclaw_half.started", address=addr, pid=os.getpid())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    await server.stop(5.0)


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
