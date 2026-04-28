"""Tests for src/coremind/core/daemon.py."""

from __future__ import annotations

import asyncio
import signal
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from coremind.core.daemon import CoreMindDaemon, _handle_event
from coremind.core.event_bus import EventBus
from coremind.errors import SignatureError, StoreError
from coremind.world.model import EntityRef, WorldEventRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(signature: str | None = "ed25519:" + "a" * 128) -> WorldEventRecord:
    """Return a minimal WorldEventRecord for testing.

    The default signature value uses an arbitrary string.  Its exact format is
    irrelevant because ``_FakeStore`` does not verify signatures.
    """
    return WorldEventRecord(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC),
        source="plugin.test",
        source_version="1.0.0",
        signature=signature,
        entity=EntityRef(type="host", id="test-host"),
        attribute="cpu_percent",
        value=42.0,
        confidence=1.0,
    )


class _FakeStore:
    """In-memory test double for WorldStore.

    Supports configurable failure injection for the first N calls.
    """

    def __init__(
        self,
        fail_calls: int = 0,
        fail_with: type[Exception] = SignatureError,
    ) -> None:
        self.applied: list[WorldEventRecord] = []
        self._fail_remaining = fail_calls
        self._fail_with: type[Exception] = fail_with
        self.applied_event: asyncio.Event = asyncio.Event()

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Record *event* or raise the configured exception."""
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise self._fail_with("test-induced error")
        self.applied.append(event)
        self.applied_event.set()


# ---------------------------------------------------------------------------
# stop() without start() — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_idempotent_before_start() -> None:
    """stop() must complete without error even when start() was never called."""
    daemon = CoreMindDaemon()

    await daemon.stop()
    await daemon.stop()  # second call must also succeed


# ---------------------------------------------------------------------------
# _handle_event — per-event ingest logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_event_skips_unsigned_meta_events() -> None:
    """Events with signature=None are not forwarded to the store."""
    store = _FakeStore()

    await _handle_event(_make_event(signature=None), store)

    assert len(store.applied) == 0


@pytest.mark.asyncio
async def test_handle_event_persists_signed_event() -> None:
    """A signed event is passed to apply_event on the store."""
    store = _FakeStore()
    event = _make_event()

    await _handle_event(event, store)

    assert store.applied == [event]


@pytest.mark.asyncio
async def test_handle_event_does_not_raise_on_signature_error() -> None:
    """SignatureError from apply_event is swallowed so the loop can continue."""
    store = _FakeStore(fail_calls=1, fail_with=SignatureError)

    # Must not propagate the exception.
    await _handle_event(_make_event(), store)


@pytest.mark.asyncio
async def test_handle_event_does_not_raise_on_store_error() -> None:
    """StoreError from apply_event is swallowed so the loop can continue."""
    store = _FakeStore(fail_calls=1, fail_with=StoreError)

    # Must not propagate the exception.
    await _handle_event(_make_event(), store)


# ---------------------------------------------------------------------------
# _ingest_loop — integration with EventBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_loop_delivers_event_to_store() -> None:
    """An event published on the bus is applied to the world store."""
    bus = EventBus()
    store = _FakeStore()
    daemon = CoreMindDaemon()

    loop_task = asyncio.create_task(daemon._ingest_loop(bus, store))
    await asyncio.sleep(0)  # let the loop start waiting on the bus

    event = _make_event()
    await bus.publish(event)

    await asyncio.wait_for(store.applied_event.wait(), timeout=1.0)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert store.applied == [event]


@pytest.mark.asyncio
async def test_ingest_loop_skips_meta_events() -> None:
    """Meta-events (signature=None) published on the bus never reach the store."""
    bus = EventBus()
    store = _FakeStore()
    daemon = CoreMindDaemon()

    loop_task = asyncio.create_task(daemon._ingest_loop(bus, store))
    await asyncio.sleep(0)  # let the loop start waiting on the bus

    # Publish the meta-event, then a signed sentinel so we know when the loop
    # has processed both.  Waiting on the sentinel avoids relying on
    # asyncio.sleep(0) yield counts for synchronisation.
    await bus.publish(_make_event(signature=None))
    sentinel = _make_event()
    await bus.publish(sentinel)

    await asyncio.wait_for(store.applied_event.wait(), timeout=1.0)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    # Only the sentinel reached the store; the meta-event was skipped.
    assert store.applied == [sentinel]


@pytest.mark.asyncio
async def test_ingest_loop_continues_after_signature_error() -> None:
    """The loop processes a subsequent event after a SignatureError on the first."""
    bus = EventBus()
    # First call raises SignatureError; second call succeeds.
    store = _FakeStore(fail_calls=1, fail_with=SignatureError)
    daemon = CoreMindDaemon()

    loop_task = asyncio.create_task(daemon._ingest_loop(bus, store))
    await asyncio.sleep(0)

    await bus.publish(_make_event())  # will trigger SignatureError
    second = _make_event()
    await bus.publish(second)  # should succeed

    await asyncio.wait_for(store.applied_event.wait(), timeout=1.0)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert store.applied == [second]


@pytest.mark.asyncio
async def test_ingest_loop_continues_after_store_error() -> None:
    """The loop processes a subsequent event after a StoreError on the first."""
    bus = EventBus()
    store = _FakeStore(fail_calls=1, fail_with=StoreError)
    daemon = CoreMindDaemon()

    loop_task = asyncio.create_task(daemon._ingest_loop(bus, store))
    await asyncio.sleep(0)

    await bus.publish(_make_event())  # will trigger StoreError
    second = _make_event()
    await bus.publish(second)

    await asyncio.wait_for(store.applied_event.wait(), timeout=1.0)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert store.applied == [second]


# ---------------------------------------------------------------------------
# run_forever() — signal-driven shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_forever_stops_cleanly_on_signal() -> None:
    """run_forever() installs signal handlers and calls stop() when one fires.

    ``add_signal_handler`` is patched so no real OS signal is required.  The
    captured callback is invoked directly to simulate signal delivery.
    """
    daemon = CoreMindDaemon()
    loop = asyncio.get_running_loop()
    captured_handlers: dict[int, Callable[[], None]] = {}

    def _capture_handler(sig: int, cb: Callable[[], None]) -> None:
        captured_handlers[sig] = cb

    with (
        patch.object(daemon, "start", new_callable=AsyncMock),
        patch.object(daemon, "stop", new_callable=AsyncMock) as mock_stop,
        patch.object(loop, "add_signal_handler", side_effect=_capture_handler),
    ):
        run_task = asyncio.create_task(daemon.run_forever())
        # Two yields allow run_forever to reach ``await stop_event.wait()``.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert signal.SIGINT in captured_handlers, "SIGINT handler must be registered"
        # Simulate the signal arriving.
        captured_handlers[signal.SIGINT]()
        await asyncio.wait_for(run_task, timeout=3.0)

    mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# _approval_expirer_loop — periodic approval-expiration sweep
# ---------------------------------------------------------------------------


class _FakeApprovals:
    """Minimal ApprovalGate test double — counts ``expire_stale`` invocations."""

    def __init__(self, raise_first: bool = False) -> None:
        self.calls = 0
        self.tick: asyncio.Event = asyncio.Event()
        self._raise_first = raise_first

    async def expire_stale(self) -> int:
        self.calls += 1
        self.tick.set()
        if self._raise_first and self.calls == 1:
            raise RuntimeError("transient failure")
        return 0


@pytest.mark.asyncio
async def test_approval_expirer_loop_sweeps_until_stop_event() -> None:
    """The sweep runs at least once and exits cleanly when the stop event is set."""
    daemon = CoreMindDaemon()
    approvals = _FakeApprovals()

    task = asyncio.create_task(daemon._approval_expirer_loop(approvals))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(approvals.tick.wait(), timeout=1.0)
    finally:
        daemon._approval_expirer_stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert approvals.calls >= 1


@pytest.mark.asyncio
async def test_approval_expirer_loop_continues_after_exception() -> None:
    """A transient failure must not terminate the sweep loop."""
    daemon = CoreMindDaemon()
    approvals = _FakeApprovals(raise_first=True)

    task = asyncio.create_task(daemon._approval_expirer_loop(approvals))  # type: ignore[arg-type]
    try:
        # Wait until at least one sweep has executed (which raises).
        await asyncio.wait_for(approvals.tick.wait(), timeout=1.0)
    finally:
        daemon._approval_expirer_stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    # The task survived the raised exception — it ended only on stop.
    assert task.done()
    assert task.exception() is None
    assert approvals.calls >= 1


# ---------------------------------------------------------------------------
# _approved_dispatcher_loop — picks up CLI / channel approvals
# ---------------------------------------------------------------------------


class _FakeDispatcher:
    """Minimal ApprovalGate test double — counts ``dispatch_approved`` calls."""

    def __init__(self, raise_first: bool = False) -> None:
        self.calls = 0
        self.tick: asyncio.Event = asyncio.Event()
        self._raise_first = raise_first

    async def dispatch_approved(self) -> int:
        self.calls += 1
        self.tick.set()
        if self._raise_first and self.calls == 1:
            raise RuntimeError("transient")
        return 0


@pytest.mark.asyncio
async def test_approved_dispatcher_loop_runs_until_stop_event() -> None:
    """The dispatcher runs at least once and exits cleanly when the stop event is set."""
    daemon = CoreMindDaemon()
    approvals = _FakeDispatcher()

    task = asyncio.create_task(daemon._approved_dispatcher_loop(approvals))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(approvals.tick.wait(), timeout=1.0)
    finally:
        daemon._approved_dispatcher_stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert approvals.calls >= 1


@pytest.mark.asyncio
async def test_approved_dispatcher_loop_continues_after_exception() -> None:
    """A failed dispatch must not terminate the loop."""
    daemon = CoreMindDaemon()
    approvals = _FakeDispatcher(raise_first=True)

    task = asyncio.create_task(daemon._approved_dispatcher_loop(approvals))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(approvals.tick.wait(), timeout=1.0)
    finally:
        daemon._approved_dispatcher_stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert task.done()
    assert task.exception() is None
    assert approvals.calls >= 1
