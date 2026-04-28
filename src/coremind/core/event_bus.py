"""In-process async event bus for distributing WorldEventRecords.

Implements a fan-out pub/sub model: each subscriber receives its own
asyncio.Queue. Backpressure is handled by dropping the oldest item
from any full subscriber queue and emitting a bus.overflow meta-event
to all subscribers that have remaining capacity.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, overload

import structlog

from coremind.world.model import EntityRef, WorldEventRecord

log = structlog.get_logger(__name__)

_DAEMON_SOURCE: Final[str] = "coremind.daemon"
_DAEMON_VERSION: Final[str] = "0.0.0"

DEFAULT_MAX_QUEUE_SIZE: Final[int] = 1000


def _make_overflow_event(dropped_count: int) -> WorldEventRecord:
    """Create an internal meta-event describing a bus queue overflow.

    Args:
        dropped_count: Total number of events dropped across all subscribers
            during a single publish call.

    Returns:
        A WorldEventRecord with attribute ``bus.overflow`` and signature=None.
    """
    return WorldEventRecord(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC),
        source=_DAEMON_SOURCE,
        source_version=_DAEMON_VERSION,
        signature=None,
        entity=EntityRef(type="daemon", id="bus"),
        attribute="bus.overflow",
        value={"dropped_count": dropped_count},
        confidence=1.0,
    )


class _Subscription(AsyncGenerator[WorldEventRecord, None]):
    """AsyncGenerator wrapping a single subscriber queue.

    Inherits from ``AsyncGenerator`` so callers can safely call ``aclose()``
    before any events have been read. Unstarted async-generator ``finally``
    blocks are not guaranteed to run on ``aclose()``, so cleanup is handled
    explicitly here.
    """

    __slots__ = ("_bus_queues", "_closed", "_queue", "_shutdown")

    def __init__(
        self,
        queue: asyncio.Queue[WorldEventRecord],
        bus_queues: list[asyncio.Queue[WorldEventRecord]],
    ) -> None:
        self._queue = queue
        self._bus_queues = bus_queues
        self._closed = False
        self._shutdown = asyncio.Event()

    async def asend(self, value: None) -> WorldEventRecord:
        """Return the next event from the subscription queue.

        Races the queue read against the shutdown event so that a concurrent
        ``aclose()`` call always unblocks a waiting reader.

        Args:
            value: Unused (send protocol requirement; must be None).

        Returns:
            The next WorldEventRecord from the queue.

        Raises:
            StopAsyncIteration: When the subscription has been closed.
        """
        if self._closed:
            raise StopAsyncIteration

        get_task: asyncio.Task[WorldEventRecord] = asyncio.create_task(self._queue.get())
        shutdown_task: asyncio.Task[bool] = asyncio.create_task(self._shutdown.wait())

        try:
            done, pending = await asyncio.wait(
                {get_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            get_task.cancel()
            shutdown_task.cancel()
            raise

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if shutdown_task in done:
            raise StopAsyncIteration

        return get_task.result()

    @overload
    async def athrow(
        self,
        typ: type[BaseException],
        val: BaseException | object = ...,
        tb: TracebackType | None = ...,
    ) -> WorldEventRecord: ...

    @overload
    async def athrow(
        self,
        typ: BaseException,
        val: None = ...,
        tb: TracebackType | None = ...,
    ) -> WorldEventRecord: ...

    async def athrow(
        self,
        typ: type[BaseException] | BaseException,
        val: BaseException | object | None = None,
        tb: TracebackType | None = None,
    ) -> WorldEventRecord:
        """Throw an exception into the subscription, closing it.

        Args:
            typ: The exception type or instance to throw.
            val: Optional exception value when typ is a type.
            tb: Optional traceback.

        Raises:
            The given exception unconditionally.
        """
        await self.aclose()
        exc: BaseException = typ if isinstance(typ, BaseException) else typ()
        raise exc

    async def aclose(self) -> None:
        """Deregister from the bus and stop delivering events.

        Safe to call before any events have been read.
        """
        if not self._closed:
            self._closed = True
            self._shutdown.set()
            with contextlib.suppress(ValueError):
                self._bus_queues.remove(self._queue)


class EventBus:
    """In-process async pub/sub bus for WorldEventRecord fan-out.

    Each call to ``subscribe()`` registers a new asyncio.Queue-backed
    subscriber. Events published via ``publish()`` are delivered to every
    active subscriber. If a subscriber's queue is full when a new event
    arrives, the oldest buffered event is discarded and a ``bus.overflow``
    meta-event is delivered to all subscribers that still have capacity.
    """

    def __init__(self, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        """Initialize the EventBus.

        Args:
            max_queue_size: Maximum number of events buffered per subscriber
                before overflow handling is triggered.
        """
        self._max_queue_size = max_queue_size
        self._queues: list[asyncio.Queue[WorldEventRecord]] = []

    @property
    def subscriber_count(self) -> int:
        """Return the number of currently active subscribers."""
        return len(self._queues)

    @property
    def max_queue_size(self) -> int:
        """Return the configured maximum queue size per subscriber."""
        return self._max_queue_size

    async def publish(self, event: WorldEventRecord) -> None:
        """Publish an event to all active subscribers.

        If a subscriber's queue is at capacity, the oldest item is dropped
        and a ``bus.overflow`` meta-event is emitted to all subscribers with
        remaining capacity.

        Args:
            event: The WorldEventRecord to distribute.
        """
        overflow_count = 0

        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                    overflow_count += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

        if overflow_count > 0:
            log.warning(
                "bus_overflow",
                dropped_count=overflow_count,
                triggering_event_id=event.id,
            )
            await self._publish_meta(_make_overflow_event(overflow_count))

    async def _publish_meta(self, event: WorldEventRecord) -> None:
        """Deliver a meta-event to subscribers without triggering overflow.

        Queues that are full are silently skipped; meta-events are
        best-effort and must never cause further overflow.

        Args:
            event: The meta-event to deliver.
        """
        for queue in list(self._queues):
            if not queue.full():
                queue.put_nowait(event)

    def subscribe(self) -> AsyncGenerator[WorldEventRecord, None]:
        """Create a new subscription to the event stream.

        A new asyncio.Queue is registered immediately. The subscription
        remains active until the returned generator is closed, at which
        point the queue is automatically deregistered.

        Returns:
            An ``AsyncGenerator`` that yields WorldEventRecord objects in
            arrival order and supports ``aclose()`` for explicit cleanup.
        """
        queue: asyncio.Queue[WorldEventRecord] = asyncio.Queue(
            maxsize=self._max_queue_size,
        )
        self._queues.append(queue)
        return _Subscription(queue, self._queues)
