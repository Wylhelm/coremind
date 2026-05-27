# Phase 3 — JEPA-Inspired Embedding World

**Target:** CoreMind v2
**Duration estimate:** 1 week
**Agent:** Opus in VS Code
**Prerequisites:** None (can be implemented in parallel)

---

## Subphases

This phase is split into independent subphases for parallel agent sessions:

| Subphase | Focus | Prerequisites | Effort |
| --- | --- | --- | --- |
| [3A — Snapshot Differ](PHASE_3A_SNAPSHOT_DIFFER.md) | Pure-logic diff between snapshots | None | 1–2h |
| [3B — Embedding Encoder](PHASE_3B_EMBEDDING_ENCODER.md) | Ollama wrapper + caching | None | 2–3h |
| [3C — Snapshot Memory](PHASE_3C_SNAPSHOT_MEMORY.md) | Qdrant storage + similarity search | 3B (type only) | 2–3h |
| [3D — Compressed Prompt](PHASE_3D_COMPRESSED_PROMPT.md) | Builder + Pipeline orchestration | 3A, 3B, 3C | 2–3h |
| [3E — Integration](PHASE_3E_INTEGRATION.md) | Wire into L4/L5 + CLI + config | 3D | 2–3h |

**Parallelism:** 3A and 3B can run simultaneously. 3C can start once 3B defines `VECTOR_DIM`. 3D requires all three. 3E is last.

---

## 1. Problem Statement

CoreMind v1 sends the full `WorldSnapshot` JSON to the LLM at every reasoning cycle. Real-world measurements from production:

- **Snapshot size:** typically 15,000–30,000 tokens
- **Entities:** ~48 in steady state
- **Truly changed entities per cycle:** 1–4
- **% of snapshot relevant to current cycle:** <10%

### Cost Implications

| Metric | v1 |
|---|---|
| Tokens per reasoning cycle | 15K–30K |
| Cycles per day | ~144 (every 10 min) |
| Daily tokens | 2.1M–4.3M |
| Cost (DeepSeek-Pro $0.27/M) | $0.57–$1.16/day |

### Quality Implications

Worse than the cost: **hallucination from stale text**. The LLM regularly references values that haven't been updated since the last cycle but appear "current" in the snapshot text. This is how we got the "Roborock hasn't cleaned since May 17" loop when it actually cleaned on May 24 — the snapshot was stale and the LLM trusted it.

### Goal

Reduce token usage by ~90% while *improving* reasoning quality through:
1. **Embedding-based representation** instead of verbose text
2. **Diff-only updates** — only changed entities go to the LLM
3. **Similarity context** — "this state looks like X past states"

---

## 2. Background — JEPA in 30 Seconds

Yann LeCun's **JEPA (Joint Embedding Predictive Architecture)** trains models to predict abstract representations in embedding space rather than generating raw outputs. The key insight: **most useful cognition happens in embedding space, not in token space**.

We're not implementing JEPA itself. We're using its core idea — that **rich embedding representations beat verbose text** — as a practical engineering pattern.

We already have `nomic-embed-text` running locally via Ollama at `10.0.0.175:11434`. This gives us 768-dimensional embeddings for free.

---

## 3. Design

### 3.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  L2 — World Model                                                │
│                                                                  │
│   ┌──────────────────┐     ┌──────────────────┐                  │
│   │ Current Snapshot │     │ Previous Snapshot│                  │
│   └────────┬─────────┘     └────────┬─────────┘                  │
│            │                         │                            │
│            └────────────┬────────────┘                            │
│                         ▼                                         │
│                ┌─────────────────┐                                │
│                │ SnapshotDiffer  │                                │
│                └────────┬────────┘                                │
│                         ▼                                         │
│              ┌────────────────────┐                               │
│              │  SnapshotDiff      │                               │
│              │  - added: [...]    │                               │
│              │  - removed: [...]  │                               │
│              │  - changed: [...]  │                               │
│              │  - unchanged: 45   │                               │
│              └────────┬───────────┘                               │
│                       │                                           │
│                       ▼                                           │
│            ┌──────────────────────┐                               │
│            │  EmbeddingEncoder    │                               │
│            │  (Ollama nomic-768)  │                               │
│            └─────────┬────────────┘                               │
│                      │                                            │
│                      ▼                                            │
│           ┌─────────────────────┐         ┌───────────────────┐  │
│           │  Snapshot embedding  │────────▶│  Qdrant: store    │  │
│           └─────────┬────────────┘         │  & similarity     │  │
│                     │                       │  search           │  │
│                     ▼                       └─────────┬─────────┘  │
│           ┌─────────────────────────┐                 │            │
│           │  CompressedPromptBuilder │◀────────────────┘            │
│           └─────────┬───────────────┘                              │
│                     ▼                                              │
│       ┌──────────────────────────────┐                             │
│       │  CompressedPrompt (~2500 tok)│                             │
│       └──────────────────────────────┘                             │
│                     │                                              │
└─────────────────────┼──────────────────────────────────────────────┘
                      ▼
              L4 / L5 (Reasoning, Intention)
