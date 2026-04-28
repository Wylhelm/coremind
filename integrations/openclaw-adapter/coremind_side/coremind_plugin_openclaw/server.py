"""gRPC server implementing the CoreMindHalf service.

The OpenClaw-side extension connects to this server as a client. Each event
it pushes via :meth:`IngestEvent` is:

1. Verified to have a valid ed25519 signature from the adapter plugin's key.
2. Forwarded to the CoreMind daemon via ``CoreMindHost.EmitEvent``.
3. Counted for health reporting.

Offline buffering is the responsibility of the **OpenClaw (producer) side**
— it is closer to the event source and can queue on its own filesystem. When
the daemon is unreachable this forwarder surfaces ``UNAVAILABLE`` promptly
so the TS side can decide whether to retry or drop. Buffering on *both*
sides would cause duplicate events on reconnect.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, verify
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind_plugin_openclaw._generated import adapter_pb2, adapter_pb2_grpc

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Daemon forwarder
# ---------------------------------------------------------------------------


class DaemonForwarder:
    """Forwards signed WorldEvents to the CoreMind daemon.

    On transport failure the channel is torn down (so the next call
    re-connects) and the error is surfaced to the caller. Offline queueing
    lives on the OpenClaw-side producer; see module docstring.
    """

    def __init__(
        self,
        host_socket: Path,
        *,
        plugin_id: str,
    ) -> None:
        self._address = f"unix://{host_socket}"
        self._plugin_id = plugin_id
        self._channel: grpc.aio.Channel | None = None
        self._stub: plugin_pb2_grpc.CoreMindHostStub | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the channel to the daemon host."""
        async with self._lock:
            if self._channel is not None:
                return
            self._channel = grpc.aio.insecure_channel(self._address)
            self._stub = plugin_pb2_grpc.CoreMindHostStub(self._channel)  # type: ignore[no-untyped-call]
            log.info("daemon_forwarder.connected", address=self._address)

    async def close(self) -> None:
        async with self._lock:
            if self._channel is not None:
                await self._channel.close(grace=None)
                self._channel = None
                self._stub = None

    async def _reset(self) -> None:
        """Tear down the channel so the next call reconnects."""
        async with self._lock:
            if self._channel is not None:
                await self._channel.close(grace=None)
            self._channel = None
            self._stub = None

    async def forward(self, event: plugin_pb2.WorldEvent) -> None:
        """Forward *event* to the daemon. Raises ``grpc.RpcError`` on failure."""
        if self._stub is None:
            await self.connect()
        metadata = (("x-plugin-id", self._plugin_id),)
        try:
            assert self._stub is not None  # noqa: S101
            await self._stub.EmitEvent(event, metadata=metadata)
        except grpc.RpcError as exc:
            log.warning(
                "daemon_forwarder.emit_failed",
                code=exc.code().name if exc.code() else "unknown",
                details=exc.details(),
            )
            # Drop the channel so we re-open on the next attempt.
            await self._reset()
            raise


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------


class _CoreMindHalfServicer(adapter_pb2_grpc.CoreMindHalfServicer):
    """gRPC servicer handling in-bound events from the OpenClaw side."""

    def __init__(
        self,
        *,
        plugin_public_key: Ed25519PublicKey,
        plugin_id: str,
        forwarder: DaemonForwarder,
    ) -> None:
        self._plugin_public_key = plugin_public_key
        self._plugin_id = plugin_id
        self._forwarder = forwarder
        self._events_processed: int = 0

    async def IngestEvent(  # noqa: N802
        self,
        request: plugin_pb2.WorldEvent,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        """Verify and forward a signed WorldEvent to the CoreMind daemon."""
        if request.source != self._plugin_id:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"event.source must equal {self._plugin_id!r}; got {request.source!r}",
            )
            return empty_pb2.Empty()

        sig: bytes = request.signature
        if not sig:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "WorldEvent.signature is empty",
            )
            return empty_pb2.Empty()

        event_dict = MessageToDict(request, preserving_proto_field_name=True)
        event_dict.pop("signature", None)
        payload = canonical_json(event_dict)
        if not verify(payload, sig, self._plugin_public_key):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "WorldEvent signature verification failed",
            )
            return empty_pb2.Empty()

        try:
            await self._forwarder.forward(request)
        except grpc.RpcError:
            # Already logged by the forwarder; the event is buffered on disk.
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "CoreMind daemon unreachable; event buffered for retry",
            )
            return empty_pb2.Empty()

        self._events_processed += 1
        return empty_pb2.Empty()

    async def HealthCheck(  # noqa: N802
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        ts = Timestamp()
        ts.FromDatetime(datetime.now(UTC))
        return adapter_pb2.Health(
            state=adapter_pb2.HEALTH_STATE_OK,
            message="coremind half nominal",
            as_of=ts,
            events_processed=self._events_processed,
            actions_dispatched=0,
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class CoreMindHalfServer:
    """Runs the ``CoreMindHalf`` gRPC server on a Unix socket or TCP address."""

    def __init__(
        self,
        address: str,
        *,
        plugin_public_key: Ed25519PublicKey,
        plugin_id: str,
        forwarder: DaemonForwarder,
        graceful_shutdown_timeout: float = 5.0,
    ) -> None:
        self._address = address
        self._plugin_public_key = plugin_public_key
        self._plugin_id = plugin_id
        self._forwarder = forwarder
        self._graceful_shutdown_timeout = graceful_shutdown_timeout
        self._server: grpc.aio.Server | None = None

    async def start(self) -> None:
        servicer = _CoreMindHalfServicer(
            plugin_public_key=self._plugin_public_key,
            plugin_id=self._plugin_id,
            forwarder=self._forwarder,
        )
        self._server = grpc.aio.server()
        adapter_pb2_grpc.add_CoreMindHalfServicer_to_server(servicer, self._server)  # type: ignore[no-untyped-call]

        # Clean stale Unix socket if necessary. Sync pathlib here is fine: it
        # runs once at startup before any traffic.
        if self._address.startswith("unix://"):
            sock_path = Path(self._address.removeprefix("unix://"))
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            if sock_path.exists():  # noqa: ASYNC240 — startup-only, local FS
                sock_path.unlink()  # noqa: ASYNC240 — startup-only, local FS

        self._server.add_insecure_port(self._address)
        await self._server.start()
        log.info("coremind_half.started", address=self._address)

    async def stop(self) -> None:
        if self._server is None:
            return
        await self._server.stop(self._graceful_shutdown_timeout)
        self._server = None
        log.info("coremind_half.stopped")

    async def wait_for_termination(self) -> None:
        if self._server is None:
            return
        await self._server.wait_for_termination()
