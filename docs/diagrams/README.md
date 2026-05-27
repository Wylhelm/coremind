# Diagrams

Visual companion to the [v2 Architecture](../phases/v2/ARCHITECTURE.md). Each
file is a [Draw.io](https://www.drawio.com/) document and can be opened with the
**Draw.io Integration** VS Code extension or on
[diagrams.net](https://app.diagrams.net).

## Current Diagrams (v2 — 9-Layer Architecture)

| File | What it shows | Anchors in v2 `ARCHITECTURE.md` |
| ---- | ---- | ---- |
| [01-system-overview.drawio](01-system-overview.drawio) | The nine cognitive layers L0–L8, forward flow, L7→L2/L3 reflection feedback, L8→L2–L7 meta-adjustments. | §2 |
| [02-process-topology.drawio](02-process-topology.drawio) | Daemon process, plugin host (gRPC), event bus, layer tasks, DiscoveryEngine, MetaLoop, InvestigationEngine, EmbeddingEncoder, persistent stores. | §2, §9 |
| [03-data-model.drawio](03-data-model.drawio) | Core types (WorldEvent, Entity, Relationship, Intent, Action) + v2 types (AutonomyConfig, DeviceCapabilities, InvestigationRun, MetaObservation, AdjustmentRecord, Embedding types). | §8 |
| [04-autonomy-slider.drawio](04-autonomy-slider.drawio) | Autonomy slider resolution: hard-ASK gate → domain slider lookup → `resolve_agency()` → SAFE/SUGGEST/ASK. Per-domain defaults and graduation flow. | §3 |
| [05-embedding-world.drawio](05-embedding-world.drawio) | JEPA-inspired embedding pipeline: SnapshotDiffer → EmbeddingEncoder (Ollama nomic-768d) → CompressedPromptBuilder → ~2.5K tokens for L4/L5. | §4 |
| [06-auto-investigation.drawio](06-auto-investigation.drawio) | Investigation lifecycle state machine: FORMED → DESIGNING_TEST → EXECUTING → ANALYZING → RESOLVED/UNRESOLVED/ESCALATED. TestDesigner strategies per anomaly type. | §5 |
| [07-meta-loop.drawio](07-meta-loop.drawio) | L8 self-improvement cycle: Observe → Evaluate → Learn → Tune. MetaSafetyValidator hard gate. Forbidden paths. Adjustment policies. | §7 |
| [08-openclaw-integration.drawio](08-openclaw-integration.drawio) | OpenClaw adapter boundary (v2): bidirectional plugin with slider promotion routing, investigation escalations, meta-loop reports. | §2, boundary contract |

## Archived (v1)

The original v1 diagrams (7-layer architecture, v0.1.0–v0.3.3) are preserved in
[`v1-archive/`](v1-archive/) for historical reference.

## Editing

- VS Code: install [`hediet.vscode-drawio`](https://marketplace.visualstudio.com/items?itemName=hediet.vscode-drawio) and open any `.drawio` file.
- Web: open the file via *File → Open → Device* on <https://app.diagrams.net>.

When the architecture changes, update the relevant diagram in the same PR as the
prose change. Diagrams are *not* the source of truth — the Architecture doc is —
but they must stay consistent with it.

## Exporting

To regenerate PNG/SVG companions (optional), use the Draw.io extension's
*Export* command, or the `drawio` CLI:

```bash
drawio -x -f svg -o 01-system-overview.svg 01-system-overview.drawio
```

Exported images are not committed by default; only the `.drawio` sources are
authoritative.