```

### 3.2 Components

#### 3.2.1 EmbeddingEncoder

```python
class EmbeddingEncoder:
    """Wraps Ollama nomic-embed-text. Caches embeddings."""

    def __init__(
        self,
        ollama_url: str = "http://10.0.0.175:11434",
        model: str = "nomic-embed-text",
        cache_size: int = 5000,
    ):
        self._url = ollama_url.rstrip("/") + "/api/embeddings"
        self._model = model
        self._cache: dict[str, list[float]] = {}
        self._cache_max = cache_size
        self._client = httpx.AsyncClient(timeout=10.0)
        self._stats = EncoderStats()

    async def encode_text(self, text: str) -> list[float]:
        """Encode a string to a 768-d vector. Cached by content hash."""
        cache_key = self._hash(text)
        if cache_key in self._cache:
            self._stats.cache_hits += 1
            return self._cache[cache_key]

        self._stats.cache_misses += 1
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model, "prompt": text},
            )
            response.raise_for_status()
            vector = response.json()["embedding"]
        except Exception as e:
            self._stats.errors += 1
            log.warning("encoder.fail", error=str(e))
            raise EncoderError(f"Embedding failed: {e}") from e

        # LRU eviction
        if len(self._cache) >= self._cache_max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = vector
        return vector

    async def encode_entity(self, entity: Entity) -> list[float]:
        """Encode a single entity."""
        text = self._entity_to_text(entity)
        return await self.encode_text(text)

    async def encode_snapshot(self, snapshot: WorldSnapshot) -> list[float]:
        """Encode a full snapshot as a weighted average of entity embeddings.

        Weights:
        - Recently-updated entities get higher weight
        - Larger entities (more attributes) get higher weight
        """
        if not snapshot.entities:
            # Zero vector for empty snapshot
            return [0.0] * 768

        entity_vectors = await asyncio.gather(*[
            self.encode_entity(e) for e in snapshot.entities
        ])
        weights = [self._compute_weight(e) for e in snapshot.entities]

        return self._weighted_average(entity_vectors, weights)

    def _entity_to_text(self, entity: Entity) -> str:
        """Convert an entity to embedding-input text."""
        parts = [f"{entity.entity_type}:{entity.entity_id}"]
        for attr, value in sorted(entity.attributes.items()):
            parts.append(f"{attr}={value}")
        if entity.room:
            parts.append(f"room={entity.room}")
        return " | ".join(parts)

    def _compute_weight(self, entity: Entity) -> float:
        # Base weight
        weight = 1.0
        # Boost recently-updated entities
        if entity.last_updated:
            age_hours = (datetime.now(UTC) - entity.last_updated).total_seconds() / 3600
            weight *= max(0.1, 1.0 - (age_hours / 24))  # 0.1 to 1.0 over 24h
        # Boost entities with more attributes
        weight *= 1 + min(len(entity.attributes), 10) / 10
        return weight

    def _weighted_average(self, vectors: list[list[float]], weights: list[float]) -> list[float]:
        total_weight = sum(weights)
        if total_weight == 0:
            return [0.0] * len(vectors[0])
        result = [0.0] * len(vectors[0])
        for vec, w in zip(vectors, weights):
            for i, v in enumerate(vec):
                result[i] += v * w / total_weight
        return result

    def _hash(self, text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode()).hexdigest()


