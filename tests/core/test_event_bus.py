"""Tests for src/coremind/core/event_bus.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from coremind.core.event_bus import DEFAULT_MAX_QUEUE_SIZE, EventBus
from coremind.world.model import EntityRef, JsonValue, WorldEventRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    attribute: str = "cpu_percent",
    value: JsonValue = 42.0,
    event_id: str = "test-event-01",
) -> WorldEventRecord:
    """Build a minimal WorldEventRecord for testing."""
    return WorldEventRecord(
        id=event_id,
        timestamp=datetime.now(UTC),
        source="plugin.test",
        source_version="1.0.0",
        signature="ed25519:" + "a" * 128,
        entity=EntityRef(type="host", id="test-host"),
        attribute=attribute,
        value=value,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Basic delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_delivers_event_to_single_subscriber() -> None:
    bus = EventBus()
    sub = bus.subscribe()

    await bus.publish(_make_event("cpu_percent", 42.0))

    event = await anext(sub)

    assert event.attribute == "cpu_percent"
    assert event.value == 42.0
    await sub.aclose()


@pytest.mark.asyncio
async def test_publish_delivers_event_to_multiple_subscribers() -> None:
    bus = EventBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()

    await bus.publish(_make_event("cpu_percent", 55.0))

    event_a = await anext(sub_a)
    event_b = await anext(sub_b)

    assert event_a.value == 55.0
    assert event_b.value == 55.0
    await sub_a.aclose()
    await sub_b.aclose()


@pytest.mark.asyncio
async def test_events_are_delivered_in_order() -> None:
    bus = EventBus()
    sub = bus.subscribe()

    for i in range(5):
        await bus.publish(_make_event("counter", i, event_id=f"evt-{i}"))

    received = [await anext(sub) for _ in range(5)]

    assert [e.value for e in received] == list(range(5))
    await sub.aclose()


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_does_not_raise() -> None:
    bus = EventBus()

    await bus.publish(_make_event())  # must not raise


# ---------------------------------------------------------------------------
# Subscription lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_registers_queue() -> None:
    bus = EventBus()

    assert bus.subscriber_count == 0
    sub = bus.subscribe()
    assert bus.subscriber_count == 1

    await sub.aclose()


@pytest.mark.asyncio
async def test_subscribe_cleanup_removes_queue_after_close() -> None:
    bus = EventBus()
    sub = bus.subscribe()

    assert bus.subscriber_count == 1

    await sub.aclose()

    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_multiple_subscribers_all_removed_after_close() -> None:
    bus = EventBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()

    assert bus.subscriber_count == 2

    await sub_a.aclose()
    await sub_b.aclose()

    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_closed_subscriber_no_longer_receives_events() -> None:
    bus = EventBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()

    await sub_a.aclose()
    await bus.publish(_make_event("temp", 99.0))

    # sub_b should still receive the event
    event = await anext(sub_b)
    assert event.value == 99.0
    # bus should only have sub_b's queue
    assert bus.subscriber_count == 1

    await sub_b.aclose()


# ---------------------------------------------------------------------------
# Overflow and backpressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overflow_drops_oldest_event() -> None:
    """When a subscriber's queue is full, the oldest event is dropped."""
    bus = EventBus(max_queue_size=2)
    sub = bus.subscribe()

    # Fill the queue to capacity
    await bus.publish(_make_event("counter", 0, "evt-0"))
    await bus.publish(_make_event("counter", 1, "evt-1"))

    # This publish overflows: drops value=0, adds value=2
    # Then tries to add the meta-event but queue is full → skipped
    await bus.publish(_make_event("counter", 2, "evt-2"))

    first = await anext(sub)
    second = await anext(sub)

    assert first.value == 1
    assert second.value == 2
    await sub.aclose()


