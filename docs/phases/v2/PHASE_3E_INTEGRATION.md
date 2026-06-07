# Phase 3E — Integration with L4/L5 & CLI

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_EMBEDDING_WORLD.md](PHASE_3_EMBEDDING_WORLD.md)
**Prerequisites:** Phase 3D (WorldEncodingPipeline produces CompressedPrompt)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Wire the `WorldEncodingPipeline` into the existing daemon lifecycle:

1. Initialize the pipeline in `daemon.py`
2. Replace full-snapshot prompts in L4 (Reasoning) and L5 (Intention) with `CompressedPrompt`
3. Add CLI commands for embed-stats and similar-snapshot queries
4. Add configuration loading from `~/.coremind/config.toml`

---

## 2. Deliverables

| File | Change |
| --- | --- |
| `src/coremind/core/config.py` | Add `EmbeddingConfig` |
| `src/coremind/core/daemon.py` | Initialize `WorldEncodingPipeline` |
| `src/coremind/intention/prompts.py` | New template accepting `CompressedPrompt` |
| `src/coremind/reasoning/prompts.py` | New template accepting `CompressedPrompt` |
| `src/coremind/intention/loop.py` | Call pipeline, pass compressed prompt |
| `src/coremind/reasoning/loop.py` | Call pipeline, pass compressed prompt |
| `src/coremind/cli/world_commands.py` | `embed-stats`, `similar`, `prune` commands |
| `tests/world/test_integration.py` | End-to-end tests with mocked deps |

---

## 3. Configuration

### 3.1 Pydantic Config Model

```python
class EmbeddingConfig(BaseModel):
    """Configuration for the embedding world pipeline."""
    enabled: bool = True
    encoder_url: str = "http://OLLAMA_HOST:11434"
    encoder_model: str = "nomic-embed-text"
    cache_size: int = 5000
    timeout_seconds: float = 10.0
    qdrant_url: str = "http://localhost:6333"
    collection_name: str = "snapshot_embeddings"
    top_k_similar: int = 3
    exclude_recent_seconds: float = 600.0
    prune_keep_count: int = 1000
    prune_interval_seconds: float = 21600.0
    fallback_on_error: bool = True
```

### 3.2 TOML Section

```toml
[embedding]
enabled = true
encoder_url = "http://OLLAMA_HOST:11434"
encoder_model = "nomic-embed-text"
cache_size = 5000
timeout_seconds = 10.0
qdrant_url = "http://localhost:6333"
collection_name = "snapshot_embeddings"
top_k_similar = 3
exclude_recent_seconds = 600.0
prune_keep_count = 1000
prune_interval_seconds = 21600.0
fallback_on_error = true
```

---

## 4. Daemon Initialization

In `daemon.py`, during startup:

```python
async def _init_embedding_pipeline(self) -> WorldEncodingPipeline | None:
    """Initialize the embedding pipeline if enabled."""
    cfg = self._config.embedding
    if not cfg.enabled:
        log.info("embedding.disabled")
        return None

    encoder = EmbeddingEncoder(
        ollama_url=cfg.encoder_url,
        model=cfg.encoder_model,
        cache_size=cfg.cache_size,
        timeout_seconds=cfg.timeout_seconds,
    )
    differ = SnapshotDiffer()
    memory = SnapshotMemory(
        qdrant_url=cfg.qdrant_url,
        collection=cfg.collection_name,
    )
    await memory.ensure_collection()

    builder = CompressedPromptBuilder(memory=memory, top_k=cfg.top_k_similar)

    return WorldEncodingPipeline(
        encoder=encoder,
        differ=differ,
        memory=memory,
        prompt_builder=builder,
    )
```

---

## 5. Prompt Migration

### 5.1 Intention Prompt (Before)

```python
def build_intention_prompt(snapshot: WorldSnapshot, narrative: str) -> str:
    snapshot_json = json.dumps([e.model_dump() for e in snapshot.entities], indent=2)
    return f"""You are CoreMind's intention layer.
...
World state:
{snapshot_json}
...
"""
```

### 5.2 Intention Prompt (After)

```python
def build_intention_prompt(
    compressed: CompressedPrompt,
    narrative: str,
    *,
    fallback_snapshot: WorldSnapshot | None = None,
) -> str:
    """Build the intention prompt using compressed world state.

    If compressed.full_fallback is set, uses the fallback text instead.
    """
    world_context = compressed.to_prompt_text()

    return f"""You are CoreMind's intention layer.
...
{world_context}

## Instructions
Generate intents based on:
- Changes since last cycle (most important)
- Similar past states (detect recurring patterns)
- Key metrics (overall system context)

Focus on what is NEW or DIFFERENT, not on unchanged background state.
...
"""
```

### 5.3 Reasoning Prompt (Same Pattern)

Apply the same transformation to `reasoning/prompts.py`. Replace full snapshot JSON with `compressed.to_prompt_text()`.