class EncoderStats(BaseModel):
    cache_hits: int = 0
    cache_misses: int = 0
    errors: int = 0
```

#### 3.2.2 SnapshotDiffer

```python
class SnapshotDiff(BaseModel):
    """Represents the difference between two snapshots."""
    added: list[Entity] = Field(default_factory=list)
    removed: list[Entity] = Field(default_factory=list)
    changed: list[tuple[Entity, Entity]] = Field(default_factory=list)  # (old, new)
    unchanged_count: int = 0
    total_current: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    @property
    def change_summary(self) -> str:
        parts = []
        if self.added: parts.append(f"{len(self.added)} added")
        if self.removed: parts.append(f"{len(self.removed)} removed")
        if self.changed: parts.append(f"{len(self.changed)} changed")
        if self.unchanged_count: parts.append(f"{self.unchanged_count} unchanged")
        return ", ".join(parts) if parts else "no changes"


class SnapshotDiffer:
    """Computes diffs between snapshots."""

    def diff(
        self,
        current: WorldSnapshot,
        previous: WorldSnapshot | None,
    ) -> SnapshotDiff:
        if previous is None:
            return SnapshotDiff(
                added=list(current.entities),
                total_current=len(current.entities),
            )

        prev_by_key = {self._key(e): e for e in previous.entities}
        curr_by_key = {self._key(e): e for e in current.entities}

        prev_keys = set(prev_by_key.keys())
        curr_keys = set(curr_by_key.keys())

        added = [curr_by_key[k] for k in curr_keys - prev_keys]
        removed = [prev_by_key[k] for k in prev_keys - curr_keys]

        changed: list[tuple[Entity, Entity]] = []
        unchanged = 0
        for k in prev_keys & curr_keys:
            old, new = prev_by_key[k], curr_by_key[k]
            if self._entities_differ(old, new):
                changed.append((old, new))
            else:
                unchanged += 1

        return SnapshotDiff(
            added=added,
            removed=removed,
            changed=changed,
            unchanged_count=unchanged,
            total_current=len(current.entities),
        )

    def _key(self, entity: Entity) -> str:
        return f"{entity.entity_type}:{entity.entity_id}"

    def _entities_differ(self, old: Entity, new: Entity) -> bool:
        # Compare attributes ignoring timestamps that always tick
        old_attrs = {k: v for k, v in old.attributes.items() if k not in IGNORED_ATTRS}
        new_attrs = {k: v for k, v in new.attributes.items() if k not in IGNORED_ATTRS}
        return old_attrs != new_attrs


