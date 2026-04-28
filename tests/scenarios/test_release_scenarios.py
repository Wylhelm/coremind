"""Golden-path end-to-end scenarios required for the v0.1.0 release.

Each scenario corresponds to one bullet of Phase 4 task 4.11 in
``docs/phases/PHASE_4_REFLECTION_ECOSYSTEM.md``.  These tests are wired with
in-memory adapters only — they require neither SurrealDB nor Qdrant nor any
real LLM backend — so CI can run them on every PR before tagging a release.

Mark: ``e2e``.  Excluded from ``just test`` (unit run); selected by
``just test-scenarios`` and the release job in ``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc.aio
import pytest
from click.testing import CliRunner
from coremind_plugin_systemstats.main import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    build_signed_event,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.action.journal import ActionJournal
from coremind.action.schemas import Action
from coremind.cli import cli
from coremind.config import DaemonConfig, NotifyConfig, QuietHoursConfig, TelegramConfig
from coremind.core.daemon import CoreMindDaemon
from coremind.core.event_bus import EventBus
from coremind.errors import LLMError, ReasoningError
from coremind.intention.salience import score_confidence
from coremind.intention.schemas import (
    ActionProposal,
    InternalQuestion,
    RawIntent,
)
from coremind.memory.procedural import ProceduralMemory, Rule
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind.plugin_host.registry import PluginRegistry
from coremind.plugin_host.server import PluginHostServer
from coremind.reasoning.llm import LLM, CompletionResult, LayerConfig, LLMConfig
from coremind.reasoning.loop import ReasoningLoop
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.reasoning.schemas import ReasoningOutput
from coremind.world.model import (
    Entity,
    EntityRef,
    WorldEventRecord,
    WorldSnapshot,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


_PLUGIN_MANIFEST = plugin_pb2.PluginManifest(
    plugin_id=PLUGIN_ID,
    version=PLUGIN_VERSION,
    display_name="System Stats (scenarios)",
    kind=plugin_pb2.PLUGIN_KIND_SENSOR,
    provides_entities=["host"],
    emits_attributes=["cpu_percent", "memory_percent", "uptime_seconds"],
)


class _InMemoryStore:
    """WorldStore double that records every applied event and yields snapshots."""

    def __init__(self) -> None:
        self.events: list[WorldEventRecord] = []
        self._received: asyncio.Event = asyncio.Event()

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Append *event* and wake any waiter."""
        self.events.append(event)
        self._received.set()

    async def wait_for_count(self, count: int, max_wait: float = 5.0) -> None:
        """Block until ``count`` events have been stored."""
        async with asyncio.timeout(max_wait):
            while len(self.events) < count:
                self._received.clear()
                await self._received.wait()

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        """Build a snapshot from currently-recorded events."""
        now = at or datetime.now(UTC)
        entities: dict[tuple[str, str], Entity] = {}
        for ev in self.events:
            key = (ev.entity.type, ev.entity.id)
            entities[key] = Entity(
                type=ev.entity.type,
                display_name=ev.entity.id,
                created_at=ev.timestamp,
                updated_at=ev.timestamp,
                properties={ev.attribute: ev.value},
                source_plugins=[ev.source],
            )
        return WorldSnapshot(
            taken_at=now,
            entities=list(entities.values()),
            recent_events=list(self.events[-50:]),
        )


class _ScriptedBackend:
    """LLM backend that returns a fixed JSON payload on every call."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, str] | None,
        api_key: str | None,
    ) -> CompletionResult:
        """Return the canned payload, recording call count."""
        self.calls += 1
        return CompletionResult(
            content=json.dumps(self._payload),
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        )


class _FlakyBackend:
    """Backend that raises ``LLMError`` on the first call, then succeeds."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, str] | None,
        api_key: str | None,
    ) -> CompletionResult:
        """Fail once, then return the canned payload."""
        self.calls += 1
        if self.calls == 1:
            raise LLMError("simulated provider outage")
        return CompletionResult(
            content=json.dumps(self._payload),
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        )


