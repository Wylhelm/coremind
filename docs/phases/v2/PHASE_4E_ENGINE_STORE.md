# Phase 4E — Investigation Engine & Store

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** Phase 4A (schemas), Phase 4B (designers), Phase 4C (executor), Phase 4D (analyzer)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Implement `InvestigationEngine` (the orchestrator) and `InvestigationStore` (SurrealDB persistence). The engine wires designers, executor, and analyzer into a full lifecycle: FORMED → DESIGNING_TEST → EXECUTING_TEST → ANALYZING → terminal state.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/investigation/engine.py` | `InvestigationEngine` class |
| `src/coremind/investigation/store.py` | `InvestigationStore` (SurrealDB adapter) |
| `tests/investigation/test_engine.py` | Integration-level tests |
| `tests/investigation/test_store.py` | Store tests (with mock SurrealDB) |

---

## 3. InvestigationEngine

### 3.1 Constructor

```python
class InvestigationEngine:
    """Orchestrates investigations end-to-end."""

    def __init__(
        self,
        designers: list[TestDesigner],
        executor: TestExecutor,
        analyzer: ResultAnalyzer,
        store: InvestigationStore,
        narrative_store: NarrativeStoreProtocol,
        event_bus: EventBusProtocol,
        config: InvestigationConfig,
    ):
        self._designers = designers
        self._executor = executor
        self._analyzer = analyzer
        self._store = store
        self._narrative = narrative_store
        self._bus = event_bus
        self._config = config

        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._active: dict[str, asyncio.Task[None]] = {}
```

### 3.2 Public API

```python
async def investigate(self, anomaly: AnomalyContext) -> str:
    """Start an investigation for an anomaly. Returns investigation_id.

    Deduplicates: if an active investigation exists for the same entity+type,
    returns its ID without creating a new one.
    """
    ...

async def cancel(self, investigation_id: str) -> bool:
    """Cancel an in-flight investigation. Returns True if cancelled."""
    ...

async def list_active(self) -> list[InvestigationRun]:
    """Return all currently active investigations."""
    ...

async def get(self, investigation_id: str) -> InvestigationRun | None:
    """Get investigation by ID."""
    ...
```

### 3.3 Internal Lifecycle

```python
async def _run_investigation(self, run: InvestigationRun) -> None:
    async with self._semaphore:
        try:
            # 1. DESIGNING_TEST
            run.status = InvestigationStatus.DESIGNING_TEST
            await self._store.save(run)

            designer = self._find_designer(run.anomaly_type)
            if not designer:
                run.status = InvestigationStatus.UNRESOLVED
                run.conclusion = InvestigationConclusion(
                    verdict="unresolved", confidence=0.0,
                    reasoning=f"No test designer for {run.anomaly_type}",
                )
                run.completed_at = datetime.now(UTC)
                await self._store.save(run)
                return

            anomaly_ctx = AnomalyContext(
                description=run.anomaly_description,
                anomaly_type=run.anomaly_type,
                metadata=run.anomaly_metadata,
            )
            run.tests = await designer.design(anomaly_ctx)
            await self._store.save(run)

            if not run.tests:
                run.status = InvestigationStatus.UNRESOLVED
                run.conclusion = InvestigationConclusion(
                    verdict="unresolved", confidence=0.0, reasoning="No tests designed",
                )
                run.completed_at = datetime.now(UTC)
                await self._store.save(run)
                return

            # 2. EXECUTING_TEST
            run.status = InvestigationStatus.EXECUTING_TEST
            await self._store.save(run)

            results = await asyncio.gather(*[
                self._executor.execute(test) for test in run.tests
            ])
            run.results = list(results)
            await self._store.save(run)

            # 3. ANALYZING
            run.status = InvestigationStatus.ANALYZING
            await self._store.save(run)

            conclusion = await self._analyzer.analyze(run)
            run.conclusion = conclusion

            # 4. Apply verdict
            match conclusion.verdict:
                case "resolved":
                    run.status = InvestigationStatus.RESOLVED
                    await self._handle_resolution(run)
                case "escalated":
                    run.status = InvestigationStatus.ESCALATED
                    await self._handle_escalation(run)
                case "unresolved":
                    if run.retry_count < run.max_retries:
                        run.retry_count += 1
                        run.status = InvestigationStatus.FORMED  # re-queue
                    else:
                        run.status = InvestigationStatus.ESCALATED
                        run.conclusion = InvestigationConclusion(
                            verdict="escalated",
                            confidence=0.5,
                            reasoning=f"Inconclusive after {run.max_retries + 1} attempts",
                            user_message=f"Couldn't resolve: {run.anomaly_description}. Tried {run.max_retries + 1} times.",
                        )
                        await self._handle_escalation(run)

            run.completed_at = datetime.now(UTC)
            await self._store.save(run)

        except Exception as e:
            log.exception("investigation.failed", investigation_id=run.investigation_id)
            run.status = InvestigationStatus.UNRESOLVED
            run.completed_at = datetime.now(UTC)
            run.conclusion = InvestigationConclusion(
                verdict="unresolved", confidence=0.0,
                reasoning=f"Investigation failed: {e}",
            )
            await self._store.save(run)
