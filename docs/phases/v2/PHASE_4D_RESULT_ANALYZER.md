# Phase 4D — Result Analyzer

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** Phase 4A (schemas)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement `ResultAnalyzer` — the component that takes an `InvestigationRun` (with test results) and produces an `InvestigationConclusion`. Most analysis is rule-based per anomaly type. An LLM fallback handles unhandled types.

This component is pure logic over data (no I/O except the optional LLM fallback), so it is highly testable.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/investigation/analyzer.py` | `ResultAnalyzer` class |
| `tests/investigation/test_analyzer.py` | Unit tests (one per verdict path) |

---

## 3. Architecture

```python
class ResultAnalyzer:
    """Analyzes investigation test results and produces a conclusion."""

    def __init__(self, llm_client: LLMClientProtocol | None = None):
        self._llm = llm_client
        self._analyzers: dict[AnomalyType, Callable[..., Awaitable[InvestigationConclusion]]] = {
            AnomalyType.STALE_DATE_CLAIM: self._analyze_stale_date,
            AnomalyType.DEVICE_UNAVAILABLE: self._analyze_unavailable,
            AnomalyType.DATA_ANOMALY_NUMERIC: self._analyze_numeric,
            AnomalyType.MISSING_DATA: self._analyze_missing_data,
            AnomalyType.PATTERN_CHANGE: self._analyze_pattern,
        }

    async def analyze(self, investigation: InvestigationRun) -> InvestigationConclusion:
        """Produce a conclusion from the investigation's test results."""
        analyzer = self._analyzers.get(investigation.anomaly_type)
        if analyzer:
            return await analyzer(investigation)
        if self._llm:
            return await self._llm_analyze(investigation)
        return InvestigationConclusion(
            verdict="unresolved",
            confidence=0.3,
            reasoning=f"No analyzer for anomaly type {investigation.anomaly_type}",
        )
```

---

## 4. Per-Type Analyzers

### 4.1 `_analyze_stale_date`

Logic:
1. If no results → `unresolved`
2. If test failed → `unresolved`
3. Extract `actual_date` from result, compare to `claimed_date`
4. If `actual_date > claimed_date` → `resolved` (claim was stale)
5. If `actual_date <= claimed_date` → `escalated` (anomaly confirmed)

```python
async def _analyze_stale_date(self, inv: InvestigationRun) -> InvestigationConclusion:
    if not inv.results:
        return InvestigationConclusion(verdict="unresolved", confidence=0.0, reasoning="No test results")

    result = inv.results[0]
    if not result.success:
        return InvestigationConclusion(verdict="unresolved", confidence=0.2, reasoning=f"Test failed: {result.error}")

    attribute = inv.anomaly_metadata.get("attribute", "last_changed")
    actual_date_str = result.raw_output.get(attribute)
    claimed_date_str = inv.anomaly_metadata.get("claimed_date")

    if not actual_date_str or not claimed_date_str:
        return InvestigationConclusion(verdict="unresolved", confidence=0.3, reasoning="Could not extract dates")

    actual_date = datetime.fromisoformat(actual_date_str.replace("Z", "+00:00"))
    claimed_date = datetime.fromisoformat(claimed_date_str.replace("Z", "+00:00"))

    if actual_date > claimed_date:
        return InvestigationConclusion(
            verdict="resolved",
            confidence=0.95,
            reasoning=f"Claim was stale. Actual {attribute}: {actual_date.isoformat()}. Claim said: {claimed_date.isoformat()}.",
        )
    else:
        days_ago = (datetime.now(UTC) - actual_date).days
        return InvestigationConclusion(
            verdict="escalated",
            confidence=0.9,
            reasoning=f"Anomaly confirmed. Actual {attribute}: {actual_date.isoformat()} ({days_ago} days ago).",
            user_message=f"Confirmed: {inv.anomaly_description}. Last activity: {actual_date.isoformat()} ({days_ago} days ago).",
            suggested_action="Investigate why no activity has occurred",
        )
