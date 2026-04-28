"""CoreMindPlugin gRPC implementation for the OpenClaw adapter.

The daemon discovers this plugin and calls:

* :meth:`Identify` — returns the static manifest.
* :meth:`Start`     — opens an empty WorldEvent stream; this plugin does not
                      emit events through the streaming API (events arrive
                      from the OpenClaw side and are forwarded via
                      ``CoreMindHost.EmitEvent``).
* :meth:`Stop`      — shuts the plugin down.
* :meth:`HealthCheck` — returns an aggregate status of both halves.
* :meth:`InvokeAction` — routes an effector action to the OpenClaw half via
                         :class:`ActionDispatcher`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import grpc
import grpc.aio
import structlog
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind_plugin_openclaw.action_dispatcher import (
    ActionDispatcher,
    InvalidParametersError,
    PermissionDeniedError,
    UnknownOperationError,
)

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.openclaw_adapter"
PLUGIN_VERSION: str = "0.1.0"
DISPLAY_NAME: str = "OpenClaw Adapter"
LICENSE_STR: str = "AGPL-3.0-or-later"
AUTHOR: str = "Guillaume Gagnon"


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def build_manifest(scoped_permissions: list[str] | None = None) -> plugin_pb2.PluginManifest:
    """Return a :class:`PluginManifest` proto reflecting this plugin's identity.

    Args:
        scoped_permissions: Live permission scope (may be narrower than the
            manifest file's declaration).  Defaults to the full set.
    """
    permissions = scoped_permissions or [
        "network:local",
        "openclaw:channels:*",
        "openclaw:skills:*",
        "openclaw:cron:manage",
    ]
    return plugin_pb2.PluginManifest(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        display_name=DISPLAY_NAME,
        kind=plugin_pb2.PLUGIN_KIND_BIDIRECTIONAL,
        provides_entities=["conversation", "skill_run", "cron_job", "approval"],
        emits_attributes=[
            "message_received",
            "message_sent",
            "skill_invoked",
            "skill_completed",
            "cron_executed",
            "approval_responded",
            "integration.openclaw.degraded",
        ],
        accepts_operations=[
            "openclaw.notify",
            "openclaw.approve_request",
            "openclaw.invoke_skill",
            "openclaw.schedule_cron",
            "openclaw.cancel_cron",
        ],
        required_permissions=permissions,
        license=LICENSE_STR,
        author=AUTHOR,
        min_daemon_version="0.0.1",
    )


# ---------------------------------------------------------------------------
# Servicer
# ---------------------------------------------------------------------------


class CoreMindPluginServicer(plugin_pb2_grpc.CoreMindPluginServicer):
    """Plugin lifecycle servicer for the OpenClaw adapter."""

    def __init__(
        self,
        *,
        dispatcher: ActionDispatcher,
        scoped_permissions: list[str] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._scoped_permissions = scoped_permissions
        self._started: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()
        self._events_emitted: int = 0
        self._actions_attempted: int = 0
        self._actions_failed: int = 0

    async def Identify(  # noqa: N802
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> plugin_pb2.PluginManifest:
        return build_manifest(self._scoped_permissions)

    async def Start(  # noqa: N802
        self,
        request: plugin_pb2.PluginConfig,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> AsyncIterator[plugin_pb2.WorldEvent]:
        """Open a long-lived, intentionally empty event stream.

        The adapter does not push events through the streaming API: OpenClaw
        pushes them into :class:`CoreMindHalfServer`, which forwards them to
        the daemon via ``CoreMindHost.EmitEvent`` out-of-band. But the daemon
        still expects this server-streaming RPC to stay open for the lifetime
        of the plugin, so we yield nothing and block on :attr:`_stop_event`.
        """
        if self._started:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "plugin already started")
            return
        self._started = True
        self._stop_event.clear()
        log.info("openclaw_plugin.started")
        # Block until Stop() is called or the daemon cancels the RPC. The
        # generator yields zero items but must remain an async generator —
        # `return` alone here is not enough; we need the `yield` below to be
        # syntactically present. It is unreachable.
        await self._stop_event.wait()
        return
        yield plugin_pb2.WorldEvent()  # pragma: no cover — unreachable, makes this a generator

    async def Stop(  # noqa: N802
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> empty_pb2.Empty:
        if not self._started:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "plugin not started")
        self._started = False
        self._stop_event.set()
        log.info("openclaw_plugin.stopped")
        return empty_pb2.Empty()

    async def HealthCheck(  # noqa: N802
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> plugin_pb2.HealthStatus:
        ts = Timestamp()
        ts.FromDatetime(datetime.now(UTC))
        return plugin_pb2.HealthStatus(
            state=plugin_pb2.HEALTH_STATE_OK,
            message="openclaw adapter running",
            last_event_at=ts,
            events_emitted=self._events_emitted,
            actions_attempted=self._actions_attempted,
            actions_failed=self._actions_failed,
        )

    async def InvokeAction(  # noqa: N802
        self,
        request: plugin_pb2.ActionRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> plugin_pb2.ActionResult:
        self._actions_attempted += 1
        operation = request.operation
        params = MessageToDict(request.parameters) if request.HasField("parameters") else {}
        completed_at = Timestamp()

        try:
            outcome = await self._dispatcher.dispatch(operation, params)
        except PermissionDeniedError as exc:
            self._actions_failed += 1
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            return plugin_pb2.ActionResult()
        except UnknownOperationError as exc:
            self._actions_failed += 1
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
            return plugin_pb2.ActionResult()
        except InvalidParametersError as exc:
            self._actions_failed += 1
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return plugin_pb2.ActionResult()
        except grpc.RpcError as exc:
            self._actions_failed += 1
            msg = exc.details() or "openclaw half unavailable"
            await context.abort(grpc.StatusCode.UNAVAILABLE, msg)
            return plugin_pb2.ActionResult()

        completed_at.FromDatetime(datetime.now(UTC))
        output = Struct()
        # Struct cannot nest arbitrary None; drop them for cleanliness.
        output.update({k: v for k, v in outcome.items() if v is not None})
        return plugin_pb2.ActionResult(
            action_id=request.action_id,
            status=plugin_pb2.ACTION_STATUS_OK,
            message="",
            output=output,
            completed_at=completed_at,
        )
