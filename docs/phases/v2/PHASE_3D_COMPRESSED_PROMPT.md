# Phase 3D — Compressed Prompt Builder & Pipeline

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_EMBEDDING_WORLD.md](PHASE_3_EMBEDDING_WORLD.md)
**Prerequisites:** Phase 3A (SnapshotDiffer), Phase 3B (EmbeddingEncoder), Phase 3C (SnapshotMemory)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Create the `CompressedPromptBuilder` and `WorldEncodingPipeline` that orchestrate the differ, encoder, and memory into a single `process()` call. This is the integration layer that produces a `CompressedPrompt` ready for L4/L5 consumption.

Includes graceful fallback to full-text snapshots when the embedding service is down.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/world/compressed_prompt.py` | `CompressedPrompt`, `CompressedPromptBuilder` |
| `src/coremind/world/pipeline.py` | `WorldEncodingPipeline` |
| `tests/world/test_compressed_prompt.py` | Builder unit tests |
| `tests/world/test_pipeline.py` | Pipeline integration tests (mocked deps) |

---

## 3. Data Model

```python
class CompressedPrompt(BaseModel):
    """Compact world-state representation for LLM consumption."""

    summary: str                      # "48 entities (3 changed)"
    changes_text: str                 # Human-readable diff
    similar_states_text: str          # Top-K similar past states
    key_metrics_text: str             # Summary statistics
    full_fallback: str | None = None  # Full text if embedding fails

    @property
    def estimated_tokens(self) -> int:
        """Rough estimate: 1 token ~ 4 chars."""
        return len(self.to_prompt_text()) // 4

    def to_prompt_text(self) -> str:
        """Render as final prompt text for LLM."""
        parts = [
            "## World State Summary",
            self.summary,
            "",
            "## Changes Since Last Cycle",
            self.changes_text,
            "",
        ]
        if self.similar_states_text:
            parts.extend([
                "## Similar Past States",
                self.similar_states_text,
                "",
            ])
        if self.key_metrics_text:
            parts.extend([
                "## Key Metrics",
                self.key_metrics_text,
            ])
        return "\n".join(parts)
```

---

## 4. CompressedPromptBuilder

```python
class CompressedPromptBuilder:
    """Builds compact prompts from diffs + similarity results."""

    def __init__(self, memory: SnapshotMemory, top_k: int = 3):
        self._memory = memory
        self._top_k = top_k

    async def build(
        self,
        snapshot: WorldSnapshot,
        diff: SnapshotDiff,
        snapshot_embedding: list[float],
    ) -> CompressedPrompt:
        summary = (
            f"{diff.total_current} entities, {diff.change_summary}. "
            f"Timestamp: {snapshot.timestamp.isoformat()}."
        )

        changes_text = self._format_changes(diff)

        similar = await self._memory.find_similar(snapshot_embedding, k=self._top_k)
        similar_text = self._format_similar(similar)

        metrics = self._compute_metrics(snapshot)
        metrics_text = self._format_metrics(metrics)

        return CompressedPrompt(
            summary=summary,
            changes_text=changes_text,
            similar_states_text=similar_text,
            key_metrics_text=metrics_text,
        )

    def _format_changes(self, diff: SnapshotDiff) -> str:
        if not diff.has_changes:
            return "No changes since last cycle."

        lines: list[str] = []
        for entity in diff.added:
            lines.append(f"+ {entity.entity_id}: {self._brief(entity)}")
        for entity in diff.removed:
            lines.append(f"- {entity.entity_id} (removed)")
        for old, new in diff.changed:
            lines.append(f"~ {new.entity_id}: {self._diff_attrs(old, new)}")
        return "\n".join(lines)

    def _brief(self, entity: Entity) -> str:
        """One-line entity summary (type + top 3 attributes)."""
        important = sorted(entity.attributes.items())[:3]
        attrs = ", ".join(f"{k}={v}" for k, v in important)
        return f"({entity.entity_type}) {attrs}"

    def _diff_attrs(self, old: Entity, new: Entity) -> str:
        """Show only changed attributes between old and new."""
        diffs: list[str] = []
        all_keys = set(old.attributes.keys()) | set(new.attributes.keys())
        for k in sorted(all_keys):
            if k in IGNORED_ATTRS:
                continue
            o = old.attributes.get(k)
            n = new.attributes.get(k)
            if o != n:
                diffs.append(f"{k}: {o} → {n}")
        return "; ".join(diffs)

    def _format_similar(self, similar: list[SimilarSnapshot]) -> str:
        if not similar:
            return ""
        lines: list[str] = []
        for s in similar:
            age = self._age_str(s.timestamp)
            lines.append(f"- {age} (similarity {s.score:.2f}): {s.summary}")
        return "\n".join(lines)

    def _compute_metrics(self, snapshot: WorldSnapshot) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "total_entities": len(snapshot.entities),
            "by_type": {},
        }
        for entity in snapshot.entities:
            t = entity.entity_type
            metrics["by_type"][t] = metrics["by_type"].get(t, 0) + 1
        return metrics

    def _format_metrics(self, metrics: dict[str, Any]) -> str:
        lines = [f"Total entities: {metrics['total_entities']}"]
        for entity_type, count in sorted(metrics["by_type"].items()):
            lines.append(f"- {entity_type}: {count}")
        return "\n".join(lines)

    def _age_str(self, timestamp: datetime) -> str:
        delta = datetime.now(UTC) - timestamp
        if delta.days > 0:
            return f"{delta.days}d ago"
        elif delta.total_seconds() > 3600:
            return f"{int(delta.total_seconds() / 3600)}h ago"
        else:
            return f"{int(delta.total_seconds() / 60)}m ago"
