"""Dashboard HTTP server tests (Phase 4, Task 4.6)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application, Request

from coremind.action.schemas import Action
from coremind.dashboard import (
    DASHBOARD_DEFAULT_HOST,
    DASHBOARD_DEFAULT_PORT,
    DashboardAuth,
    DashboardDataSources,
    DashboardServer,
    JournalEntryView,
    StoredReflectionReport,
    create_app,
)
from coremind.intention.schemas import (
    ActionProposal,
    Intent,
    IntentStatus,
    InternalQuestion,
)
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import ApprovalAction, UserRef
from coremind.reasoning.schemas import (
    Anomaly,
    Pattern,
    Prediction,
    ReasoningOutput,
    TokenUsage,
)
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    ReflectionReport,
    RuleLearningResult,
)
from coremind.world.model import (
    Entity,
    EntityRef,
    Relationship,
    WorldEventRecord,
    WorldSnapshot,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWorld:
    def __init__(self, snapshot: WorldSnapshot, events: list[WorldEventRecord]) -> None:
        self._snapshot = snapshot
        self._events = events

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        return self._snapshot

    async def recent_events(
        self,
        since: datetime,
        limit: int = 500,
    ) -> list[WorldEventRecord]:
        return [e for e in self._events if e.timestamp > since][:limit]


class _FakeCycles:
    def __init__(self, cycles: list[ReasoningOutput]) -> None:
        self._cycles = cycles

    async def list_cycles(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ReasoningOutput]:
        items = list(self._cycles)
        items.sort(key=lambda c: c.timestamp, reverse=True)
        return items[:limit]

    async def get_cycle(self, cycle_id: str) -> ReasoningOutput | None:
        for cycle in self._cycles:
            if cycle.cycle_id == cycle_id:
                return cycle
        return None


class _FakeIntents:
    def __init__(self, intents: list[Intent]) -> None:
        self._intents = intents

    async def list(
        self,
        *,
        status: IntentStatus | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Intent]:
        items = list(self._intents)
        if status is not None:
            items = [i for i in items if i.status == status]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit]


class _FakeJournalEntry:
    def __init__(
        self,
        *,
        seq: int,
        kind: str,
        timestamp: datetime,
        payload: dict[str, object],
    ) -> None:
        self.seq = seq
        self.kind = kind
        self.timestamp = timestamp
        self.payload = payload


class _FakeJournal:
    def __init__(self, entries: list[_FakeJournalEntry]) -> None:
        self._entries = entries

    async def read_recent(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[JournalEntryView]:
        items = list(self._entries)
        if since is not None:
            items = [e for e in items if e.timestamp >= since]
        items.sort(key=lambda e: e.seq, reverse=True)
        return list(items[:limit])

    async def find_action(self, action_id: str) -> Action | None:
        return None


class _FakeReflection:
    def __init__(self, reports: list[StoredReflectionReport]) -> None:
        self._reports = reports

    async def list_reports(self, *, limit: int = 20) -> list[StoredReflectionReport]:
        items = list(self._reports)
        items.sort(key=lambda r: r.stored_at, reverse=True)
        return items[:limit]


class _ManualEventBus:
    """Minimal :class:`EventSubscriber` used to drive the SSE tests."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WorldEventRecord | None] = asyncio.Queue()

    async def push(self, event: WorldEventRecord) -> None:
        await self._queue.put(event)

    async def close(self) -> None:
        await self._queue.put(None)

    def subscribe(self) -> AsyncIterator[WorldEventRecord]:
        async def _gen() -> AsyncIterator[WorldEventRecord]:
            while True:
                event = await self._queue.get()
                if event is None:
                    return
                yield event

        return _gen()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_event(
    *,
    source: str = "test",
    entity_type: str = "person",
    entity_id: str = "alice",
    attribute: str = "status",
    value: str = "online",
    minutes_ago: int = 5,
) -> WorldEventRecord:
    return WorldEventRecord(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source=source,
        source_version="0.1.0",
        signature=None,
        entity=EntityRef(type=entity_type, id=entity_id),
        attribute=attribute,
        value=value,
        confidence=0.9,
    )