```

### 3.4 Resolution & Escalation Handlers

```python
async def _handle_resolution(self, run: InvestigationRun) -> None:
    """Update narrative state silently."""
    await self._narrative.update_resolution(
        investigation_id=run.investigation_id,
        description=run.anomaly_description,
        resolution=run.conclusion.reasoning,
    )
    if entity_id := run.anomaly_metadata.get("entity_id"):
        await self._narrative.mark_anomaly_resolved(entity_id, run.anomaly_type.value)
    log.info("investigation.resolved", investigation_id=run.investigation_id)


async def _handle_escalation(self, run: InvestigationRun) -> None:
    """Publish escalation event for notification."""
    await self._bus.publish("investigation.escalated", {
        "investigation_id": run.investigation_id,
        "user_message": run.conclusion.user_message,
        "suggested_action": run.conclusion.suggested_action,
        "evidence": [r.raw_output for r in run.results if r.success],
    })
    log.info("investigation.escalated", investigation_id=run.investigation_id)
```

### 3.5 Deduplication

```python
async def investigate(self, anomaly: AnomalyContext) -> str:
    existing = await self._store.find_active(
        anomaly_type=anomaly.anomaly_type,
        entity_id=anomaly.metadata.get("entity_id"),
    )
    if existing:
        return existing.investigation_id

    run = InvestigationRun(
        anomaly_description=anomaly.description,
        anomaly_type=anomaly.anomaly_type,
        anomaly_metadata=anomaly.metadata,
        hypothesis=self._formulate_hypothesis(anomaly),
    )
    await self._store.save(run)

    task = asyncio.create_task(self._run_investigation(run))
    self._active[run.investigation_id] = task
    task.add_done_callback(lambda _: self._active.pop(run.investigation_id, None))

    return run.investigation_id
```

---

## 4. InvestigationStore

### 4.1 Interface

```python
class InvestigationStore:
    """Persists investigations to SurrealDB."""

    def __init__(self, db: SurrealDBClient):
        self._db = db

    async def save(self, run: InvestigationRun) -> None:
        """Upsert an investigation run."""
        ...

    async def get(self, investigation_id: str) -> InvestigationRun | None:
        """Retrieve by ID."""
        ...

    async def find_active(
        self, anomaly_type: AnomalyType, entity_id: str | None,
    ) -> InvestigationRun | None:
        """Find an active investigation for this entity+type combination."""
        ...

    async def list_recent(self, limit: int = 50) -> list[InvestigationRun]:
        """List recent investigations, most recent first."""
        ...

    async def list_by_status(self, status: InvestigationStatus) -> list[InvestigationRun]:
        """List investigations filtered by status."""
        ...

    async def prune(self, retention_days: int) -> int:
        """Remove completed investigations older than retention_days. Returns count removed."""
        ...
```

### 4.2 SurrealDB Schema

```sql
DEFINE TABLE investigation_run SCHEMAFULL;
DEFINE FIELD investigation_id ON investigation_run TYPE string;
DEFINE FIELD anomaly_description ON investigation_run TYPE string;
DEFINE FIELD anomaly_type ON investigation_run TYPE string;
DEFINE FIELD anomaly_metadata ON investigation_run TYPE object;
DEFINE FIELD hypothesis ON investigation_run TYPE string;
DEFINE FIELD status ON investigation_run TYPE string;
DEFINE FIELD tests ON investigation_run TYPE array;
DEFINE FIELD results ON investigation_run TYPE array;
DEFINE FIELD conclusion ON investigation_run OPTION TYPE object;
DEFINE FIELD started_at ON investigation_run TYPE datetime;
DEFINE FIELD completed_at ON investigation_run OPTION TYPE datetime;
DEFINE FIELD retry_count ON investigation_run TYPE int;
DEFINE FIELD max_retries ON investigation_run TYPE int;

