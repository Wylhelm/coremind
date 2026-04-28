"""End-to-end test: plugin → gRPC → EventBus → ingest loop → world store.

This test wires together all real Phase-1 components — PluginHostServer,
PluginRegistry, EventBus, and CoreMindDaemon._ingest_loop — using an
in-memory store double instead of SurrealDB.  No external services are
required; the gRPC transport runs over an in-process Unix domain socket.

Mark: e2e (full pipeline, no docker-compose).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import grpc.aio
import pytest
from coremind_plugin_systemstats.main import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    build_signed_event,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from coremind.core.daemon import CoreMindDaemon
from coremind.core.event_bus import EventBus
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind.plugin_host.registry import PluginRegistry
from coremind.plugin_host.server import PluginHostServer
from coremind.world.model import WorldEventRecord

# ---------------------------------------------------------------------------
# In-memory store test double
# ---------------------------------------------------------------------------

_PLUGIN_MANIFEST = plugin_pb2.PluginManifest(
    plugin_id=PLUGIN_ID,
    version=PLUGIN_VERSION,
    display_name="System Stats (E2E)",
    kind=plugin_pb2.PLUGIN_KIND_SENSOR,
    provides_entities=["host"],
    emits_attributes=["cpu_percent", "memory_percent", "uptime_seconds"],
)

_EXPECTED_ATTRIBUTES = frozenset({"cpu_percent", "memory_percent", "uptime_seconds"})


class _InMemoryStore:
    """Minimal WorldStore test double that captures apply_event calls.

    Thread-safe via asyncio.Event so the test can wait for events without
    polling or sleeping.
    """

    def __init__(self) -> None:
        self.events: list[WorldEventRecord] = []
        self._received: asyncio.Event = asyncio.Event()

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Append *event* and signal any waiting waiter."""
        self.events.append(event)
        self._received.set()

    async def wait_for_count(self, count: int, max_wait: float = 5.0) -> None:
        """Block until at least *count* events have been stored.

        Args:
            count: Minimum number of events to wait for.
            max_wait: Maximum seconds to wait before raising TimeoutError.

        Raises:
            TimeoutError: If *count* events are not received within *max_wait*.
        """
        async with asyncio.timeout(max_wait):
            while len(self.events) < count:
                self._received.clear()
                await self._received.wait()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def plugin_private_key() -> Ed25519PrivateKey:
    """Fresh in-memory ed25519 private key — no filesystem I/O."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def populated_registry(plugin_private_key: Ed25519PrivateKey) -> PluginRegistry:
    """PluginRegistry with the systemstats plugin pre-registered."""
    registry = PluginRegistry()
    registry.register(_PLUGIN_MANIFEST, plugin_private_key.public_key())
    return registry


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_signed_plugin_events_land_in_world_store(
    tmp_path: Path,
    plugin_private_key: Ed25519PrivateKey,
    populated_registry: PluginRegistry,
) -> None:
    """3 signed WorldEvents emitted over gRPC arrive in the world store.

    Pipeline under test::

        build_signed_event  →  gRPC EmitEvent  →  PluginHostServer
            →  EventBus  →  _ingest_loop  →  _InMemoryStore

    Assertions:
    - All three expected attributes are present.
    - Every event carries the correct source plugin ID.
    - Every event targets a ``host`` entity named ``testhost``.
    """
    socket_path = tmp_path / "run" / "plugin_host.sock"
    event_bus = EventBus()
    store = _InMemoryStore()

    server = PluginHostServer(
        socket_path=socket_path,
        registry=populated_registry,
        event_bus=event_bus,
        secrets_resolver=lambda _: None,
    )
    await server.start()

    daemon = CoreMindDaemon()
    ingest_task = asyncio.create_task(
        daemon._ingest_loop(event_bus, store),
        name="e2e.ingest",
    )

    try:
        channel_addr = f"unix://{socket_path}"
        async with grpc.aio.insecure_channel(channel_addr) as channel:
            stub: plugin_pb2_grpc.CoreMindHostStub = plugin_pb2_grpc.CoreMindHostStub(  # type: ignore[no-untyped-call]  # generated stub
                channel
            )
            metadata = (("x-plugin-id", PLUGIN_ID),)

            for attribute, value in (
                ("cpu_percent", 42.0),
                ("memory_percent", 55.0),
                ("uptime_seconds", 3600),
            ):
                proto_event = build_signed_event(plugin_private_key, attribute, value, "testhost")
                await stub.EmitEvent(proto_event, metadata=metadata)

        # Wait up to 5 s for all three events to be persisted.
        await store.wait_for_count(3, max_wait=5.0)

    finally:
        ingest_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest_task
        await server.stop()

    received_attributes = {event.attribute for event in store.events}
    assert received_attributes == _EXPECTED_ATTRIBUTES

    assert all(event.source == PLUGIN_ID for event in store.events)
    assert all(event.entity.type == "host" for event in store.events)
    assert all(event.entity.id == "testhost" for event in store.events)
