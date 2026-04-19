# Conventions

**Version:** 0.1 (Design)
**Status:** Stable
**Audience:** All contributors to the CoreMind codebase

---

## Table of Contents

1. [Python Style](#1-python-style)
2. [Naming](#2-naming)
3. [Error Handling](#3-error-handling)
4. [Logging](#4-logging)
5. [Testing](#5-testing)
6. [Commit Messages](#6-commit-messages)

---

## 1. Python Style

### Tooling

- **Formatter + linter:** [Ruff](https://docs.astral.sh/ruff/). No Black, no isort, no flake8.
- **Type checker:** `mypy --strict`. Zero-tolerance for `Any` in public signatures.
- **Minimum Python version:** 3.12. Use PEP 695 type aliases (`type X = ...`), `typing.override`, and other 3.12+ features freely.

Run both tools with:

```bash
just lint   # ruff check . && mypy src/
```

### Type hints

Every function and method — public or private — carries type annotations on all parameters and the return type. No exceptions.

```python
# Correct
async def apply_event(self, event: WorldEvent) -> None: ...

# Wrong — return type missing
async def apply_event(self, event: WorldEvent): ...
```

Use `collections.abc` generics (`Sequence`, `Mapping`, `Callable`) in public signatures rather than `list` / `dict` / concrete types. Write `X | None` rather than `Optional[X]`.

### Async

All I/O is `async def`. Synchronous I/O (blocking file reads, blocking HTTP calls, `time.sleep`) is a defect in new code.

Use `asyncio.gather` for independent concurrent awaits:

```python
# Correct — concurrent
result_a, result_b = await asyncio.gather(fetch_a(), fetch_b())

# Acceptable only when ordering matters
result_a = await fetch_a()
result_b = await fetch_b(result_a)
```

### Data models

Use **Pydantic v2** (`BaseModel`) for every data structure that crosses a module boundary. Pydantic models are the contract between layers.

```python
from pydantic import BaseModel, Field

class WorldEvent(BaseModel):
    id: str
    timestamp: datetime
    source: str
    confidence: float = Field(ge=0.0, le=1.0)
```

### Datetimes

Always timezone-aware. Import and use `UTC`:

```python
from datetime import UTC, datetime

now = datetime.now(UTC)
```

`datetime.now()` without a timezone is a defect.

### Docstrings

Every public function and class has a [Google-style docstring](https://google.github.io/styleguide/pyguide.html#383-functions-and-methods) stating *what*, not *how*. Private helpers are documented at the author's discretion.

```python
def verify_signature(event: WorldEvent, public_key: Ed25519PublicKey) -> bool:
    """Verify the ed25519 signature on a WorldEvent.

    Args:
        event: The event whose signature field will be checked.
        public_key: The signer's public key.

    Returns:
        True if the signature is valid, False otherwise.

    Raises:
        SignatureError: If the signature field is malformed or missing.
    """
```

### `print` and `json`

- `print()` is forbidden in source code. Use `structlog` (see [§4 Logging](#4-logging)).
- `json.loads` / `json.dumps` are forbidden on data that will be signed or verified. Use `coremind.crypto.canonical_json`.

### Global mutable state

No module-level mutable variables. Pass dependencies through constructors (dependency injection). No service locators.

### Function length

Target ≤ 40 lines per function. Extract named helpers when exceeded.

---

## 2. Naming

| Context | Convention | Example |
| --- | --- | --- |
| Functions and methods | `snake_case` | `apply_event`, `get_snapshot` |
| Variables and parameters | `snake_case` | `event_id`, `signing_key` |
| Classes | `PascalCase` | `WorldEvent`, `AuditJournal` |
| Exceptions | `PascalCase` + `Error` suffix | `SignatureError`, `PluginTimeoutError` |
| Protocols / interfaces | `PascalCase` | `WorldStore`, `PluginHost` |
| Module-level constants | `UPPER_SNAKE_CASE` | `MAX_PAYLOAD_BYTES`, `SCHEMA_VERSION` |
| Type aliases | `PascalCase` | `type EntityId = str` |
| Private names | Leading underscore | `_build_canonical_form` |
| Test helpers / fixtures | Descriptive `snake_case` | `make_world_event`, `signing_key` |

### No abbreviations in public names

Public API uses full English words. Abbreviations obscure meaning and break searchability.

```python
# Correct
async def get_entity_snapshot(entity_id: EntityId) -> EntitySnapshot: ...

# Wrong
async def get_ent_snap(eid: str) -> Snapshot: ...
```

Widely accepted abbreviations (`id`, `url`, `http`, `llm`, `cpu`) are acceptable.

---

## 3. Error Handling

### Exception hierarchy

All custom exceptions inherit from `CoreMindError`, defined in `src/coremind/errors.py`:

```python
# coremind/errors.py
class CoreMindError(Exception):
    """Root exception for all CoreMind-specific errors."""

class SignatureError(CoreMindError):
    """Raised when an event signature fails verification."""

class PluginError(CoreMindError):
    """Raised for plugin lifecycle and communication failures."""
```

Always raise the most specific applicable exception. Callers catch by type; a generic `CoreMindError` is a last resort.

### Never swallow errors silently

```python
# Correct — log and re-raise with context
try:
    result = await store.apply_event(event)
except StorageError as exc:
    logger.error("world_store.apply_event_failed", event_id=event.id, exc_info=True)
    raise

# Wrong — swallowed
try:
    result = await store.apply_event(event)
except Exception:
    pass
```

`except Exception` without re-raising is always a defect. Catch specific exception types.

### Layer failures produce meta-events

When an error occurs in a layer, emit a `meta-event` on the `EventBus` rather than silently corrupting state. Errors must be visible to the system.

---

## 4. Logging

### Library

Use `structlog`. Never use `print` or the stdlib `logging` module directly in application code.

```python
import structlog

logger = structlog.get_logger(__name__)
```

Obtain a logger at module level. Do not pass loggers as parameters.

### Log calls

Log structured key-value pairs, not formatted strings:

```python
# Correct — structured, machine-readable
logger.info("event.applied", event_id=event.id, source=event.source)

# Wrong — unstructured string
logger.info(f"Applied event {event.id} from {event.source}")
```

### Log levels

| Level | When to use |
| --- | --- |
| `debug` | Verbose diagnostics, useful only during development |
| `info` | Normal operational milestones (event applied, plugin loaded) |
| `warning` | Recoverable anomalies (retry attempted, deprecated config key) |
| `error` | Failures requiring attention; always include `exc_info=True` |
| `critical` | System cannot continue; imminent shutdown |

### Output format

| Environment | Format | How configured |
| --- | --- | --- |
| Production | JSON (one object per line) | `structlog` JSON renderer |
| Development | Human-readable coloured text | `structlog` dev renderer |

Format is selected via `~/.coremind/config.toml`. See [Configuration](../src/coremind/config/).

### Security

API keys, secrets, and raw message bodies are **never** logged. Taint-track any string that originates from an external source and redact before it reaches a log call.

---

## 5. Testing

### Layout

`tests/` mirrors `src/coremind/` exactly. One test file per source module:

```text
src/coremind/world/store.py  →  tests/world/test_store.py
src/coremind/crypto/signatures.py  →  tests/crypto/test_signatures.py
```

Shared fixtures live in the nearest `conftest.py`. Fixtures in `tests/conftest.py` are available project-wide.

### Test names

Names describe the behavior under test, not the implementation:

```python
# Correct
def test_apply_event_rejects_tampered_signature(): ...

# Wrong
def test_apply_event_2(): ...
```

### Async tests

Mark with `@pytest.mark.asyncio`:

```python
import pytest

@pytest.mark.asyncio
async def test_event_bus_delivers_to_subscriber():
    bus = EventBus()
    ...
```

### Test categories

| Category | Marker | Requirement |
| --- | --- | --- |
| Unit | *(default)* | Fast, no I/O, no containers. Always run. |
| Integration | `@pytest.mark.integration` | Requires `docker-compose up`. Run with `pytest -m integration`. |
| E2E | `@pytest.mark.e2e` | Full daemon + plugins. Run sparingly. |

### Coverage targets

| Layer | Target |
| --- | --- |
| `crypto/`, `world/` | 90 %+ |
| `reasoning/`, `intention/`, `action/` | 80 %+ |
| CLI, plugin host, adapters | 70 %+ |

Run coverage with:

```bash
just test   # pytest --cov=src/coremind --cov-report=term-missing
```

### Determinism

- No `time.sleep`. Use `asyncio.wait_for` or event-driven waits.
- Inject a `Clock` protocol when a module needs "now". Do not call `datetime.now()` inside testable units.
- No network calls in unit tests. Use in-process fakes.

### What every public function needs

Every public function has at least one unit test. The test asserts the function's contract (not its implementation). Error paths count; happy paths alone are insufficient.

---

## 6. Commit Messages

CoreMind uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

### Format

```text
<type>(<scope>): <short summary>

[optional body]

[optional footer(s)]
```

- **Summary line:** imperative mood, ≤ 72 characters, no trailing period.
- **Body:** wrap at 72 characters. Explain *why*, not *what*.
- **Footer:** reference issues (`Closes #42`) or note breaking changes (`BREAKING CHANGE: ...`).

### Types

| Type | When to use |
| --- | --- |
| `feat` | New user-visible capability |
| `fix` | Bug fix |
| `refactor` | Behaviour-preserving restructuring |
| `docs` | Documentation only |
| `test` | Tests only |
| `chore` | Tooling, deps, housekeeping |
| `perf` | Performance improvement |

### Scopes

Use the top-level module name: `world`, `crypto`, `reasoning`, `action`, `plugin`, `config`, `ci`, `docs`.

### Examples

```text
feat(world): add snapshot query with point-in-time support

fix(crypto): reject events with future timestamps beyond 60 s

docs(phases): mark Task 0.6 complete in PHASE_0_FOUNDATIONS.md

chore(ci): pin grpcio-tools to 1.64.x for reproducible stubs
```

### Gate

Every commit must pass `just lint && just test` locally before push. CI enforces the same gate; a failing CI build blocks merge.