```

---

## 5. WorldEncodingPipeline

```python
class WorldEncodingPipeline:
    """Orchestrates encoder + differ + memory + prompt builder.

    Gracefully falls back to full text on encoder failure.
    """

    def __init__(
        self,
        encoder: EmbeddingEncoder,
        differ: SnapshotDiffer,
        memory: SnapshotMemory,
        prompt_builder: CompressedPromptBuilder,
    ):
        self._encoder = encoder
        self._differ = differ
        self._memory = memory
        self._prompt_builder = prompt_builder
        self._fallback_active = False
        self._previous_snapshot: WorldSnapshot | None = None

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    async def process(self, current: WorldSnapshot) -> CompressedPrompt:
        """Process a new snapshot. Returns a CompressedPrompt for L4/L5.

        Stores the embedding in Qdrant. Falls back to full text on error.
        """
        diff = self._differ.diff(current, self._previous_snapshot)

        try:
            embedding = await self._encoder.encode_snapshot(current)

            await self._memory.store(
                snapshot_id=current.snapshot_id,
                vector=embedding,
                summary=f"{diff.total_current} entities, {diff.change_summary}",
                entity_count=diff.total_current,
                timestamp=current.timestamp,
            )

            prompt = await self._prompt_builder.build(current, diff, embedding)

            if self._fallback_active:
                log.info("encoding.recovered")
                self._fallback_active = False

            self._previous_snapshot = current
            return prompt

        except EncoderError as e:
            if not self._fallback_active:
                log.warning("encoding.fallback_active", error=str(e))
                self._fallback_active = True

            self._previous_snapshot = current
            return self._build_fallback(current, diff)

    def _build_fallback(
        self, snapshot: WorldSnapshot, diff: SnapshotDiff
    ) -> CompressedPrompt:
        """V1-style full text fallback."""
        full_text = json.dumps(
            [e.model_dump() for e in snapshot.entities],
            indent=2,
            default=str,
        )
        return CompressedPrompt(
            summary=f"{diff.total_current} entities (embedding service unavailable)",
            changes_text=full_text,
            similar_states_text="",
            key_metrics_text="",
            full_fallback=full_text,
        )
```

---

## 6. Tests

```python
# tests/world/test_compressed_prompt.py

def test_compressed_prompt_to_text_includes_sections():
    prompt = CompressedPrompt(
        summary="48 entities, 3 changed",
        changes_text="~ light.bureau: state: off → on",
        similar_states_text="- 4h ago (similarity 0.94): 48 entities",
        key_metrics_text="Total entities: 48",
    )
    text = prompt.to_prompt_text()
    assert "## World State Summary" in text
    assert "## Changes Since Last Cycle" in text
    assert "## Similar Past States" in text
    assert "## Key Metrics" in text
    assert "light.bureau" in text