```

### 4.2 `_analyze_unavailable`

Logic:
1. Need at least 2 results (availability + last_seen)
2. If device is now available → `resolved`
3. If still unavailable, compute hours offline → `escalated`

```python
async def _analyze_unavailable(self, inv: InvestigationRun) -> InvestigationConclusion:
    if len(inv.results) < 2:
        return InvestigationConclusion(verdict="unresolved", confidence=0.2, reasoning="Insufficient results")

    availability = inv.results[0]
    last_seen = inv.results[1]

    if availability.success and availability.raw_output.get("available"):
        return InvestigationConclusion(
            verdict="resolved",
            confidence=0.95,
            reasoning=f"Device is now available. State: {availability.raw_output.get('current_state')}",
        )

    last_valid_at = last_seen.raw_output.get("last_valid_at")
    if last_valid_at:
        last_dt = datetime.fromisoformat(last_valid_at.replace("Z", "+00:00"))
        hours_offline = (datetime.now(UTC) - last_dt).total_seconds() / 3600
        entity_id = inv.anomaly_metadata.get("entity_id", "unknown")
        return InvestigationConclusion(
            verdict="escalated",
            confidence=0.9,
            reasoning=f"Device offline for {hours_offline:.1f}h since {last_valid_at}",
            user_message=f"Device {entity_id} offline for {hours_offline:.1f} hours (since {last_valid_at}).",
            suggested_action="Check power, network, or restart the device",
        )

    return InvestigationConclusion(
        verdict="escalated",
        confidence=0.7,
        reasoning="Device unavailable, no historical state found",
        user_message=f"Device {inv.anomaly_metadata.get('entity_id')} is unavailable with no recent valid state.",
    )
```

### 4.3 `_analyze_numeric`

Logic:
1. Need baseline result + re-query result
2. If `|z_score| > 3` → `escalated` (confirmed anomaly)
3. If `|z_score| <= 3` → `resolved` (within normal range)

```python
async def _analyze_numeric(self, inv: InvestigationRun) -> InvestigationConclusion:
    if len(inv.results) < 2:
        return InvestigationConclusion(verdict="unresolved", confidence=0.2, reasoning="Insufficient results")

    baseline = inv.results[0].raw_output
    z_score = baseline.get("z_score")
    observed = baseline.get("observed_value")

    if z_score is None:
        return InvestigationConclusion(verdict="unresolved", confidence=0.3, reasoning="Could not compute z-score")

    if abs(z_score) > 3:
        return InvestigationConclusion(
            verdict="escalated",
            confidence=0.9,
            reasoning=f"Confirmed anomaly. Observed: {observed}, baseline: {baseline['mean']:.1f} ± {baseline['stdev']:.1f}, z={z_score:.2f}",
            user_message=f"Anomaly confirmed: value {observed} is {abs(z_score):.1f}σ from baseline ({baseline['mean']:.1f} ± {baseline['stdev']:.1f}).",
        )
    else:
        return InvestigationConclusion(
            verdict="resolved",
            confidence=0.9,
            reasoning=f"Value {observed} within normal range (z={z_score:.2f}, baseline {baseline['mean']:.1f} ± {baseline['stdev']:.1f})",
        )
```

### 4.4 `_analyze_missing_data`

Logic:
1. If force-poll succeeded → `resolved`
2. If plugin is dead → `escalated`
3. If error rate > 50% → `escalated` (degraded)
4. Otherwise → `unresolved` (transient)

### 4.5 `_analyze_pattern`

Logic:
1. If `highest_similarity > 0.9` → `resolved` (not truly anomalous)
2. If `highest_similarity < 0.5` → `escalated` (genuinely unusual)
3. Otherwise → `unresolved` (ambiguous)

---

## 5. LLM Fallback

For anomaly types without a rule-based analyzer, build a structured prompt and parse the LLM response:

```python
async def _llm_analyze(self, inv: InvestigationRun) -> InvestigationConclusion:
    """Fallback to LLM analysis for unhandled anomaly types."""
    prompt = self._build_llm_prompt(inv)
    # Uses structured output via LLM.complete_structured()
    return await self._llm.complete_structured(
        prompt=prompt,
        response_model=InvestigationConclusion,
    )