def _make_snapshot() -> WorldSnapshot:
    now = datetime.now(UTC)
    return WorldSnapshot(
        taken_at=now,
        entities=[
            Entity(
                type="person",
                display_name="Alice",
                created_at=now,
                updated_at=now,
                properties={"status": "online"},
                source_plugins=["test"],
            )
        ],
        relationships=[
            Relationship(
                type="knows",
                from_entity=EntityRef(type="person", id="alice"),
                to_entity=EntityRef(type="person", id="bob"),
                weight=0.75,
                created_at=now,
                last_reinforced=now,
            )
        ],
        recent_events=[],
    )


def _make_intent(
    *,
    text: str = "Should I do X?",
    status: IntentStatus = "pending",
    category: str = "ask",
) -> Intent:
    return Intent(
        id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        question=InternalQuestion(
            id=uuid.uuid4().hex,
            text=text,
            grounding=[EntityRef(type="person", id="alice")],
        ),
        proposed_action=ActionProposal(
            operation="coremind.test.noop",
            parameters={},
            expected_outcome="nothing",
            action_class="test",
        ),
        salience=0.6,
        confidence=0.8,
        category=category,  # type: ignore[arg-type]
        status=status,
    )


def _make_cycle(*, cycle_id: str = "c1") -> ReasoningOutput:
    entity = EntityRef(type="person", id="alice")
    return ReasoningOutput(
        cycle_id=cycle_id,
        timestamp=datetime.now(UTC),
        model_used="gpt-test",
        patterns=[
            Pattern(
                id="p1",
                description="weekly stand-up",
                entities_involved=[entity],
                confidence=0.8,
            )
        ],
        anomalies=[
            Anomaly(
                id="a1",
                description="missed sync",
                entity=entity,
                severity="medium",
                baseline_description="usually attends",
            )
        ],
        predictions=[
            Prediction(
                id="pr1",
                hypothesis="Will reply by tomorrow",
                horizon_hours=24,
                confidence=0.7,
                falsifiable_by="no reply seen by tomorrow EOD",
            )
        ],
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _make_report() -> StoredReflectionReport:
    now = datetime.now(UTC)
    report = ReflectionReport(
        cycle_id="reflect-1",
        window_start=now - timedelta(days=7),
        window_end=now,
        cycles_evaluated=3,
        intents_evaluated=2,
        actions_evaluated=1,
        predictions=PredictionEvaluationResult(
            evaluated=2,
            correct=1,
            wrong=1,
            undetermined=0,
        ),
        feedback=FeedbackEvaluationResult(
            evaluated=1,
            approved=1,
            rejected=0,
            reversed=0,
            dismissed=0,
        ),
        calibration=CalibrationResult(brier_score=0.25, sample_count=2),
        rules=RuleLearningResult(),
        markdown="# Weekly report\n\nAll quiet.",
    )
    return StoredReflectionReport(stored_at=now, report=report)


def _make_journal_entry(*, seq: int, operation: str, action_class: str) -> _FakeJournalEntry:
    return _FakeJournalEntry(
        seq=seq,
        kind="action",
        timestamp=datetime.now(UTC),
        payload={
            "id": uuid.uuid4().hex,
            "operation": operation,
            "action_class": action_class,
            "result": {"status": "ok"},
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_sources() -> DashboardDataSources:
    return DashboardDataSources(
        world=_FakeWorld(_make_snapshot(), [_make_event()]),
        cycles=_FakeCycles([_make_cycle()]),
        intents=_FakeIntents([_make_intent()]),
        journal=_FakeJournal(
            [
                _make_journal_entry(seq=1, operation="noop", action_class="test"),
                _make_journal_entry(seq=2, operation="other", action_class="hvac"),
            ]
        ),
        reflection=_FakeReflection([_make_report()]),
        notifications=DashboardNotificationPort(),
    )


_TEST_TOKEN = "test-token-of-sufficient-length"  # noqa: S105 — fixture, not a real credential.
_TEST_ORIGIN = "http://127.0.0.1:9900"


@pytest.fixture
def dashboard_auth() -> DashboardAuth:
    return DashboardAuth(
        api_token=_TEST_TOKEN,
        operator=UserRef(id="alice", display_name="Alice"),
        allowed_origins=(_TEST_ORIGIN,),
    )


@pytest.fixture
async def client(
    data_sources: DashboardDataSources,
    dashboard_auth: DashboardAuth,
) -> AsyncIterator[TestClient[Request, Application]]:
    app = create_app(data_sources, auth=dashboard_auth)
    async with TestClient(TestServer(app)) as client:
        yield client


def _approval_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TEST_TOKEN}",
        "Origin": _TEST_ORIGIN,
    }


# ---------------------------------------------------------------------------
# Page-rendering tests
# ---------------------------------------------------------------------------


async def test_overview_renders_counts(
    client: TestClient[Request, Application],
    data_sources: DashboardDataSources,
) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "Overview" in text
    assert "World entities" in text
    # Single seeded entity, single seeded relationship — assert against the
    # stable testid hooks rather than fragile substring matches like ">1<".
    assert 'data-testid="entity-count">1<' in text
    assert 'data-testid="relationship-count">1<' in text
    assert 'data-testid="pending-approvals">0<' in text


async def test_events_page_lists_initial_events(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/events")
    assert resp.status == 200
    text = await resp.text()
    assert "Live events" in text
    assert "person:alice" in text


async def test_graph_page_renders_entities_and_relationships(
    client: TestClient[Request, Application],
) -> None:
    resp = await client.get("/graph")
    assert resp.status == 200
    text = await resp.text()
    assert "Alice" in text
    assert "knows" in text


async def test_graph_json_returns_nodes_and_edges(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/api/graph")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["type"] == "person"
    assert len(data["edges"]) == 1
    assert data["edges"][0]["from"] == "person:alice"
    assert data["edges"][0]["to"] == "person:bob"


async def test_reasoning_page_lists_cycles(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/reasoning")
    assert resp.status == 200
    text = await resp.text()
    assert "c1" in text
    assert "gpt-test" in text


async def test_intents_page_lists_intents(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/intents")
    assert resp.status == 200
    text = await resp.text()
    assert "Should I do X?" in text
    assert "ask" in text


async def test_actions_page_lists_journal(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/actions")
    assert resp.status == 200
    text = await resp.text()
    assert "noop" in text
    assert "hvac" in text


async def test_actions_page_filters_by_query(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/actions", params={"q": "hvac"})
    assert resp.status == 200
    text = await resp.text()
    assert "hvac" in text
    assert "noop" not in text


async def test_reflection_page_renders_report(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/reflection")
    assert resp.status == 200
    text = await resp.text()
    assert "reflect-1" in text
    assert "Weekly report" in text


async def test_unknown_route_returns_404(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/does-not-exist")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


async def test_events_stream_pushes_published_events(
    data_sources: DashboardDataSources,
) -> None:
    bus = _ManualEventBus()
    sources = DashboardDataSources(
        world=data_sources.world,
        events=bus,
    )
    app = create_app(sources)
    async with TestClient(TestServer(app)) as client, client.get("/api/events/stream") as resp:
        assert resp.status == 200
        event = _make_event(attribute="heartbeat")
        await bus.push(event)
        await bus.close()
        chunks: list[str] = []
        async for raw, _ in resp.content.iter_chunks():
            chunks.append(raw.decode("utf-8"))
            if "heartbeat" in "".join(chunks):
                break
        payload = "".join(chunks)
        assert "event: event" in payload
        assert "heartbeat" in payload


async def test_events_stream_without_subscriber_closes_immediately() -> None:
    sources = DashboardDataSources()  # no event subscriber
    app = create_app(sources)
    async with TestClient(TestServer(app)) as client, client.get("/api/events/stream") as resp:
        assert resp.status == 200
        body = await resp.text()
        assert "event: end" in body


# ---------------------------------------------------------------------------
# Approval submission
# ---------------------------------------------------------------------------


async def test_submit_approval_forwards_to_dashboard_port(
    client: TestClient[Request, Application],
    data_sources: DashboardDataSources,
) -> None:
    assert data_sources.notifications is not None
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "intent-123", "decision": "approve"},
        headers=_approval_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}

    iterator = data_sources.notifications.subscribe_responses()
    response = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
    assert response.intent_id == "intent-123"
    assert response.decision == "approve"
    # Responder is the configured operator, not a hardcoded "dashboard".
    assert response.responder.id == "alice"
    assert response.responder.display_name == "Alice"


async def test_submit_approval_rejects_invalid_decision(
    client: TestClient[Request, Application],
) -> None:
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "yes"},
        headers=_approval_headers(),
    )
    assert resp.status == 400


async def test_submit_approval_rejects_invalid_json(
    client: TestClient[Request, Application],
) -> None:
    resp = await client.post(
        "/api/approvals",
        data="not-json",
        headers=_approval_headers(),
    )
    assert resp.status == 400


async def test_submit_approval_returns_503_without_adapter(
    dashboard_auth: DashboardAuth,
) -> None:
    app = create_app(DashboardDataSources(), auth=dashboard_auth)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/approvals",
            json={"intent_id": "i1", "decision": "approve"},
            headers=_approval_headers(),
        )
        assert resp.status == 503


# ---------------------------------------------------------------------------
# Auth & CSRF
# ---------------------------------------------------------------------------


async def test_submit_approval_requires_token(client: TestClient[Request, Application]) -> None:
    """No Authorization header → 401."""
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "approve"},
        headers={"Origin": _TEST_ORIGIN},
    )
    assert resp.status == 401


async def test_submit_approval_rejects_wrong_token(
    client: TestClient[Request, Application],
) -> None:
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "approve"},
        headers={"Authorization": "Bearer not-the-token", "Origin": _TEST_ORIGIN},
    )
    assert resp.status == 401