def test_compressed_prompt_omits_empty_sections():
    prompt = CompressedPrompt(
        summary="48 entities, no changes",
        changes_text="No changes since last cycle.",
        similar_states_text="",
        key_metrics_text="",
    )
    text = prompt.to_prompt_text()
    assert "## Similar Past States" not in text
    assert "## Key Metrics" not in text

def test_estimated_tokens():
    prompt = CompressedPrompt(
        summary="x" * 100,
        changes_text="y" * 200,
        similar_states_text="z" * 100,
        key_metrics_text="w" * 100,
    )
    # ~500 chars of content + section headers
    assert prompt.estimated_tokens > 100
    assert prompt.estimated_tokens < 500

@pytest.mark.asyncio
async def test_builder_format_changes_added():
    builder = CompressedPromptBuilder(memory=mock_memory, top_k=3)
    diff = SnapshotDiff(
        added=[make_entity("light.new", state="on")],
        total_current=10,
    )
    prompt = await builder.build(mock_snapshot, diff, [0.1] * 768)
    assert "+ light.new" in prompt.changes_text

@pytest.mark.asyncio
async def test_builder_format_changes_removed():
    builder = CompressedPromptBuilder(memory=mock_memory, top_k=3)
    diff = SnapshotDiff(
        removed=[make_entity("light.old")],
        total_current=10,
    )
    prompt = await builder.build(mock_snapshot, diff, [0.1] * 768)
    assert "- light.old (removed)" in prompt.changes_text


# tests/world/test_pipeline.py

@pytest.mark.asyncio
async def test_pipeline_normal_path():
    pipeline = make_pipeline(encoder_ok=True)
    result = await pipeline.process(make_snapshot(["a", "b"]))
    assert result.full_fallback is None
    assert not pipeline.fallback_active

@pytest.mark.asyncio
async def test_pipeline_fallback_on_encoder_error():
    pipeline = make_pipeline(encoder_ok=False)
    result = await pipeline.process(make_snapshot(["a", "b"]))
    assert result.full_fallback is not None
    assert pipeline.fallback_active

@pytest.mark.asyncio
async def test_pipeline_recovers_after_encoder_returns():
    encoder = ToggleableEncoder(fail=True)
    pipeline = make_pipeline(encoder=encoder)

    # First call: fallback
    r1 = await pipeline.process(make_snapshot(["a"]))
    assert pipeline.fallback_active

    # Encoder recovers
    encoder.fail = False

    # Second call: normal
    r2 = await pipeline.process(make_snapshot(["a", "b"]))
    assert not pipeline.fallback_active
    assert r2.full_fallback is None

@pytest.mark.asyncio
async def test_pipeline_stores_previous_for_diffing():
    pipeline = make_pipeline(encoder_ok=True)
    await pipeline.process(make_snapshot(["a"]))
    assert pipeline._previous_snapshot is not None

    # Second call should diff against first
    r2 = await pipeline.process(make_snapshot(["a", "b"]))
    assert "1 added" in r2.summary
```

---

## 7. Token Budget Validation

After implementation, verify the compressed prompt stays within budget:

```python
@pytest.mark.asyncio
async def test_compressed_prompt_under_3000_tokens():
    """A typical 48-entity snapshot with 3 changes should produce <3000 tokens."""
    pipeline = make_pipeline_with_real_builder()
    snapshot = make_realistic_snapshot(entity_count=48, changed_count=3)
    result = await pipeline.process(snapshot)
    assert result.estimated_tokens < 3000
```

---

## 8. Success Criteria

- [ ] `CompressedPrompt.to_prompt_text()` renders clean Markdown sections
- [ ] `CompressedPromptBuilder` formats added/removed/changed entities
- [ ] Similar states are retrieved and formatted with age + score
- [ ] Key metrics roll up entity counts by type
- [ ] `WorldEncodingPipeline.process()` orchestrates all components end-to-end
- [ ] Pipeline tracks `_previous_snapshot` for incremental diffing
- [ ] Fallback activates on `EncoderError` and produces full-text output
- [ ] Recovery from fallback is logged and resets state
- [ ] Typical prompts are <3000 estimated tokens
- [ ] All tests pass
- [ ] `mypy --strict` passes
- [ ] `ruff check` passes