```

---

## 6. Tests

```python
# tests/investigation/test_analyzer.py

@pytest.mark.asyncio
async def test_stale_date_resolved_when_actual_newer():
    inv = make_investigation(
        anomaly_type=AnomalyType.STALE_DATE_CLAIM,
        metadata={"claimed_date": "2026-05-17T00:00:00+00:00", "attribute": "last_changed"},
        results=[make_result(success=True, output={"last_changed": "2026-05-24T15:32:00+00:00"})],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "resolved"
    assert conclusion.confidence >= 0.9


@pytest.mark.asyncio
async def test_stale_date_escalated_when_anomaly_confirmed():
    inv = make_investigation(
        anomaly_type=AnomalyType.STALE_DATE_CLAIM,
        metadata={"claimed_date": "2026-05-17T00:00:00+00:00", "attribute": "last_changed"},
        results=[make_result(success=True, output={"last_changed": "2026-05-16T00:00:00+00:00"})],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "escalated"
    assert conclusion.user_message is not None


@pytest.mark.asyncio
async def test_unavailable_resolved_when_device_back():
    inv = make_investigation(
        anomaly_type=AnomalyType.DEVICE_UNAVAILABLE,
        metadata={"entity_id": "light.bureau"},
        results=[
            make_result(success=True, output={"available": True, "current_state": "on"}),
            make_result(success=True, output={"last_valid_at": "2026-05-26T10:00:00+00:00"}),
        ],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "resolved"


@pytest.mark.asyncio
async def test_unavailable_escalated_when_still_offline():
    inv = make_investigation(
        anomaly_type=AnomalyType.DEVICE_UNAVAILABLE,
        metadata={"entity_id": "light.bureau"},
        results=[
            make_result(success=True, output={"available": False, "current_state": "unavailable"}),
            make_result(success=True, output={"last_valid_at": "2026-05-23T10:00:00+00:00"}),
        ],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "escalated"
    assert "offline" in conclusion.reasoning


@pytest.mark.asyncio
async def test_numeric_resolved_within_baseline():
    inv = make_investigation(
        anomaly_type=AnomalyType.DATA_ANOMALY_NUMERIC,
        results=[
            make_result(success=True, output={"mean": 5000, "stdev": 1500, "observed_value": 4800, "z_score": -0.13}),
            make_result(success=True, output={"state": "4800"}),
        ],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "resolved"


@pytest.mark.asyncio
async def test_numeric_escalated_outside_baseline():
    inv = make_investigation(
        anomaly_type=AnomalyType.DATA_ANOMALY_NUMERIC,
        results=[
            make_result(success=True, output={"mean": 5000, "stdev": 500, "observed_value": 36, "z_score": -9.93}),
            make_result(success=True, output={"state": "36"}),
        ],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "escalated"


@pytest.mark.asyncio
async def test_pattern_resolved_high_similarity():
    inv = make_investigation(
        anomaly_type=AnomalyType.PATTERN_CHANGE,
        results=[make_result(success=True, output={"highest_similarity": 0.95, "similar_snapshots": []})],
    )
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "resolved"


@pytest.mark.asyncio
async def test_unknown_type_without_llm():
    inv = make_investigation(anomaly_type=AnomalyType.UNKNOWN, results=[])
    analyzer = ResultAnalyzer(llm_client=None)
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "unresolved"


@pytest.mark.asyncio
async def test_no_results_returns_unresolved():
    inv = make_investigation(anomaly_type=AnomalyType.STALE_DATE_CLAIM, results=[])
    analyzer = ResultAnalyzer()
    conclusion = await analyzer.analyze(inv)
    assert conclusion.verdict == "unresolved"
    assert conclusion.confidence == 0.0
```

---

## 7. Success Criteria

- [ ] All 5 rule-based analyzers implemented
- [ ] Each analyzer handles empty/failed results gracefully
- [ ] LLM fallback uses structured output (not free-form parsing)
- [ ] Every conclusion path has a test
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
