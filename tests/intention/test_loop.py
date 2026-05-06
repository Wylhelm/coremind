"""IntentionLoop integration test with mocked LLM and ports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.action.approvals import ApprovalGate
from coremind.action.executor import Executor
from coremind.action.journal import ActionJournal
from coremind.action.notification_journal import NotificationJournal
from coremind.action.router import ActionRouter
from coremind.action.schemas import Action, ActionResult
from coremind.core.event_bus import EventBus
from coremind.intention.loop import IntentionLoop, IntentionLoopConfig
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import (
    ActionProposal,
    InternalQuestion,
    QuestionBatch,
    RawIntent,
)
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.reasoning.schemas import ReasoningOutput
from coremind.world.model import EntityRef, WorldEventRecord, WorldSnapshot


class _StaticSnapshot:
    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        return WorldSnapshot(
            taken_at=at or datetime(2025, 1, 1, tzinfo=UTC),
            entities=[],
            recent_events=[],
        )


class _EmptyReasoning:
    async def list_cycles(
        self, since: datetime | None = None, limit: int = 50
    ) -> list[ReasoningOutput]:
        return []


class _FakeLLM:
    """Returns a pre-baked QuestionBatch."""

    def __init__(self, batch: QuestionBatch) -> None:
        self._batch = batch
        self.calls = 0

    async def complete_structured(
        self,
        layer: str,
        system: str,
        user: str,
        response_model,  # type: ignore[no-untyped-def]
        *,
        max_tokens: int | None = None,
    ):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._batch


class _FakeEffector:
    def __init__(self) -> None:
        self.calls: list[Action] = []

    async def invoke(self, action: Action) -> ActionResult:
        self.calls.append(action)
        return ActionResult(
            action_id=action.id,
            status="ok",
            completed_at=datetime(2025, 1, 1, tzinfo=UTC),
        )


async def test_run_cycle_produces_and_routes_intents(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _FakeEffector()
    executor = Executor(
        journal,
        intents,
        lambda _op: effector,
        notify_port=port,
        suggest_grace=timedelta(milliseconds=1),
    )
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)

    batch = QuestionBatch(
        questions=[
            RawIntent(
                question=InternalQuestion(id="q1", text="Should I turn on the light?"),
                proposed_action=ActionProposal(
                    operation="plugin.ha.turn_on",
                    parameters={"entity_id": "light.kitchen"},
                    action_class="light",
                    expected_outcome="on",
                ),
                # High model confidence → safe category.
                model_confidence=0.99,
                model_salience=0.9,
            ),
        ]
    )
    llm = _FakeLLM(batch)

    loop = IntentionLoop(
        _StaticSnapshot(),
        _EmptyReasoning(),
        intents,
        llm,  # type: ignore[arg-type]
        router,
        config=IntentionLoopConfig(interval_seconds=10),
    )
    created = await loop.run_cycle()

    assert len(created) == 1
    # Without procedural rule matches, the confidence blend caps at ~0.69,
    # so a high model_confidence lands in the ``suggest`` tier — then the
    # executor dispatches after the grace window.
    assert created[0].category == "suggest"
    assert len(effector.calls) == 1
    saved = await intents.get(created[0].id)
    assert saved is not None
    assert saved.status == "done"


async def test_forced_class_in_loop_is_routed_to_ask(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _FakeEffector()
    executor = Executor(journal, intents, lambda _op: effector, notify_port=port)
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)

    # High confidence would normally route to safe — but action_class is forced.
    batch = QuestionBatch(
        questions=[
            RawIntent(
                question=InternalQuestion(id="q", text="Send email?"),
                proposed_action=ActionProposal(
                    operation="plugin.gmail.send",
                    parameters={},
                    action_class="email.outbound",
                ),
                model_confidence=0.99,
                model_salience=0.9,
            )
        ]
    )
    loop = IntentionLoop(
        _StaticSnapshot(),
        _EmptyReasoning(),
        intents,
        _FakeLLM(batch),  # type: ignore[arg-type]
        router,
    )
    created = await loop.run_cycle()
    assert len(created) == 1
    saved = await intents.get(created[0].id)
    assert saved is not None
    assert saved.category == "ask"
    assert saved.status == "pending_approval"
    # forced-class override meta was journaled
    text = (tmp_path / "audit.log").read_text()
    assert "security.category.override_blocked" in text
    assert effector.calls == []


# --- Event-driven mode tests ---


async def test_event_driven_mode_triggers_on_threshold(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    """Event-driven mode triggers a cycle when observation threshold is crossed."""
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _FakeEffector()
    executor = Executor(
        journal,
        intents,
        lambda _op: effector,
        notify_port=port,
        suggest_grace=timedelta(milliseconds=1),
        notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
    )
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)

    batch = QuestionBatch(
        questions=[
            RawIntent(
                question=InternalQuestion(id="q1", text="Should I turn on the light?"),
                proposed_action=ActionProposal(
                    operation="plugin.ha.turn_on",
                    parameters={"entity_id": "light.kitchen"},
                    action_class="light",
                    expected_outcome="on",
                ),
                model_confidence=0.99,
                model_salience=0.9,
            ),
        ]
    )
    llm = _FakeLLM(batch)

    event_bus = EventBus()
    loop = IntentionLoop(
        _StaticSnapshot(),
        _EmptyReasoning(),
        intents,
        llm,
        router,
        event_bus=event_bus,  # type: ignore[arg-type]
        config=IntentionLoopConfig(event_driven=True, routine_interval_seconds=60),
    )
    loop._observation_threshold = 3  # Small threshold for test
    loop.start()
    await asyncio.sleep(0.1)

    # Publish significant events to trigger the cycle
    for i in range(5):
        await event_bus.publish(
            WorldEventRecord(
                id=f"ev{i}",
                timestamp=datetime.now(UTC),
                source="test",
                source_version="1.0",
                signature=b"sig",
                entity=EntityRef(type="health", id=f"e{i}"),
                attribute="anomaly",
                value={"severity": "high"},
                confidence=0.9,
            )
        )
    await asyncio.sleep(0.3)

    await loop.stop()
    assert llm.calls >= 1, "Event-driven mode should trigger at least one cycle"


async def test_event_driven_mode_filters_insignificant_events(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    """Events with low confidence or irrelevant types should not trigger cycles."""
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    router = ActionRouter(
        Executor(
            journal,
            intents,
            lambda _op: _FakeEffector(),
            notify_port=port,
            notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
        ),
        ApprovalGate(
            port,
            intents,
            journal,
            Executor(
                journal,
                intents,
                lambda _op: _FakeEffector(),
                notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
            ),
        ),
        intents,
        journal,
    )

    llm = _FakeLLM(QuestionBatch(questions=[]))
    event_bus = EventBus()
    loop = IntentionLoop(
        _StaticSnapshot(),
        _EmptyReasoning(),
        intents,
        llm,
        router,
        event_bus=event_bus,  # type: ignore[arg-type]
        config=IntentionLoopConfig(event_driven=True, routine_interval_seconds=60),
    )
    loop._observation_threshold = 3
    loop.start()
    await asyncio.sleep(0.1)

    # Publish low-confidence events (should be filtered out)
    for i in range(10):
        await event_bus.publish(
            WorldEventRecord(
                id=f"low{i}",
                timestamp=datetime.now(UTC),
                source="test",
                source_version="1.0",
                signature=b"sig",
                entity=EntityRef(type="health", id=f"e{i}"),
                attribute="anomaly",
                value={},
                confidence=0.5,  # Below 0.7 threshold
            )
        )
    await asyncio.sleep(0.2)

    # Also publish irrelevant entity types
    for i in range(10):
        await event_bus.publish(
            WorldEventRecord(
                id=f"irr{i}",
                timestamp=datetime.now(UTC),
                source="test",
                source_version="1.0",
                signature=b"sig",
                entity=EntityRef(type="calendar", id=f"e{i}"),
                attribute="event",
                value={},
                confidence=0.9,
            )
        )

    await loop.stop()
    assert llm.calls == 0, "Irrelevant/low-confidence events should not trigger cycles"