IGNORED_ATTRS = {"last_changed", "last_updated", "last_seen"}
```

#### 3.2.3 SnapshotMemory (Qdrant)

```python
class SnapshotMemory:
    """Stores and retrieves snapshot embeddings via Qdrant."""

    def __init__(self, qdrant_url: str, collection: str = "snapshot_embeddings"):
        self._client = QdrantClient(url=qdrant_url)
        self._collection = collection

    async def ensure_collection(self) -> None:
        """Create collection if not exists."""
        try:
            self._client.get_collection(self._collection)
        except Exception:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE),
            )

    async def store(
        self,
        snapshot_id: str,
        vector: list[float],
        summary: str,
        entity_count: int,
        timestamp: datetime,
    ) -> None:
        self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=snapshot_id,
                    vector=vector,
                    payload={
                        "summary": summary,
                        "entity_count": entity_count,
                        "timestamp": timestamp.isoformat(),
                    },
                )
            ],
        )

    async def find_similar(
        self,
        vector: list[float],
        k: int = 5,
        exclude_recent_seconds: float = 600.0,
    ) -> list[SimilarSnapshot]:
        """Find top-K most similar past snapshots.

        Excludes very recent ones (within exclude_recent_seconds) to avoid
        matching against essentially-the-same state.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=exclude_recent_seconds)).isoformat()
        results = self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            limit=k,
            query_filter=Filter(
                must=[FieldCondition(key="timestamp", range=Range(lt=cutoff))]
            ),
        )
        return [
            SimilarSnapshot(
                snapshot_id=str(r.id),
                score=r.score,
                summary=r.payload["summary"],
                entity_count=r.payload["entity_count"],
                timestamp=datetime.fromisoformat(r.payload["timestamp"]),
            )
            for r in results
        ]

    async def prune(self, keep_count: int = 1000) -> int:
        """Keep only the most recent N embeddings. Returns count pruned."""
        # Get total
        count = self._client.count(collection_name=self._collection).count
        if count <= keep_count:
            return 0
        # Get all timestamps sorted
        # ... pagination logic to find cutoff timestamp
        # Delete points older than cutoff
        # Return pruned count
        ...


class SimilarSnapshot(BaseModel):
    snapshot_id: str
    score: float                    # cosine similarity 0..1
    summary: str
    entity_count: int
    timestamp: datetime
```

#### 3.2.4 CompressedPromptBuilder

```python
class CompressedPrompt(BaseModel):
    summary: str                    # "48 entities (3 changed)"
    changes_text: str               # Human-readable diff
    similar_states_text: str        # Top-K similar past states
    key_metrics_text: str           # Summary statistics
    full_fallback: str | None = None

    @property
    def estimated_tokens(self) -> int:
        """Rough estimate: 1 token ≈ 4 chars."""
        full = self.to_prompt_text()
        return len(full) // 4

    def to_prompt_text(self) -> str:
        """Render as final prompt text."""
        parts = [
            f"## World State Summary",
            self.summary,
            "",
            f"## Changes Since Last Cycle",
            self.changes_text,
            "",
        ]
        if self.similar_states_text:
            parts.extend([
                f"## Similar Past States",
                self.similar_states_text,
                "",
            ])
        if self.key_metrics_text:
            parts.extend([
                f"## Key Metrics",
                self.key_metrics_text,
            ])
        return "\n".join(parts)


class CompressedPromptBuilder:
    def __init__(self, memory: SnapshotMemory):
        self._memory = memory

    async def build(
        self,
        snapshot: WorldSnapshot,
        diff: SnapshotDiff,
        snapshot_embedding: list[float],
    ) -> CompressedPrompt:
        # 1. Summary
        summary = (
            f"{diff.total_current} entities, {diff.change_summary}. "
            f"Timestamp: {snapshot.timestamp.isoformat()}."
        )

        # 2. Changes text — only changed entities
        changes_text = self._format_changes(diff)

        # 3. Similar past states
        similar = await self._memory.find_similar(snapshot_embedding, k=3)
        similar_text = self._format_similar(similar)

        # 4. Key metrics
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

        lines = []
        for entity in diff.added:
            lines.append(f"+ {entity.entity_id}: {self._brief(entity)}")
        for entity in diff.removed:
            lines.append(f"- {entity.entity_id} (removed)")
        for old, new in diff.changed:
            lines.append(f"~ {new.entity_id}: {self._diff_attrs(old, new)}")
        return "\n".join(lines)

    def _brief(self, entity: Entity) -> str:
        """One-line entity summary."""
        important = sorted(entity.attributes.items())[:3]
        attrs = ", ".join(f"{k}={v}" for k, v in important)
        return f"({entity.entity_type}) {attrs}"

    def _diff_attrs(self, old: Entity, new: Entity) -> str:
        """Show only changed attributes."""
        diffs = []
        for k in set(old.attributes.keys()) | set(new.attributes.keys()):
            o = old.attributes.get(k)
            n = new.attributes.get(k)
            if o != n:
                diffs.append(f"{k}: {o} → {n}")
        return "; ".join(diffs)

    def _format_similar(self, similar: list[SimilarSnapshot]) -> str:
        if not similar:
            return ""
        lines = []
        for s in similar:
            age = self._age_str(s.timestamp)
            lines.append(f"- {age} (similarity {s.score:.2f}): {s.summary}")
        return "\n".join(lines)

    def _compute_metrics(self, snapshot: WorldSnapshot) -> dict[str, Any]:
        """Compute roll-up statistics."""
        metrics = {
            "total_entities": len(snapshot.entities),
            "by_type": {},
        }
        for entity in snapshot.entities:
            metrics["by_type"][entity.entity_type] = metrics["by_type"].get(entity.entity_type, 0) + 1
        return metrics

    def _format_metrics(self, metrics: dict) -> str:
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

### 3.3 Fallback Strategy

If the embedding service is unreachable:

```python
class WorldEncodingPipeline:
    """Orchestrates encoder + differ + memory + prompt builder.

    Gracefully falls back to full text snapshots on encoder failure.
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

    async def process(
        self,
        current: WorldSnapshot,
        previous: WorldSnapshot | None,
    ) -> CompressedPrompt:
        diff = self._differ.diff(current, previous)

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
            return prompt

        except EncoderError as e:
            if not self._fallback_active:
                log.warning("encoding.fallback_active", error=str(e))
                self._fallback_active = True

            # Return text fallback
            return CompressedPrompt(
                summary=f"{diff.total_current} entities (embedding service unavailable)",
                changes_text=self._format_full_snapshot(current),
                similar_states_text="",
                key_metrics_text="",
                full_fallback=self._format_full_snapshot(current),
            )

    def _format_full_snapshot(self, snapshot: WorldSnapshot) -> str:
        """V1-style full text. Used only on fallback."""
        return json.dumps([e.model_dump() for e in snapshot.entities], indent=2, default=str)