async def test_submit_approval_rejects_wrong_scheme(
    client: TestClient[Request, Application],
) -> None:
    """Basic / token / etc. schemes are rejected; only ``Bearer`` is accepted."""
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "approve"},
        headers={"Authorization": f"Basic {_TEST_TOKEN}", "Origin": _TEST_ORIGIN},
    )
    assert resp.status == 401


async def test_submit_approval_rejects_bad_origin(client: TestClient[Request, Application]) -> None:
    """Off-origin requests are CSRF-rejected even with a valid token."""
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "approve"},
        headers={
            "Authorization": f"Bearer {_TEST_TOKEN}",
            "Origin": "http://evil.example",
        },
    )
    assert resp.status == 403


async def test_submit_approval_rejects_missing_origin(
    client: TestClient[Request, Application],
) -> None:
    """A missing Origin/Referer is rejected — browsers always set Origin."""
    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "i1", "decision": "approve"},
        headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
    )
    assert resp.status == 403


async def test_submit_approval_503_when_auth_not_configured(
    data_sources: DashboardDataSources,
) -> None:
    """Without a DashboardAuth, the endpoint fails closed."""
    app = create_app(data_sources, auth=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/approvals",
            json={"intent_id": "i1", "decision": "approve"},
            headers=_approval_headers(),
        )
        assert resp.status == 503


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def test_dashboard_server_start_stop_uses_loopback_defaults(
    data_sources: DashboardDataSources,
) -> None:
    server = DashboardServer(data_sources, host=DASHBOARD_DEFAULT_HOST, port=0)
    assert server.host == DASHBOARD_DEFAULT_HOST
    # Default port surfaced for documentation / config consumers.
    assert DASHBOARD_DEFAULT_PORT == 9900
    await server.start()
    try:
        # Idempotent restart.
        await server.start()
    finally:
        await server.stop()
        await server.stop()  # also idempotent