@pytest.mark.asyncio
async def test_overflow_emits_meta_event_to_subscriber_with_capacity() -> None:
    """The bus.overflow meta-event is delivered to non-full subscribers."""
    bus = EventBus(max_queue_size=2)
    sub_slow = bus.subscribe()  # will overflow
    sub_monitor = bus.subscribe()  # will receive the meta-event

    # Fill both queues to capacity
    await bus.publish(_make_event("counter", 0, "evt-0"))
    await bus.publish(_make_event("counter", 1, "evt-1"))

    # Drain sub_monitor so it has room for both the new event and the meta-event
    _ = await anext(sub_monitor)
    _ = await anext(sub_monitor)

    # This publish triggers overflow on sub_slow (full) and emits meta to sub_monitor
    await bus.publish(_make_event("counter", 2, "evt-2"))

    # sub_monitor: received event_2 (fits), then meta-event (fits)
    event_2 = await anext(sub_monitor)
    meta = await anext(sub_monitor)

    assert event_2.value == 2
    assert meta.attribute == "bus.overflow"
    assert meta.source == "coremind.daemon"
    assert isinstance(meta.value, dict)
    assert meta.value["dropped_count"] == 1

    await sub_slow.aclose()
    await sub_monitor.aclose()


@pytest.mark.asyncio
async def test_overflow_meta_event_has_no_signature() -> None:
    """Internal meta-events carry signature=None (never persisted to L2)."""
    bus = EventBus(max_queue_size=2)
    sub_slow = bus.subscribe()
    sub_monitor = bus.subscribe()

    await bus.publish(_make_event("counter", 0, "evt-0"))
    await bus.publish(_make_event("counter", 1, "evt-1"))
    _ = await anext(sub_monitor)
    _ = await anext(sub_monitor)

    await bus.publish(_make_event("counter", 2, "evt-2"))

    _ = await anext(sub_monitor)
    meta = await anext(sub_monitor)

    assert meta.signature is None
    await sub_slow.aclose()
    await sub_monitor.aclose()


@pytest.mark.asyncio
async def test_default_max_queue_size_is_applied() -> None:
    """EventBus uses DEFAULT_MAX_QUEUE_SIZE when no argument is given."""
    bus = EventBus()
    sub = bus.subscribe()

    assert bus.max_queue_size == DEFAULT_MAX_QUEUE_SIZE

    await sub.aclose()


# ---------------------------------------------------------------------------
# Iteration protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_for_iterates_events_until_closed() -> None:
    """async for delivers all pending events and terminates cleanly on close."""
    bus = EventBus()
    sub = bus.subscribe()

    for i in range(3):
        await bus.publish(_make_event("counter", i, f"evt-{i}"))

    received: list[WorldEventRecord] = []
    all_received = asyncio.Event()

    async def reader() -> None:
        async for event in sub:
            received.append(event)
            if len(received) == 3:
                all_received.set()

    reader_task = asyncio.create_task(reader())
    await all_received.wait()
    await sub.aclose()
    await reader_task

    assert [e.value for e in received] == list(range(3))


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    """Calling aclose() a second time does not raise."""
    bus = EventBus()
    sub = bus.subscribe()

    await sub.aclose()
    await sub.aclose()  # must not raise

    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_concurrent_close_unblocks_blocked_reader() -> None:
    """aclose() from a concurrent task wakes a reader blocked on asend()."""
    bus = EventBus()
    sub = bus.subscribe()

    result_holder: list[WorldEventRecord | None] = []

    async def reader() -> None:
        try:
            result_holder.append(await anext(sub))
        except StopAsyncIteration:
            result_holder.append(None)

    reader_task = asyncio.create_task(reader())
    await asyncio.sleep(0)  # yield so reader reaches asyncio.wait

    await sub.aclose()
    await reader_task

    assert result_holder == [None]
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_concurrent_publishers_all_deliver_events() -> None:
    """Events from concurrent publishers are all delivered without loss or corruption."""
    bus = EventBus()
    sub = bus.subscribe()

    n_publishers = 10
    n_events_each = 5

    async def publish_n(publisher_id: int) -> None:
        for i in range(n_events_each):
            await bus.publish(
                _make_event("concurrent", publisher_id * 100 + i, f"evt-{publisher_id}-{i}")
            )

    await asyncio.gather(*[publish_n(i) for i in range(n_publishers)])

    received = [await anext(sub) for _ in range(n_publishers * n_events_each)]
    await sub.aclose()

    assert len(received) == n_publishers * n_events_each