```

---

## 4. Files to Create/Modify

### New Files

| File | Purpose |
|---|---|
| `src/coremind/world/embeddings.py` | EmbeddingEncoder, EncoderStats, EncoderError |
| `src/coremind/world/differ.py` | SnapshotDiff, SnapshotDiffer |
| `src/coremind/world/snapshot_memory.py` | SnapshotMemory (Qdrant wrapper) |
| `src/coremind/world/compressed_prompt.py` | CompressedPrompt, CompressedPromptBuilder |
| `src/coremind/world/pipeline.py` | WorldEncodingPipeline |
| `src/coremind/world/schemas.py` | All schemas |
| `tests/test_world_encoder.py` | Encoder tests |
| `tests/test_world_differ.py` | Differ tests |
| `tests/test_world_pipeline.py` | Pipeline integration tests |

### Modified Files

| File | Change |
|---|---|
| `src/coremind/intention/loop.py` | Use CompressedPrompt instead of full snapshot |
| `src/coremind/reasoning/loop.py` | Use CompressedPrompt instead of full snapshot |
| `src/coremind/intention/prompts.py` | New template that accepts CompressedPrompt |
| `src/coremind/reasoning/prompts.py` | New template that accepts CompressedPrompt |
| `src/coremind/core/daemon.py` | Initialize WorldEncodingPipeline |
| `src/coremind/core/config.py` | Add EmbeddingConfig |
| `pyproject.toml` | Add `qdrant-client` if not already there |
| `~/.coremind/config.toml` | Add `[embedding]` section |

---

## 5. Configuration

```toml
[embedding]
enabled = true
encoder_url = "http://10.0.0.175:11434"
encoder_model = "nomic-embed-text"
cache_size = 5000

# Qdrant
qdrant_url = "http://localhost:6333"
collection_name = "snapshot_embeddings"

# Compressed prompt
max_changes_in_prompt = 20          # cap entity changes shown
top_k_similar = 3                   # top-K similar past states
exclude_recent_seconds = 600.0      # exclude similar matches < 10 min old

# Pruning
prune_keep_count = 1000             # keep last N embeddings
prune_interval_seconds = 21600      # every 6h