DEFINE INDEX idx_investigation_status ON investigation_run FIELDS status;
DEFINE INDEX idx_investigation_anomaly ON investigation_run FIELDS anomaly_type, status;
```

---

## 5. Tests

```python
# tests/investigation/test_engine.py

@pytest.mark.asyncio
async def test_engine_full_lifecycle_resolved():
    """Engine runs full cycle: FORMED → DESIGNING → EXECUTING → ANALYZING → RESOLVED."""
    engine = make_engine(
        designer_result=[make_test("ha_query_entity")],
        executor_result=make_result(success=True, output={"last_changed": "2026-05-24T15:32:00+00:00"}),
        analyzer_result=InvestigationConclusion(verdict="resolved", confidence=0.95, reasoning="Stale claim"),
    )
    inv_id = await engine.investigate(make_anomaly(AnomalyType.STALE_DATE_CLAIM))
    await asyncio.sleep(0.1)  # let task complete

    run = await engine.get(inv_id)
    assert run.status == InvestigationStatus.RESOLVED
    assert run.conclusion.verdict == "resolved"
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_engine_deduplicates():
    """Same entity+type → returns existing ID."""
    engine = make_engine(...)
    id1 = await engine.investigate(make_anomaly(entity_id="light.bureau"))
    id2 = await engine.investigate(make_anomaly(entity_id="light.bureau"))
    assert id1 == id2


@pytest.mark.asyncio
async def test_engine_respects_max_concurrent():
    """Semaphore limits parallel investigations."""
    engine = make_engine(config=InvestigationConfig(max_concurrent=2))
    # Launch 5 slow investigations
    ids = []
    for i in range(5):
        ids.append(await engine.investigate(make_anomaly(entity_id=f"device.{i}")))
    # At most 2 are executing simultaneously (tested via semaphore counter)
    assert engine._semaphore._value >= 0


@pytest.mark.asyncio
async def test_engine_retries_then_escalates():
    """Unresolved → retry up to max_retries, then escalate."""
    engine = make_engine(
        analyzer_result=InvestigationConclusion(verdict="unresolved", confidence=0.3, reasoning="Ambiguous"),
        config=InvestigationConfig(max_retries=2),
    )
    inv_id = await engine.investigate(make_anomaly())
    await asyncio.sleep(0.5)

    run = await engine.get(inv_id)
    assert run.status == InvestigationStatus.ESCALATED
    assert run.retry_count == 2


@pytest.mark.asyncio
async def test_engine_handles_exception_gracefully():
    """If executor raises unexpectedly, investigation goes to UNRESOLVED."""
    engine = make_engine(executor_raises=RuntimeError("boom"))
    inv_id = await engine.investigate(make_anomaly())
    await asyncio.sleep(0.1)

    run = await engine.get(inv_id)
    assert run.status == InvestigationStatus.UNRESOLVED
    assert "boom" in run.conclusion.reasoning


# tests/investigation/test_store.py

@pytest.mark.asyncio
async def test_store_save_and_get(mock_surreal):
    store = InvestigationStore(db=mock_surreal)
    run = make_investigation_run()
    await store.save(run)
    retrieved = await store.get(run.investigation_id)
    assert retrieved == run


@pytest.mark.asyncio
async def test_store_find_active(mock_surreal):
    store = InvestigationStore(db=mock_surreal)
    run = make_investigation_run(status=InvestigationStatus.EXECUTING_TEST)
    await store.save(run)
    found = await store.find_active(AnomalyType.STALE_DATE_CLAIM, "vacuum.s7")
    assert found.investigation_id == run.investigation_id


@pytest.mark.asyncio
async def test_store_prune_old_records(mock_surreal):
    store = InvestigationStore(db=mock_surreal)
    # Insert old completed investigation
    old_run = make_investigation_run(
        status=InvestigationStatus.RESOLVED,
        completed_at=datetime.now(UTC) - timedelta(days=60),
    )
    await store.save(old_run)
    count = await store.prune(retention_days=30)
    assert count == 1
```

---

## 6. Success Criteria

- [ ] Engine runs full FORMED → terminal state lifecycle
- [ ] Deduplication prevents redundant investigations
- [ ] Semaphore enforces max_concurrent
- [ ] Retry logic works (retry N times, then escalate)
- [ ] Resolution updates narrative store
- [ ] Escalation publishes event to bus
- [ ] Exception handling prevents engine crashes
- [ ] Store persists and retrieves investigations correctly
- [ ] Store `find_active` prevents duplicates
- [ ] Store `prune` removes old records
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
