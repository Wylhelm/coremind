# Diagrams

Visual companion to [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md). Each file is a
[Draw.io](https://www.drawio.com/) document and can be opened with the **Draw.io
Integration** VS Code extension or on [diagrams.net](https://app.diagrams.net).

| File | What it shows | Anchors in `ARCHITECTURE.md` |
|---|---|---|
| [01-system-overview.drawio](01-system-overview.drawio) | The seven cognitive layers, forward data path L1→L7, and the single L7→L2/L3 feedback edge. | §2.1, §3 |
| [02-process-topology.drawio](02-process-topology.drawio) | Daemon process, plugin host (gRPC), event bus, in-process layer tasks, and persistent stores (SurrealDB, Qdrant, audit journal). | §6, §7 |
| [03-data-model.drawio](03-data-model.drawio) | The core types — `WorldEvent`, `Entity`, `Relationship`, `Intent`, `Action` — and how they reference each other. | §4 |
| [04-graduated-agency.drawio](04-graduated-agency.drawio) | L5 → L6 routing: forced-approval classes, confidence bands (Safe / Suggest / Ask), approval gate, signed audit journal. | §3.6, §9 |
| [05-openclaw-integration.drawio](05-openclaw-integration.drawio) | The OpenClaw adapter pattern: bidirectional plugin, what crosses the boundary, what each side keeps. | §14 |

## Editing

- VS Code: install [`hediet.vscode-drawio`](https://marketplace.visualstudio.com/items?itemName=hediet.vscode-drawio) and open any `.drawio` file.
- Web: open the file via *File → Open → Device* on <https://app.diagrams.net>.

When the architecture changes, update the relevant diagram in the same PR as the
prose change. Diagrams are *not* the source of truth — `ARCHITECTURE.md` is —
but they must stay consistent with it.

## Exporting

To regenerate PNG/SVG companions (optional), use the Draw.io extension's
*Export* command, or the `drawio` CLI:

```bash
drawio -x -f svg -o 01-system-overview.svg 01-system-overview.drawio
```

Exported images are not committed by default; only the `.drawio` sources are
authoritative.
