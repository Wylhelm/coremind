# Phase 6G — Declared Facts & User Feedback Loop

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A + 6B complete
**Estimated effort:** 2–3 hours

---

## 1. Goal

Allow the user to explicitly declare facts (confidence=1.0) and correct or reject inferred facts. After this sub-phase:

- User can declare facts via conversation ("Ma fille Aurélie habite à Montréal").
- User can declare facts via CLI (`coremind self-model set`).
- Seed file (`~/.coremind/self_model_seed.toml`) bootstraps initial knowledge.
- User corrections promote questioned facts to declared or reject them.
- Question generation produces L4 hypotheses for conversational verification.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/declared.py` | Declaration handler (parse + persist). |
| `src/coremind/self_model/feedback.py` | User correction/rejection pipeline. |
| `tests/self_model/test_declared.py` | Tests. |
| `tests/self_model/test_feedback.py` | Tests. |

---

## 3. Tasks for the Coding Agent

### 6G.1 Declaration Handler

**File:** `src/coremind/self_model/declared.py`

```python
class DeclarationHandler:
    """Processes explicit user declarations into self-model facts.

    Three input channels:
    1. Conversation — natural language parsed by LLM.
    2. CLI — structured key-value pairs.
    3. Seed file — TOML file loaded at startup.
    """

    def __init__(self, llm: LLM, store: SelfModelStore) -> None: ...

    async def from_conversation(self, user_message: str) -> list[SelfFact]:
        """Parse natural language into declared facts via LLM.

        Example input: "Ma fille Aurélie habite à Montréal"
        Output: SelfFact(entity_type="person", entity_id="aurelie",
                         attribute="location", value="Montréal",
                         confidence=1.0, method="declared")
        """

    async def from_cli(
        self,
        entity_type: SelfModelEntityType,
        entity_id: str,
        attribute: str,
        value: JsonValue,
    ) -> SelfFact:
        """Create a declared fact from structured CLI input.

        Always confidence=1.0, method="declared", source="user".
        """

    async def from_seed_file(self, path: Path) -> list[SelfFact]:
        """Load initial declarations from a TOML seed file.

        Seed file format:
        [[person]]
        id = "aurelie"
        name = "Aurélie"
        relationship = "fille"
        location = "Montréal"
        birthday = "2001-05-15"

        [[goal]]
        id = "retirement"
        description = "Retraite à 65 ans dans un chalet"
        target_year = 2043
        """
```

### 6G.2 LLM Declaration Parser

The `from_conversation()` method uses `LLM.complete_structured()` with a response model:

```python
class ParsedDeclaration(BaseModel):
    """LLM output when parsing a user declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: list[ExtractedFact]
    is_declaration: bool  # False if the message is not a declaration
    clarification_needed: str | None  # Question to ask if ambiguous
```

The prompt instructs the LLM to:
- Identify explicit personal statements ("my daughter", "I live in", "my goal is").
- NOT extract implicit facts (those are for the extractor, not declarations).
- Ask for clarification when ambiguous.

### 6G.3 Feedback Pipeline

**File:** `src/coremind/self_model/feedback.py`

```python
class FeedbackHandler:
    """Handles user corrections and confirmations of inferred facts."""

    def __init__(self, store: SelfModelStore) -> None: ...

    async def confirm(self, fact_id: str) -> SelfFact:
        """User confirms an inferred fact — promote to declared (confidence=1.0).

        Creates a new version with method="declared" superseding the old one.
        """

    async def reject(self, fact_id: str, reason: str = "") -> None:
        """User rejects a fact — deactivate with reason.

        Logs rejection to audit. The fact will not reappear in prompts.
        """

    async def correct(
        self, fact_id: str, new_value: JsonValue, reason: str = ""
    ) -> SelfFact:
        """User provides a correction — supersede with declared version."""

    def pending_questions(self, limit: int = 5) -> list[SelfFact]:
        """Get L4 (questioned) facts that can be asked in conversation.

        Rate limit: max 1 question per conversation turn (enforced by caller).
        """
```

### 6G.4 Seed File Format

**File:** `~/.coremind/self_model_seed.toml` (example)

```toml
# Self-Model bootstrap declarations
# All facts here are loaded as method="declared", confidence=1.0

[[person]]
id = "aurelie"
name = "Aurélie"
relationship = "fille"
location = "Montréal"
birthday = "2001-05-15"

[[person]]
id = "jeff"
name = "Jeff"
relationship = "ami"

[[goal]]
id = "retirement"
description = "Retraite à 65 ans dans un chalet"
target_year = 2043

[[goal]]
id = "sante"
description = "Sevrage vape et amélioration du sommeil"

[[project]]
id = "coremind"
name = "CoreMind"
status = "active"

[[project]]
id = "g-bot-immo"
name = "G-Bot Immo"
status = "paused"

[[identity]]
id = "tech"
domain = "tech"
role = "architecte_ia"
languages = ["python", "typescript"]

[[preference]]
id = "voice_style"
domain = "voice"
attribute = "style"
value = "radio"
```

---

## 4. Success Criteria

1. `from_conversation("Ma fille Aurélie habite à Montréal")` produces a valid person fact (mocked LLM).
2. `from_cli("person", "jeff", "location", "Québec")` persists a fact with confidence=1.0.
3. `from_seed_file()` loads all entries from a sample TOML and persists them.
4. `confirm()` promotes a fact to declared and supersedes the old version.
5. `reject()` deactivates a fact and logs the reason.
6. Seed file loading is idempotent (running twice doesn't duplicate facts).

---

## 5. Explicitly Out of Scope

- Automatic question asking in conversations (6H integration handles that).
- CLI command wiring (6I).
- Multi-turn conversation parsing (single message → facts).
