"""Tests for the meta-loop dashboard views."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from coremind.dashboard.auth import DashboardAuth
from coremind.dashboard.data import DashboardDataSources
from coremind.dashboard.server import create_app
from coremind.meta.schemas import (
    AdjustmentRecord,
    MetaObservation,
    MetaStatus,
    ProposedAdjustment,
)
from coremind.notify.port import UserRef

# ---------------------------------------------------------------------------
# Fake MetaSource
# ---------------------------------------------------------------------------

_TEST_TOKEN = "test-token-16chars"
_TEST_ORIGIN = "http://localhost:9900"


class _FakeMetaSource:
    """In-memory MetaSource for testing."""

    def __init__(self) -> None:
        self.status = MetaStatus(enabled=True, observations_count=5, adjustments_count=1)
        self.observations: list[MetaObservation] = [
            MetaObservation(
                kind="intent_repeat_rate",
                value=0.42,
                threshold=0.30,
                window_seconds=21600.0,
                triggers_policy=True,
            ),
        ]
        self.adjustments: list[AdjustmentRecord] = []
        self.proposals: list[ProposedAdjustment] = []
        self.approved: list[str] = []
        self.denied: list[str] = []
        self.rolled_back: list[str] = []

    async def get_status(self) -> MetaStatus:
        return self.status

    async def list_observations(
        self,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MetaObservation]:
        obs = self.observations
        if kind:
            obs = [o for o in obs if o.kind == kind]
        return obs[:limit]

    async def list_adjustments(
        self,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AdjustmentRecord]:
        return self.adjustments[:limit]

    async def list_proposals(self) -> list[ProposedAdjustment]:
        return self.proposals

    async def approve_proposal(self, proposal_id: str) -> None:
        self.approved.append(proposal_id)

    async def deny_proposal(self, proposal_id: str) -> None:
        self.denied.append(proposal_id)

    async def rollback_adjustment(self, adjustment_id: str) -> None:
        self.rolled_back.append(adjustment_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_meta() -> _FakeMetaSource:
    return _FakeMetaSource()


@pytest.fixture
def dashboard_auth() -> DashboardAuth:
    return DashboardAuth(
        api_token=_TEST_TOKEN,
        operator=UserRef(id="op1", display_name="Operator"),
        allowed_origins=(_TEST_ORIGIN,),
    )


@pytest.fixture
async def client(
    fake_meta: _FakeMetaSource,
    dashboard_auth: DashboardAuth,
) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    sources = DashboardDataSources(meta=fake_meta)
    app = create_app(sources, auth=dashboard_auth)
    async with TestClient(TestServer(app)) as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TEST_TOKEN}",
        "Origin": _TEST_ORIGIN,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_page_renders(
    client: TestClient[web.Request, web.Application],
) -> None:
    """GET /meta returns 200 with HTML content."""
    resp = await client.get("/meta")
    assert resp.status == 200
    text = await resp.text()
    assert "Meta-Loop Status" in text
    assert "ENABLED" in text


@pytest.mark.asyncio
async def test_meta_status_json_returns_fields(
    client: TestClient[web.Request, web.Application],
) -> None:
    """GET /api/meta/status returns expected JSON fields."""
    resp = await client.get("/api/meta/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["enabled"] is True
    assert data["observations_count"] == 5
    assert data["adjustments_count"] == 1


@pytest.mark.asyncio
async def test_meta_observations_json(
    client: TestClient[web.Request, web.Application],
) -> None:
    """GET /api/meta/observations returns observation list."""
    resp = await client.get("/api/meta/observations")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["kind"] == "intent_repeat_rate"


@pytest.mark.asyncio
async def test_meta_observations_filter_by_kind(
    client: TestClient[web.Request, web.Application],
) -> None:
    """GET /api/meta/observations?kind=... filters correctly."""
    resp = await client.get("/api/meta/observations?kind=nonexistent")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 0


@pytest.mark.asyncio
async def test_meta_proposals_json_empty(
    client: TestClient[web.Request, web.Application],
) -> None:
    """GET /api/meta/proposals returns empty list when no proposals."""
    resp = await client.get("/api/meta/proposals")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


@pytest.mark.asyncio
async def test_meta_approve_requires_auth(
    client: TestClient[web.Request, web.Application],
) -> None:
    """POST /api/meta/proposals/{id}/approve without token returns 401."""
    resp = await client.post("/api/meta/proposals/abc/approve")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_meta_approve_with_auth(
    client: TestClient[web.Request, web.Application],
    fake_meta: _FakeMetaSource,
) -> None:
    """POST /api/meta/proposals/{id}/approve with valid auth succeeds."""
    resp = await client.post(
        "/api/meta/proposals/abc/approve",
        headers=_auth_headers(),
    )
    assert resp.status == 200
    assert "abc" in fake_meta.approved


@pytest.mark.asyncio
async def test_meta_deny_with_auth(
    client: TestClient[web.Request, web.Application],
    fake_meta: _FakeMetaSource,
) -> None:
    """POST /api/meta/proposals/{id}/deny with valid auth succeeds."""
    resp = await client.post(
        "/api/meta/proposals/xyz/deny",
        headers=_auth_headers(),
    )
    assert resp.status == 200
    assert "xyz" in fake_meta.denied


@pytest.mark.asyncio
async def test_meta_rollback_requires_auth(
    client: TestClient[web.Request, web.Application],
) -> None:
    """POST /api/meta/adjustments/{id}/rollback without token returns 401."""
    resp = await client.post("/api/meta/adjustments/adj1/rollback")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_meta_rollback_with_auth(
    client: TestClient[web.Request, web.Application],
    fake_meta: _FakeMetaSource,
) -> None:
    """POST /api/meta/adjustments/{id}/rollback with valid auth succeeds."""
    resp = await client.post(
        "/api/meta/adjustments/adj1/rollback",
        headers=_auth_headers(),
    )
    assert resp.status == 200
    assert "adj1" in fake_meta.rolled_back


@pytest.mark.asyncio
async def test_meta_page_without_source() -> None:
    """GET /meta returns graceful 'not configured' when no meta source."""
    sources = DashboardDataSources()
    app = create_app(sources)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/meta")
        assert resp.status == 200
        text = await resp.text()
        assert "not configured" in text


@pytest.mark.asyncio
async def test_meta_status_json_without_source() -> None:
    """GET /api/meta/status returns 503 when no meta source."""
    sources = DashboardDataSources()
    app = create_app(sources)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/api/meta/status")
        assert resp.status == 503