### 5.4 Backward Compatibility

During migration, keep the old prompt functions with a `_v1` suffix. The loop decides which to use based on `config.embedding.enabled`:

```python
# In intention/loop.py
if self._pipeline:
    compressed = await self._pipeline.process(snapshot)
    prompt = build_intention_prompt(compressed, narrative)
else:
    prompt = build_intention_prompt_v1(snapshot, narrative)
```

---

## 6. CLI Commands

### 6.1 `coremind world embed-stats`

```text
$ coremind world embed-stats

Embedding Encoder Statistics
  Cache hits:     4521 (89.3%)
  Cache misses:   542
  Errors:         0
  Avg encode:     12ms

Snapshot Memory
  Stored:         847 embeddings
  Collection:     snapshot_embeddings
  Fallback:       inactive
```

### 6.2 `coremind world similar --limit 5`

```text
$ coremind world similar --limit 5

Similar Past States (to current snapshot):
  1. 4h ago   (sim=0.94): 48 entities, no changes
  2. 1d ago   (sim=0.91): 47 entities, 2 changed
  3. 2d ago   (sim=0.89): 49 entities, 1 changed
  4. 3d ago   (sim=0.87): 48 entities, 3 changed
  5. 7d ago   (sim=0.85): 46 entities, 1 added
```

### 6.3 `coremind world prune --keep 1000`

```text
$ coremind world prune --keep 1000

Pruned 234 old snapshot embeddings. Remaining: 1000.
```

---

## 7. Pruning Background Task

Register a periodic task in the daemon that runs every `prune_interval_seconds`:

```python
async def _prune_snapshot_embeddings(self) -> None:
    """Periodic task to prune old snapshot embeddings."""
    if not self._pipeline:
        return
    pruned = await self._pipeline._memory.prune(
        keep_count=self._config.embedding.prune_keep_count
    )
    if pruned > 0:
        log.info("embedding.pruned", count=pruned)
```

---

## 8. Tests

```python
# tests/world/test_integration.py

@pytest.mark.asyncio
async def test_intention_loop_uses_compressed_prompt():
    """When embedding is enabled, intention loop uses CompressedPrompt."""
    pipeline = make_mock_pipeline(returns=mock_compressed_prompt)
    loop = IntentionLoop(pipeline=pipeline, ...)
    await loop.run_cycle(snapshot)
    # Verify the LLM was called with compressed text, not full JSON
    assert "## World State Summary" in loop.last_prompt
    assert "entities" not in loop.last_prompt or len(loop.last_prompt) < 5000

@pytest.mark.asyncio
async def test_intention_loop_falls_back_without_pipeline():
    """When embedding is disabled, intention loop uses v1 prompt."""
    loop = IntentionLoop(pipeline=None, ...)
    await loop.run_cycle(snapshot)
    # Full JSON present
    assert len(loop.last_prompt) > 10000

@pytest.mark.asyncio
async def test_config_loads_embedding_section():
    config = load_config(toml_with_embedding_section)
    assert config.embedding.enabled is True
    assert config.embedding.encoder_url == "http://OLLAMA_HOST:11434"
    assert config.embedding.cache_size == 5000
```

---

## 9. A/B Comparison (Optional but Recommended)

During the first week of deployment, run both prompt paths and log:

```python
class PromptComparator:
    """Run old + new prompts side by side. Log token savings and similarity."""

    async def compare(
        self, snapshot: WorldSnapshot, compressed: CompressedPrompt
    ) -> None:
        old_tokens = count_tokens(build_intention_prompt_v1(snapshot, ""))
        new_tokens = compressed.estimated_tokens
        reduction_pct = (old_tokens - new_tokens) / old_tokens * 100

        log.info(
            "prompt.comparison",
            old_tokens=old_tokens,
            new_tokens=new_tokens,
            reduction_pct=round(reduction_pct, 1),
        )
```

Target: >=80% token reduction over 100 cycles before removing the v1 path.

---

## 10. Success Criteria

- [ ] `EmbeddingConfig` loads from `~/.coremind/config.toml`
- [ ] Daemon initializes the pipeline on startup (when enabled)
- [ ] Intention loop uses `CompressedPrompt` when pipeline is available
- [ ] Reasoning loop uses `CompressedPrompt` when pipeline is available
- [ ] Falls back to v1 prompts when `embedding.enabled = false`
- [ ] CLI `embed-stats` shows encoder cache statistics
- [ ] CLI `similar` shows top-K similar past states
- [ ] CLI `prune` removes old embeddings
- [ ] Pruning background task runs periodically
- [ ] No regressions in existing tests
- [ ] `mypy --strict` passes
- [ ] `ruff check` passes

---

## 11. Out of Scope

- Actual LLM quality comparison (run manually post-deployment)
- Dashboard UI for embedding stats (future)
- Tuning `IGNORED_ATTRS` (extend as needed in production)
- Multi-model embedding support (nomic-embed-text only for now)
