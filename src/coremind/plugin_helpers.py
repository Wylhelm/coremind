"""Shared plugin robustness helpers.

Provides retry wrappers so every plugin gets connection resilience and
auto-restart without duplicating logic across 8+ main.py files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
import grpc.aio
import structlog

log = structlog.get_logger(__name__)

# Defaults
MAX_RECONNECT_ATTEMPTS: int = 0  # 0 = infinite
INITIAL_BACKOFF_SECONDS: float = 2.0
MAX_BACKOFF_SECONDS: float = 120.0
RESTART_DELAY_SECONDS: float = 5.0


async def robust_plugin_loop(
    plugin_id: str,
    channel_addr: str,
    work_fn: Callable[[Any, Any], Awaitable[None]],
    *,
    interval_seconds: float = 300.0,
    max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
    initial_backoff: float = INITIAL_BACKOFF_SECONDS,
    max_backoff: float = MAX_BACKOFF_SECONDS,
    restart_delay: float = RESTART_DELAY_SECONDS,
    stub_factory: Callable[[Any], Any] | None = None,
) -> None:
    """Run a plugin with automatic reconnection on gRPC failures.

    The outer loop survives total connection loss (daemon restart, socket
    gone).  The inner loop handles transient RPC errors by reconnecting.

    Args:
        plugin_id: Plugin identifier for logging.
        channel_addr: gRPC channel address (e.g. ``unix:///path/to/sock``).
        work_fn: Async callable ``(stub, metadata) -> None`` that performs
            one poll cycle.
        interval_seconds: Seconds to sleep between work cycles.
        max_reconnect_attempts: Max reconnection attempts (0 = infinite).
        initial_backoff: Initial backoff seconds for reconnection.
        max_backoff: Maximum backoff cap.
        restart_delay: Seconds to wait after a total crash before restarting
            the outer loop.
        stub_factory: Optional factory ``channel -> stub``.  Defaults to
            ``CoreMindHostStub(channel)``.

    Example::

        async def my_work(stub, metadata):
            event = build_signed_event(...)
            await stub.EmitEvent(event, metadata=metadata)

        asyncio.run(robust_plugin_loop(
            "coremind.plugin.example",
            "unix:///home/guillaume/.coremind/run/plugin_host.sock",
            my_work,
            interval_seconds=300,
        ))
    """
    if stub_factory is None:
        # Lazy import to avoid pulling grpc-generated code at module level
        from coremind.plugin_api._generated import plugin_pb2_grpc

        def _default_stub(channel: Any) -> Any:
            return plugin_pb2_grpc.CoreMindHostStub(channel)

        stub_factory = _default_stub

    metadata = (("x-plugin-id", plugin_id),)
    attempt = 0

    while max_reconnect_attempts == 0 or attempt < max_reconnect_attempts:
        try:
            async with grpc.aio.insecure_channel(channel_addr) as channel:
                stub = stub_factory(channel)
                log.info("plugin.connected", plugin_id=plugin_id, addr=channel_addr)
                attempt = 0  # Reset on successful connection

                while True:
                    try:
                        await work_fn(stub, metadata)
                    except grpc.RpcError as exc:
                        log.warning(
                            "plugin.rpc_error_reconnecting",
                            plugin_id=plugin_id,
                            error=exc.details() if hasattr(exc, "details") else str(exc),
                        )
                        break  # Break inner loop → reconnect
                    except Exception:
                        log.exception("plugin.work_cycle_error", plugin_id=plugin_id)
                        # Non-gRPC errors shouldn't kill the connection;
                        # sleep and retry the work cycle.
                        await asyncio.sleep(restart_delay)
                        continue

                    await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            log.info("plugin.cancelled", plugin_id=plugin_id)
            raise
        except Exception:
            log.exception("plugin.connection_lost", plugin_id=plugin_id)

        # Exponential backoff before reconnect
        attempt += 1
        backoff = min(initial_backoff * (2 ** (attempt - 1)), max_backoff)
        log.info(
            "plugin.reconnecting",
            plugin_id=plugin_id,
            attempt=attempt,
            backoff_seconds=backoff,
        )
        await asyncio.sleep(backoff)

    log.critical("plugin.max_reconnect_attempts_exceeded", plugin_id=plugin_id)