async def test_empty_data_sources_renders_pages_without_error() -> None:
    app = create_app(DashboardDataSources())
    routes = ("/", "/events", "/graph", "/reasoning", "/intents", "/actions", "/reflection")
    async with TestClient(TestServer(app)) as client:
        for route in routes:
            resp = await client.get(route)
            assert resp.status == 200, route


# ---------------------------------------------------------------------------
# Output safety — XSS regression
# ---------------------------------------------------------------------------


async def test_events_page_escapes_html_in_event_fields(
    dashboard_auth: DashboardAuth,
) -> None:
    """An attacker-controlled event field must not break out of the cell.

    The dashboard renders plugin-supplied :class:`WorldEvent` data.  Without
    autoescape, a Gmail subject like ``<img src=x onerror=alert(1)>`` would
    execute JS in the dashboard origin — the only origin authorized to
    submit approvals.  We assert the raw payload never appears verbatim.
    """
    payload = "<img src=x onerror=alert(1)>"
    evil_event = WorldEventRecord(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC),
        source=payload,
        source_version="0.1.0",
        signature=None,
        entity=EntityRef(type="person", id=payload),
        attribute=payload,
        value=payload,
        confidence=0.5,
    )
    snapshot = WorldSnapshot(
        taken_at=datetime.now(UTC),
        entities=[],
        relationships=[],
        recent_events=[],
    )
    sources = DashboardDataSources(
        world=_FakeWorld(snapshot, [evil_event]),
        notifications=DashboardNotificationPort(),
    )
    app = create_app(sources, auth=dashboard_auth)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/events")
        text = await resp.text()
        # Raw HTML payload must not survive autoescape.
        assert payload not in text
        # The escaped form must be present (proves the field made it to the
        # template, just safely).
        assert "&lt;img src=x onerror=alert(1)&gt;" in text