# Fallback
fallback_on_error = true            # fall back to full text on encoder error
fallback_log_threshold_seconds = 300  # log fallback if active >5min
```

---

## 6. Migration to Compressed Prompts

### 6.1 Old Intention Prompt (v1)

```python
def build_intention_prompt(snapshot: WorldSnapshot) -> str:
    snapshot_json = json.dumps([e.model_dump() for e in snapshot.entities], indent=2)
    return f"""You are CoreMind's intention layer.

World state:
{snapshot_json}

Generate intents based on this state.
"""
```

### 6.2 New Intention Prompt (v2)

```python
def build_intention_prompt(compressed: CompressedPrompt) -> str:
    return f"""You are CoreMind's intention layer.

{compressed.to_prompt_text()}

## Instructions
Generate intents based on:
- The changes since the last cycle (most important)
- Similar past states (use to detect recurring patterns)
- Key metrics (overall system state)

Focus on what's NEW or DIFFERENT, not on unchanged background state.
"""
```

### 6.3 A/B Comparison

During migration, run both prompts in parallel and compare results:

```python
class PromptComparator:
    """Run both prompts side-by-side for validation."""

    async def compare(
        self,
        snapshot: WorldSnapshot,
        compressed: CompressedPrompt,
    ) -> ComparisonResult:
        # Old path
        old_prompt = build_intention_prompt_v1(snapshot)
        old_tokens = self._count_tokens(old_prompt)
        old_response = await self._llm.complete(old_prompt)

        # New path
        new_prompt = build_intention_prompt_v2(compressed)
        new_tokens = self._count_tokens(new_prompt)
        new_response = await self._llm.complete(new_prompt)

        return ComparisonResult(
            old_tokens=old_tokens,
            new_tokens=new_tokens,
            old_response=old_response,
            new_response=new_response,
            token_reduction_pct=(old_tokens - new_tokens) / old_tokens * 100,
            similarity_score=self._compare_responses(old_response, new_response),
        )
```

When the comparator shows >80% response similarity over 100 cycles, we can switch to the new prompt exclusively.

---

## 7. CLI Commands

```bash
# Stats
coremind world embed-stats
# Output:
# Cache hits: 4521 (89.3%)
# Cache misses: 542
# Errors: 0
# Avg embedding time: 12ms
# Token reduction (24h): 91.2%

# Find similar past states
coremind world similar --limit 5
# Output:
# 4h ago (sim=0.94): 48 entities, no changes
# 1d ago (sim=0.91): 47 entities, 2 changed
# 2d ago (sim=0.89): 49 entities, 1 changed

# Force re-encoding
coremind world re-encode --all

# Manage Qdrant collection
coremind world prune --keep 1000
coremind world collection-info
```

---

## 8. Performance Targets

| Metric | Target | Stretch |
|---|---|---|
| Token reduction per cycle | ≥80% | ≥90% |
| Embedding cache hit rate | ≥80% | ≥95% |
| Encoder latency p95 | <50ms | <20ms |
| Diff computation latency p95 | <10ms | <5ms |
| Compressed prompt build latency p95 | <100ms | <50ms |
| Similar snapshot search latency p95 | <100ms | <50ms |
| End-to-end pipeline latency p95 | <500ms | <200ms |

---

## 9. Tests

### 9.1 Unit Tests

```python
# tests/test_world_differ.py
def test_diff_added_entity():
    differ = SnapshotDiffer()
    prev = make_snapshot(["light.bureau", "light.salon"])
    curr = make_snapshot(["light.bureau", "light.salon", "light.cuisine"])
    diff = differ.diff(curr, prev)
    assert len(diff.added) == 1
    assert diff.added[0].entity_id == "light.cuisine"
    assert diff.unchanged_count == 2

def test_diff_changed_attributes():
    differ = SnapshotDiffer()
    prev_e = make_entity("light.bureau", state="off")
    curr_e = make_entity("light.bureau", state="on")
    diff = differ.diff(make_snapshot_from([curr_e]), make_snapshot_from([prev_e]))
    assert len(diff.changed) == 1
    old, new = diff.changed[0]
    assert old.attributes["state"] == "off"
    assert new.attributes["state"] == "on"

def test_diff_ignores_timestamps():
    """last_changed and last_updated should not count as changes."""
    differ = SnapshotDiffer()
    prev_e = make_entity("light.bureau", state="on", last_updated="2026-05-26T10:00:00")
    curr_e = make_entity("light.bureau", state="on", last_updated="2026-05-26T10:05:00")
    diff = differ.diff(make_snapshot_from([curr_e]), make_snapshot_from([prev_e]))
    assert len(diff.changed) == 0
    assert diff.unchanged_count == 1


# tests/test_world_encoder.py
@pytest.mark.asyncio
async def test_encoder_caches_repeated_text():
    encoder = EmbeddingEncoder(ollama_url="http://test", cache_size=10)
    encoder._client = mock_client_returning([0.1] * 768)

    v1 = await encoder.encode_text("hello")
    v2 = await encoder.encode_text("hello")

    assert v1 == v2
    assert encoder._stats.cache_hits == 1
    assert encoder._stats.cache_misses == 1
    assert encoder._client.post.call_count == 1

