# Phase 6C — Development Collectors (GitHub + VS Code Extension)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A complete
**Estimated effort:** 4–5 hours

---

## 1. Goal

Detect coding patterns, project activity, and development routines from GitHub and VS Code activity. After this sub-phase:

- GitHub collector polls the `gh` CLI for commit history, active repos, and PR activity.
- VS Code extension sends heartbeats (project name, language, duration) to the daemon.
- VS Code collector aggregates heartbeats into session data.
- Both produce structured raw data ready for the extraction engine (6B).
- Collector Protocol is established as the base interface for all collectors.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/collectors/__init__.py` | Package with Collector Protocol export. |
| `src/coremind/self_model/collectors/base.py` | `Collector` Protocol definition. |
| `src/coremind/self_model/collectors/github.py` | GitHub CLI-based data collector. |
| `src/coremind/self_model/collectors/vscode.py` | VS Code heartbeat receiver + aggregator. |
| `plugins/vscode-activity/package.json` | Extension manifest. |
| `plugins/vscode-activity/tsconfig.json` | TypeScript config. |
| `plugins/vscode-activity/src/extension.ts` | Extension source (heartbeat sender). |
| `plugins/vscode-activity/README.md` | Setup and usage docs. |
| `tests/self_model/collectors/__init__.py` | Package marker. |
| `tests/self_model/collectors/test_github.py` | GitHub collector tests. |
| `tests/self_model/collectors/test_vscode.py` | VS Code collector tests. |

---

## 3. Tasks for the Coding Agent

### 6C.1 Collector Protocol

**File:** `src/coremind/self_model/collectors/base.py`

```python
from typing import Protocol
from datetime import datetime
from collections.abc import Sequence

class RawObservation(BaseModel):
    """A single raw data point from a collector, pre-extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str           # collector identifier (e.g. "github", "vscode")
    timestamp: datetime
    category: str         # "activity" | "communication" | "health" | "finance" | "media"
    data: dict[str, JsonValue]  # collector-specific structured payload


class Collector(Protocol):
    """Interface for self-model data collectors."""

    @property
    def source_id(self) -> str:
        """Unique identifier for this collector."""
        ...

    @property
    def category(self) -> str:
        """Data category this collector produces."""
        ...

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Collect raw observations since the given timestamp.

        Args:
            since: Only return observations newer than this.

        Returns:
            List of raw observations ready for the extraction engine.
        """
        ...
```

### 6C.2 GitHub Collector

**File:** `src/coremind/self_model/collectors/github.py`

```python
class GitHubCollector:
    """Collects development activity from GitHub via the gh CLI.

    Polls for recent commits, active repos, and PR activity.
    Follows the GOG plugin pattern (subprocess to CLI tool).
    """

    source_id: str = "github"
    category: str = "activity"

    def __init__(self, config: SelfModelSourcesConfig) -> None: ...

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Collect GitHub activity since the given timestamp.

        Runs:
        - `gh api /user/repos --jq '...'` — active repos
        - `gh api /users/{user}/events --jq '...'` — recent push events

        Returns observations with data like:
        - {"repo": "coremind", "commits_today": 5, "last_commit": "2026-05-28T21:00:00Z", "languages": ["python"]}
        - {"repo": "g-bot-immo", "commits_today": 0, "days_inactive": 45}
        """
```

Implementation notes:
- Use `asyncio.create_subprocess_exec` for non-blocking subprocess calls.
- Parse JSON output from `gh` CLI.
- Timeout: 30 seconds per command.
- If `gh` is not installed, log warning and return empty list (graceful degradation).
- Extract: repo names, commit counts per repo, last commit time, languages.

### 6C.3 VS Code Extension

**Directory:** `plugins/vscode-activity/`

A lightweight TypeScript VS Code extension that sends activity heartbeats.

**`package.json`** key fields:
- `name`: `coremind-vscode-activity`
- `activationEvents`: `["*"]` (activate on any file open)
- `publisher`: `coremind`
- Permissions: minimal (no file read access needed)

**`src/extension.ts`** behavior:
1. On activation, start a 5-minute interval timer.
2. Every 5 minutes, collect:
   - Active workspace folder name (project).
   - Language ID of the active editor.
   - Whether a file is actively being edited (dirty state).
3. POST to `http://localhost:9901/self-model/heartbeat` with payload:
   ```json
   {
     "project": "coremind",
     "language": "python",
     "active": true,
     "timestamp": "2026-05-28T21:05:00Z"
   }
   ```
4. Fail silently if the daemon endpoint is unreachable (log once per session).

**Privacy guarantees:**
- **No file content** ever leaves VS Code.
- **No file paths** — only project folder name and language ID.
- **No keystrokes** — only a boolean "active" flag.

### 6C.4 VS Code Heartbeat Receiver

**File:** `src/coremind/self_model/collectors/vscode.py`

```python
class VSCodeCollector:
    """Aggregates VS Code heartbeats into coding session observations.

    Receives heartbeats from the VS Code extension at a lightweight HTTP
    endpoint and aggregates them into session-level observations.
    """

    source_id: str = "vscode"
    category: str = "activity"

    def __init__(self, config: SelfModelSourcesConfig) -> None:
        self._heartbeats: list[dict] = []  # In-memory buffer

    def receive_heartbeat(self, payload: dict[str, JsonValue]) -> None:
        """Buffer an incoming heartbeat (called by HTTP handler)."""

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Aggregate buffered heartbeats into session observations.

        Groups heartbeats by project and produces observations like:
        - {"project": "coremind", "language": "python", "session_minutes": 45, "start": "20:00", "end": "20:45"}
        """
```

The HTTP endpoint (`/self-model/heartbeat`) should be registered alongside the dashboard server or as a separate lightweight aiohttp/starlette route on port 9901.

---

## 4. Emitted Entity Types

After extraction (6B) processes this collector's output:

| Entity | Attributes |
| ------ | ---------- |
| `project:coremind` | `commits_today`, `last_commit`, `status`, `intensity` |
| `project:g-bot-immo` | `days_inactive`, `status=paused` |
| `routine:coding` | `time_window`, `days`, `avg_duration_minutes` |
| `identity:tech` | `languages`, `active_repos`, `tools` |

---

## 5. Success Criteria

1. GitHub collector returns valid `RawObservation` list when `gh` CLI is available.
2. GitHub collector returns empty list gracefully when `gh` is not installed.
3. VS Code collector aggregates 10 heartbeats into 1–2 session observations correctly.
4. VS Code extension builds without TypeScript errors (`npm run compile`).
5. Extension heartbeat payload matches the expected schema.
6. Tests pass with mocked subprocess output (no real `gh` calls in unit tests).

---

## 6. Explicitly Out of Scope

- Running the extraction engine on collected data (that's 6B's job).
- Communication collectors (6D).
- Health/calendar collectors (6E).
- Dashboard HTTP server setup (exists already from Phase 4).