async def test_events_stream_does_not_use_innerhtml() -> None:
    """The SSE client script must construct rows via textContent.

    Regression guard for the original ``row.innerHTML = `<td>${e.source}...```
    sink: any future contributor reintroducing it will trip this test.
    """
    sources = DashboardDataSources()
    app = create_app(sources)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/events")
        text = await resp.text()
        assert "innerHTML" not in text
        assert "textContent" in text


# ---------------------------------------------------------------------------
# Pending vs history
# ---------------------------------------------------------------------------


async def test_pending_drops_on_response_submission(
    client: TestClient[Request, Application],
    data_sources: DashboardDataSources,
) -> None:
    """``pending()`` shrinks when an approval response is submitted; the
    lifetime ``history`` does not.
    """
    port = data_sources.notifications
    assert port is not None

    await port.notify(
        message="approve hvac?",
        category="ask",
        actions=[ApprovalAction(label="Yes", value="approve")],
        intent_id="intent-xyz",
    )
    assert len(port.pending()) == 1
    assert len(port.history) == 1

    resp = await client.post(
        "/api/approvals",
        json={"intent_id": "intent-xyz", "decision": "approve"},
        headers=_approval_headers(),
    )
    assert resp.status == 200

    assert len(port.pending()) == 0
    assert len(port.history) == 1  # history is monotonic


async def test_overview_counter_uses_pending_not_history(
    client: TestClient[Request, Application],
    data_sources: DashboardDataSources,
) -> None:
    port = data_sources.notifications
    assert port is not None
    # Three notifications, two of them already resolved.
    for intent_id in ("a", "b", "c"):
        await port.notify(
            message=f"approve {intent_id}?",
            category="ask",
            actions=[ApprovalAction(label="Yes", value="approve")],
            intent_id=intent_id,
        )
    for intent_id in ("a", "b"):
        resp = await client.post(
            "/api/approvals",
            json={"intent_id": intent_id, "decision": "approve"},
            headers=_approval_headers(),
        )
        assert resp.status == 200

    overview = await client.get("/")
    text = await overview.text()
    # Only the unresolved intent is counted.
    assert 'data-testid="pending-approvals">1<' in text


async def test_info_notifications_are_never_pending() -> None:
    """``info``/``suggest`` notifications cannot be approved, so they must
    not inflate the pending counter even though they appear in history.
    """
    port = DashboardNotificationPort()
    await port.notify(
        message="fyi",
        category="info",
        actions=None,
        intent_id="intent-info",
    )
    assert port.pending() == []
    assert len(port.history) == 1


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


async def test_security_headers_set_on_pages(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


async def test_security_headers_set_on_api(client: TestClient[Request, Application]) -> None:
    resp = await client.get("/api/graph")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
