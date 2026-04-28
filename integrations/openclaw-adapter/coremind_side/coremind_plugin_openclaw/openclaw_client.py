"""gRPC client to the OpenClaw half of the adapter.

Thin wrapper around :class:`adapter_pb2_grpc.OpenClawHalfStub` that:

* Normalises the transport address ("unix://…" or "host:port") into a
  ``grpc.aio`` channel.
* Reconnects with exponential back-off on transport failures.
* Converts transport errors into domain-level exceptions on the boundary.

The dispatcher depends on the :class:`OpenClawClient` protocol (see
``action_dispatcher.py``); this class is one implementation of that protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

import grpc
import grpc.aio
import structlog

from coremind_plugin_openclaw._generated import adapter_pb2, adapter_pb2_grpc

log = structlog.get_logger(__name__)

_T = TypeVar("_T")

_INITIAL_BACKOFF_SECONDS: float = 1.0
_MAX_BACKOFF_SECONDS: float = 30.0
_DEFAULT_MAX_ATTEMPTS: int = 6
"""Default cap for :meth:`OpenClawGrpcClient.connect_with_backoff`.

Roughly 1 + 2 + 4 + 8 + 16 + 30 ≈ 61s of total wait before giving up. A
caller that needs indefinite retries can pass ``max_attempts=None``.
"""


class OpenClawUnavailableError(RuntimeError):
    """Raised when the OpenClaw side is reachable but rejected the call."""


def _resolve_address(address: str) -> str:
    """Return a gRPC channel address from a user-friendly form."""
    if address.startswith(("unix://", "unix:", "dns:", "ipv4:", "ipv6:")):
        return address
    if address.startswith(("/", "~")):
        return f"unix://{address}"
    return address


class OpenClawGrpcClient:
    """Async gRPC client implementing the :class:`OpenClawClient` protocol."""

    def __init__(self, address: str, *, connect_timeout: float = 5.0) -> None:
        self._address = _resolve_address(address)
        self._connect_timeout = connect_timeout
        self._channel: grpc.aio.Channel | None = None
        self._stub: adapter_pb2_grpc.OpenClawHalfStub | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the gRPC channel. Safe to call multiple times."""
        async with self._lock:
            if self._channel is not None:
                return
            log.info("openclaw_client.connecting", address=self._address)
            self._channel = grpc.aio.insecure_channel(self._address)
            self._stub = adapter_pb2_grpc.OpenClawHalfStub(self._channel)  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        """Close the underlying gRPC channel."""
        async with self._lock:
            if self._channel is not None:
                await self._channel.close(grace=None)
                self._channel = None
                self._stub = None
                log.info("openclaw_client.closed")

    async def _ensure_stub(self) -> adapter_pb2_grpc.OpenClawHalfStub:
        if self._stub is None:
            await self.connect()
        assert self._stub is not None  # noqa: S101 — invariant after connect()
        return self._stub

    async def _reset(self) -> None:
        """Tear down the channel so the next call reconnects."""
        async with self._lock:
            if self._channel is not None:
                await self._channel.close(grace=None)
            self._channel = None
            self._stub = None

    async def connect_with_backoff(
        self,
        *,
        max_attempts: int | None = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Retry :meth:`connect` + health probe with exponential back-off.

        Bounded by *max_attempts* (``None`` = unbounded). Raises
        :class:`OpenClawUnavailableError` after exhausting attempts so that
        callers (e.g. the action dispatcher) can surface ``UNAVAILABLE``
        promptly instead of hanging the RPC until the daemon's deadline.
        """
        backoff = _INITIAL_BACKOFF_SECONDS
        attempts = 0
        last_error: str | None = None
        while True:
            attempts += 1
            try:
                await self.connect()
                stub = await self._ensure_stub()
                from google.protobuf import empty_pb2  # noqa: PLC0415

                await stub.HealthCheck(empty_pb2.Empty(), timeout=self._connect_timeout)
                return
            except grpc.RpcError as exc:
                code = exc.code().name if exc.code() else "unknown"
                last_error = f"{code}: {exc.details()}"
                log.warning(
                    "openclaw_client.health_probe_failed",
                    code=code,
                    details=exc.details(),
                    backoff_seconds=backoff,
                    attempts=attempts,
                )
                await self._reset()
                if max_attempts is not None and attempts >= max_attempts:
                    raise OpenClawUnavailableError(
                        f"OpenClaw unreachable after {attempts} attempts: {last_error}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    # ------------------------------------------------------------------
    # OpenClawClient protocol
    # ------------------------------------------------------------------

    async def _with_reset(self, coro: Awaitable[_T]) -> _T:
        """Await *coro*; on transport failure drop the channel for reconnect."""
        try:
            return await coro
        except grpc.RpcError as exc:
            code = exc.code()
            # UNAVAILABLE / CANCELLED / UNKNOWN typically mean the channel is
            # dead. Force a reconnect on the next call.
            if code in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.CANCELLED,
                grpc.StatusCode.UNKNOWN,
            }:
                await self._reset()
            raise

    async def notify(self, request: adapter_pb2.NotifyRequest) -> adapter_pb2.NotifyResult:
        stub = await self._ensure_stub()
        result: adapter_pb2.NotifyResult = await self._with_reset(stub.Notify(request))
        return result

    async def request_approval(
        self, request: adapter_pb2.ApprovalRequest
    ) -> adapter_pb2.ApprovalResult:
        stub = await self._ensure_stub()
        result: adapter_pb2.ApprovalResult = await self._with_reset(stub.RequestApproval(request))
        return result

    async def invoke_skill(self, request: adapter_pb2.SkillInvocation) -> adapter_pb2.SkillResult:
        stub = await self._ensure_stub()
        result: adapter_pb2.SkillResult = await self._with_reset(stub.InvokeSkill(request))
        return result

    async def schedule_cron(
        self, request: adapter_pb2.CronScheduleRequest
    ) -> adapter_pb2.CronScheduleResult:
        stub = await self._ensure_stub()
        result: adapter_pb2.CronScheduleResult = await self._with_reset(stub.ScheduleCron(request))
        return result

    async def cancel_cron(
        self, request: adapter_pb2.CronCancelRequest
    ) -> adapter_pb2.CronCancelResult:
        stub = await self._ensure_stub()
        result: adapter_pb2.CronCancelResult = await self._with_reset(stub.CancelCron(request))
        return result