def _valid_cycle_payload() -> dict[str, Any]:
    """Return a minimal payload that validates against ReasoningOutput."""
    return {
        "cycle_id": "placeholder",
        "timestamp": datetime.now(UTC).isoformat(),
        "model_used": "placeholder",
        "patterns": [],
        "anomalies": [],
        "predictions": [],
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _simulated_event(seq: int, base: datetime) -> WorldEventRecord:
    """Build a signed-shape (signature non-None) event for ingest."""
    return WorldEventRecord(
        id=f"sim-{seq:04d}",
        timestamp=base + timedelta(minutes=seq),
        source="coremind.plugin.systemstats",
        source_version="0.1.0",
        signature="sim-signature",  # store double does not validate
        entity=EntityRef(type="host", id="laptop"),
        attribute="cpu_percent",
        value=10.0 + (seq % 40),
        confidence=0.95,
        unit="percent",
    )


# ---------------------------------------------------------------------------
# S1 — Cold start → 1 hour of simulated events → ≥ 1 reasoning cycle
# ---------------------------------------------------------------------------


async def test_s1_cold_start_produces_reasoning_cycle(tmp_path: Path) -> None:
    """Boot the ingest pipeline, stream 60 minutes of events, run one cycle."""
    bus = EventBus()
    store = _InMemoryStore()
    daemon = CoreMindDaemon()
    ingest = asyncio.create_task(daemon._ingest_loop(bus, store), name="s1.ingest")

    # Yield until the ingest task has registered its subscription on the bus.
    while bus.subscriber_count == 0:  # noqa: ASYNC110 — wait for peer task to register before publishing
        await asyncio.sleep(0)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        for i in range(60):
            await bus.publish(_simulated_event(i, base))
        await store.wait_for_count(60, max_wait=5.0)
    finally:
        ingest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest

    backend = _ScriptedBackend(_valid_cycle_payload())
    llm = LLM(LLMConfig(reasoning_heavy=LayerConfig(model="test/fake")), backend=backend)
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")
    loop = ReasoningLoop(snapshot_provider=store, memory=None, llm=llm, persister=persister)

    output = await loop.run_cycle()

    assert isinstance(output, ReasoningOutput)
    cycles = await persister.list_cycles(limit=5)
    assert len(cycles) == 1
    assert cycles[0].cycle_id == output.cycle_id
    assert backend.calls == 1


# ---------------------------------------------------------------------------
# S2 — Reflection → rule promoted → next reasoning cycle uses the rule
# ---------------------------------------------------------------------------


async def test_s2_promoted_rule_is_used_by_next_cycle(tmp_path: Path) -> None:
    """A rule promoted into procedural memory must influence the next cycle."""
    pm = ProceduralMemory(tmp_path / "procedural.jsonl")
    await pm.load()

    # Simulate a reflection-promoted rule entering procedural memory.
    rule = Rule(
        id="rule.cpu.high",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        description="Suggest a cooldown when CPU is sustained high.",
        trigger={
            "conditions": [{"field": "cpu_percent", "op": "gte", "value": 80}],
        },
        action={"operation": "host.cooldown_hint", "parameters": {}},
        confidence=0.8,
        source="reflection",
    )
    await pm.add(rule)

    # The next cycle's matcher reflects the new rule.
    matched = await pm.match({"cpu_percent": 92})
    assert any(r.id == rule.id for r in matched), "promoted rule must match its trigger"

    raw = RawIntent(
        question=InternalQuestion(id="q", text="Should I cool the host down?"),
        proposed_action=ActionProposal(
            operation="host.cooldown_hint",
            parameters={},
            action_class="host",
        ),
        model_confidence=0.4,
        model_salience=0.4,
    )
    confidence_without = score_confidence(raw, matching_rules=0)
    confidence_with = score_confidence(raw, matching_rules=len(matched))
    assert confidence_with > confidence_without, (
        "matching procedural rule must boost confidence in the next cycle"
    )


# ---------------------------------------------------------------------------
# S3 — Tampered audit journal entry → ``coremind audit verify`` fails clearly
# ---------------------------------------------------------------------------


def _make_action(action_id: str = "a1") -> Action:
    """Build a minimal action used to seed the audit journal."""
    return Action(
        id=action_id,
        intent_id="i1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        category="safe",
        operation="plugin.x.op",
        parameters={"k": "v"},
        action_class="light",
        expected_outcome="on",
        confidence=0.95,
    )


def test_s3_audit_verify_detects_tampered_journal(
    tmp_path: Path,
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    """``audit verify`` exits non-zero with a clear error after tampering."""
    priv, pub = keypair
    audit_path = tmp_path / "audit.log"

    async def _seed() -> None:
        journal = ActionJournal(audit_path, priv, pub)
        await journal.load()
        await journal.append(_make_action("a1"))
        await journal.append(_make_action("a2"))

    asyncio.run(_seed())

    lines = audit_path.read_text().splitlines()
    lines[0] = lines[0].replace("plugin.x.op", "plugin.x.EVIL")
    audit_path.write_text("\n".join(lines) + "\n")

    cfg = DaemonConfig(
        intent_store_path=tmp_path / "intents.jsonl",
        audit_log_path=audit_path,
        notify=NotifyConfig(
            primary="dashboard",
            fallbacks=[],
            telegram=TelegramConfig(enabled=False, chat_id=""),
        ),
        quiet_hours=QuietHoursConfig(enabled=False),
    )

    runner = CliRunner(mix_stderr=False)
    from unittest.mock import patch  # noqa: PLC0415 — local import to scope the patch

    with patch("coremind.cli.load_config", return_value=cfg):
        result = runner.invoke(cli, ["audit", "verify"])

    assert result.exit_code != 0, result.stdout
    # The CLI surfaces either ``audit verify failed: ...`` (raised from
    # ``load()``) or ``journal broken at line N`` (raised from ``verify()``);
    # both are clear, line-pinpointed errors per the scenario contract.
    err = result.stderr
    assert "line 1" in err
    assert ("journal broken" in err) or ("audit verify failed" in err)


# ---------------------------------------------------------------------------
# S4 — Plugin disconnect (crash) → reconnect → events resume without loss
# ---------------------------------------------------------------------------


async def test_s4_plugin_reconnects_after_crash_without_data_loss(
    tmp_path: Path,
) -> None:
    """A plugin that disconnects can rejoin the host and resume emission.

    The supervisor / auto-restart wrapper is out of scope for v0.1.0
    (Phase 4 leaves it optional).  This scenario asserts the property that
    underlies it: the plugin host accepts a fresh client connection from a
    previously-registered plugin id, and events emitted after the
    reconnect arrive in the world store alongside the pre-crash events.
    """
    socket_path = tmp_path / "run" / "plugin_host.sock"
    bus = EventBus()
    store = _InMemoryStore()

    private_key = Ed25519PrivateKey.generate()
    registry = PluginRegistry()
    registry.register(_PLUGIN_MANIFEST, private_key.public_key())

    server = PluginHostServer(
        socket_path=socket_path,
        registry=registry,
        event_bus=bus,
        secrets_resolver=lambda _: None,
    )
    await server.start()

    daemon = CoreMindDaemon()
    ingest = asyncio.create_task(daemon._ingest_loop(bus, store), name="s4.ingest")

    while bus.subscriber_count == 0:  # noqa: ASYNC110 — wait for peer task to register before publishing
        await asyncio.sleep(0)

    metadata = (("x-plugin-id", PLUGIN_ID),)
    addr = f"unix://{socket_path}"

    try:
        # First connection — emits 1 event then "crashes" (closes).
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = plugin_pb2_grpc.CoreMindHostStub(channel)  # type: ignore[no-untyped-call]
            evt = build_signed_event(private_key, "cpu_percent", 50.0, "testhost")
            await stub.EmitEvent(evt, metadata=metadata)
        # Channel context exit simulates the plugin process crashing.

        # Second connection — same plugin id, post-restart emission.
        async with grpc.aio.insecure_channel(addr) as channel:
            stub = plugin_pb2_grpc.CoreMindHostStub(channel)  # type: ignore[no-untyped-call]
            evt = build_signed_event(private_key, "memory_percent", 70.0, "testhost")
            await stub.EmitEvent(evt, metadata=metadata)

        await store.wait_for_count(2, max_wait=5.0)
    finally:
        ingest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest
        await server.stop()

    received = {ev.attribute for ev in store.events}
    assert received == {"cpu_percent", "memory_percent"}


# ---------------------------------------------------------------------------
# S5 — LLM provider fails mid-cycle → cycle aborts cleanly → next cycle ok
# ---------------------------------------------------------------------------


async def test_s5_llm_failure_then_recovery(tmp_path: Path) -> None:
    """A flaky LLM aborts one cycle but the next cycle succeeds and persists."""
    backend = _FlakyBackend(_valid_cycle_payload())
    llm = LLM(LLMConfig(reasoning_heavy=LayerConfig(model="test/fake")), backend=backend)
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")

    snapshot = WorldSnapshot(taken_at=datetime(2026, 1, 1, tzinfo=UTC))

    class _Provider:
        async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
            return snapshot

    loop = ReasoningLoop(snapshot_provider=_Provider(), memory=None, llm=llm, persister=persister)

    with pytest.raises(ReasoningError):
        await loop.run_cycle()

    cycles_after_failure = await persister.list_cycles(limit=5)
    assert cycles_after_failure == [], "failed cycle must not be persisted"

    output = await loop.run_cycle()

    assert output.cycle_id
    cycles_after_recovery = await persister.list_cycles(limit=5)
    assert len(cycles_after_recovery) == 1
    assert cycles_after_recovery[0].cycle_id == output.cycle_id
    # Backend was hit twice: once raising, once succeeding (no internal retry
    # is triggered because LLMError propagates without retry per LLM.complete_structured).
    assert backend.calls == 2
