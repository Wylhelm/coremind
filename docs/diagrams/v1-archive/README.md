# v1 Architecture Diagrams (Archived)

These diagrams depict the **v1** (7-layer) CoreMind architecture as it existed
through v0.3.x. They are preserved here for historical reference.

The active diagrams for the current architecture (v2, 9-layer with L0 Discovery
and L8 Meta) live in the parent directory: [`../`](../).

| File | What it showed (v1) |
|---|---|
| `01-system-overview.drawio` | 7-layer forward pipeline L1→L7 with single L7→L2/L3 feedback |
| `02-process-topology.drawio` | Daemon process, plugin host, event bus, stores |
| `03-data-model.drawio` | Core types: WorldEvent, Entity, Relationship, Intent, Action |
| `04-graduated-agency.drawio` | Binary Safe/Suggest/Ask routing with forced-approval classes |
| `05-openclaw-integration.drawio` | OpenClaw adapter boundary contract (v0.1.0) |

## When these were current

- **v0.1.0 – v0.3.3** (April – May 2026)
- Superseded by v2 diagrams on 2026-05-27