@pytest.mark.asyncio
async def test_encoder_weights_recent_higher():
    encoder = EmbeddingEncoder(...)
    fresh = make_entity("e1", last_updated=datetime.now(UTC))
    stale = make_entity("e2", last_updated=datetime.now(UTC) - timedelta(hours=20))
    w_fresh = encoder._compute_weight(fresh)
    w_stale = encoder._compute_weight(stale)
    assert w_fresh > w_stale

# tests/test_world_pipeline.py
@pytest.mark.asyncio
async def test_pipeline_falls_back_on_encoder_failure():
    failing_encoder = MockFailingEncoder()
    pipeline = WorldEncodingPipeline(failing_encoder, differ, memory, builder)

    result = await pipeline.process(snapshot, None)
    assert result.full_fallback is not None
    assert pipeline._fallback_active is True

@pytest.mark.asyncio
async def test_pipeline_recovers_after_encoder_returns():
    encoder = MockToggleableEncoder(fail=True)
    pipeline = WorldEncodingPipeline(encoder, ...)

    # First call: fallback
    r1 = await pipeline.process(snapshot, None)
    assert pipeline._fallback_active

    # Encoder recovers
    encoder.fail = False

    # Second call: normal path
    r2 = await pipeline.process(snapshot2, snapshot)
    assert not pipeline._fallback_active
    assert r2.full_fallback is None
```

### 9.2 Integration Tests

```python
@pytest.mark.integration
async def test_token_reduction_target():
    """Compressed prompt should be ≥80% smaller than full snapshot."""
    pipeline = WorldEncodingPipeline(real_encoder, ...)
    full_snapshot_size = count_tokens(json.dumps(real_snapshot.model_dump()))

    compressed = await pipeline.process(real_snapshot, None)
    compressed_size = count_tokens(compressed.to_prompt_text())

    reduction = (full_snapshot_size - compressed_size) / full_snapshot_size
    assert reduction >= 0.8, f"Only {reduction:.0%} reduction (target ≥80%)"

@pytest.mark.integration
async def test_similar_snapshots_retrieved():
    """After storing 10 snapshots, similar query returns matches."""
    pipeline = WorldEncodingPipeline(real_encoder, ...)
    for i in range(10):
        await pipeline.process(make_snapshot(f"sample{i}"), None)

    similar = await pipeline._memory.find_similar(known_vector, k=3)
    assert len(similar) == 3
```

### 9.3 Quality Tests (Critical)

```python
@pytest.mark.quality
async def test_compressed_prompt_preserves_reasoning_quality():
    """Run 50 cycles with both prompts. Conclusions should be ≥80% similar."""
    comparator = PromptComparator(real_llm)

    results = []
    for snapshot in load_50_real_snapshots():
        compressed = await pipeline.process(snapshot, prev)
        result = await comparator.compare(snapshot, compressed)
        results.append(result)

    avg_similarity = sum(r.similarity_score for r in results) / len(results)
    avg_token_reduction = sum(r.token_reduction_pct for r in results) / len(results)

    assert avg_similarity >= 0.80, f"Response quality degraded: {avg_similarity:.2%}"
    assert avg_token_reduction >= 80, f"Token reduction insufficient: {avg_token_reduction:.1f}%"
```

---

## 10. Success Criteria

- [ ] EmbeddingEncoder calls Ollama successfully and caches results
- [ ] SnapshotDiffer correctly identifies added/removed/changed entities
- [ ] SnapshotMemory stores and retrieves embeddings via Qdrant
- [ ] CompressedPromptBuilder produces prompts <3000 tokens for typical snapshots
- [ ] WorldEncodingPipeline orchestrates all components
- [ ] Fallback to full text works when encoder is down
- [ ] A/B comparison shows ≥80% response similarity over 50 real cycles
- [ ] Token reduction ≥80% measured in production
- [ ] No regressions in intent generation quality
- [ ] Qdrant collection pruning runs successfully
- [ ] All tests pass

---

**Next step:** Implement EmbeddingEncoder first (no dependencies), then SnapshotDiffer (no dependencies), then wire them together.
