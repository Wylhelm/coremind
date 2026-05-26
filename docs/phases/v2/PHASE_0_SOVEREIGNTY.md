# Phase 0 — Sovereignty & Internationalisation

**Target:** CoreMind v2
**Duration estimate:** 3–4 hours
**Agent:** Opus in VS Code
**Prerequisites:** None — should ship BEFORE Phase 1 becomes user-visible
**Priority:** 🔴 Critical — personal data currently hardcoded in the repository

---

## 1. Problem Statement

### 1.1 The Repository Leaks Guillaume's Personal Data

The CoreMind repository currently contains hardcoded personal information in **7 source files**:

| File | Leaked Info |
|------|-------------|
| `src/coremind/reasoning/prompts.py` | "Guillaume (47, Québec). Cats: Poukie (noire), Timimi (noire/caramel), Minuit (noir)." |
| `src/coremind/conversation/prompts.py` | "Tu es CoreMind, le compagnon IA de Guillaume. Ses chats (Poukie, Timimi, Minuit), sa fille Aurélie." |
| `src/coremind/intention/prompts.py` | "Send a Telegram notification to Guillaume", "Guillaume's salary appears as a transaction…", "Minuit est dans son panier et Poukie te regarde" |
| `src/coremind/action/executor.py` | "Guillaume receives…", "Guillaume gets…" string patterns |
| `src/coremind/presence/scheduler.py` | "Bonjour Guillaume. Bonne journée." |
| `docs/` | Executive summary, architecture docs referencing Guillaume's home |

This is unacceptable for an open-source project. **No one's personal context should live in a public repository.**

### 1.2 French is Hardcoded Into the Architecture

Every LLM-facing prompt contains: `Language: ALL messages MUST be in French.`

There is no `language` setting in `config.toml`. A user who clones CoreMind in Japan cannot use it in Japanese without editing source code.

### 1.3 Timezone is Scattered and Inconsistent

- `America/Toronto` is hardcoded in `intention/loop.py`, `conversation/handler.py`, and `dashboard/views.py`
- `quiet_hours.timezone` exists in `config.toml` but is only used by the quiet-hours policy

---

## 2. Design

### 2.1 Core Principle: CoreMind Learns, It Doesn't Read a Biography

The only things in `[personalization]` are what sensors **cannot observe**: how to address the user, what language to speak, and what timezone they're in.

Everything else — pets, family members, room layouts, habits, health patterns, preferences — is discovered through L1→L2 perception and L3 memory, then refined by the L8 meta-loop over time. That is the entire point of CoreMind.

### 2.2 Config Section: `[personalization]`

```toml
# ~/.coremind/config.toml

[personalization]
# ── Bare minimum identity ────────────────────────────────────────────
# Only what sensors CANNOT observe. CoreMind learns the rest.
user_name = "Guillaume"
greeting_name = "Guillaume"   # falls back to user_name if empty

# ── Language ─────────────────────────────────────────────────────────
# Supported: "fr", "en", "auto"
language = "fr"

# ── Timezone ─────────────────────────────────────────────────────────
timezone = "America/Toronto"

# ── Notification style ───────────────────────────────────────────────
# "je" = first person ("Je te préviens si…")
# "il" = neutral ("The system detected…")
notification_style = "je"
```

### 2.3 PersonalizationConfig Model

```python
"""User personalization — bare minimum.

CoreMind LEARNS its user, it doesn't read a biography.
Only what sensors cannot observe: name, language, timezone.
"""

from pydantic import BaseModel, Field


class PersonalizationConfig(BaseModel):
    language: str = Field(default="en", pattern=r"^(fr|en|auto)$")
    user_name: str = Field(default="User")
    timezone: str = Field(default="UTC")
    greeting_name: str = Field(default="")
    notification_style: str = Field(default="je", pattern=r"^(je|il)$")

    @property
    def language_name(self) -> str:
        return {
            "fr": "French", "en": "English",
            "auto": "the user's detected language",
        }[self.language]

    @property
    def effective_greeting(self) -> str:
        return self.greeting_name or self.user_name
```

**Note:** No `user_context` field. CoreMind discovers cats from camera, family from presence sensors, habits from time-series data. A biography in config.toml would bypass the system's entire reason for existing. The reasoning prompt says: "You are new here. Learn from the sensors and data streams before forming conclusions."

### 2.4 Template Variables

```
{{ user_name }}          → "Guillaume"
{{ language_name }}      → "French"
{{ language_directive }} → "MUST be in French"
{{ timezone }}           → "America/Toronto"
{{ greeting_name }}      → "Guillaume"
{{ notification_style }} → "je"
```

### 2.5 Prompt Changes — Before / After

#### `reasoning/prompts.py`

| Before (hardcoded) | After (templated) |
|---|---|
| `The user is Guillaume (47, Québec). Cats: Poukie (noire), Timimi, Minuit. Home: sensors in chambre/couloir/extérieur…` | **REMOVED.** CoreMind observes sensors → discovers rooms. Observes cameras → discovers cats. |
| `## About Guillaume` + biography section | **REMOVED.** Replaced with: "You are watching over {{ user_name }}'s world. You are new here — learn from the sensors and data streams. Do not assume facts you have not observed." |
| `Your observations MUST be in French` | `Your observations MUST be in {{ language_name }}` |

#### `conversation/prompts.py`

| Before (hardcoded) | After (templated) |
|---|---|
| `Tu es CoreMind, le compagnon IA de Guillaume.` | `Tu es CoreMind, le compagnon IA de {{ user_name }}.` |
| `ses chats (Poukie, Timimi, Minuit), sa fille Aurélie` | **REMOVED.** CoreMind discovers this through observation. The LLM prompt says: "Tu connais ton contexte à travers ce que tu as observé." |
| `Tu parles TOUJOURS en français.` | `Tu parles TOUJOURS en {{ language_name }}.` |

#### `intention/prompts.py`

| Before (hardcoded) | After (templated) |
|---|---|
| `Send a Telegram notification to Guillaume` | `Send a Telegram notification to {{ user_name }}` |
| `Guillaume's salary appears as a transaction…` | Removed — inferred from Firefly data, not biographical config |
| `- title: "Chats dans le salon 🐱" message: "Minuit est dans son panier…"` | Removed — the LLM generates cat notifications IF it has observed cats through presence/camera |
| `Language: ALL messages MUST be in French.` | `Language: ALL messages MUST be in {{ language_name }}.` |

#### `action/executor.py`

| Before (hardcoded) | After |
|---|---|
| `for prefix in ("User receives ", "Guillaume receives ", "Guillaume gets ", …):` | `for prefix in ("User receives ", "The user receives ", …):` — no name-specific patterns |

#### `presence/scheduler.py`

| Before (hardcoded) | After |
|---|---|
| `message="Bonjour Guillaume. Bonne journée."` | `message=f"Bonjour {config.effective_greeting}. Bonne journée."` |

### 2.6 Timezone Unification

```python
# src/coremind/personalization/config.py

def get_timezone(config: PersonalizationConfig) -> ZoneInfo:
    return ZoneInfo(config.timezone)
```

| File | Before | After |
|------|--------|-------|
| `intention/loop.py` | `ZoneInfo("America/Toronto")` | `get_timezone(config)` |
| `conversation/handler.py` | `"America/Toronto"` | `str(config.timezone)` |
| `dashboard/views.py` | `_LOCAL_TZ = ZoneInfo("America/Toronto")` | `get_timezone(config)` |

### 2.7 Generated Config on `coremind init`

```toml
# ═════════════════════════════════════════════════════════════════════
# Personalization — edit these three values. CoreMind learns the rest.
# ═════════════════════════════════════════════════════════════════════
[personalization]
user_name = "Your Name"
greeting_name = ""
language = "en"
timezone = "UTC"
notification_style = "je"
```

---

## 3. Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `src/coremind/personalization/__init__.py` | Package init |
| `src/coremind/personalization/config.py` | `PersonalizationConfig`, `get_timezone()` |
| `tests/personalization/test_config.py` | Config loading, template rendering, timezone tests |

### Modified Files

| File | Change |
|------|--------|
| `src/coremind/reasoning/prompts.py` | Replace hardcoded biography → "learn from observation", "Guillaume" → `{{ user_name }}`, "French" → `{{ language_name }}` |
| `src/coremind/intention/prompts.py` | Replace "Guillaume" → `{{ user_name }}`, remove cat examples, "French" → `{{ language_name }}` |
| `src/coremind/conversation/prompts.py` | Replace Guillaume/cats/Aurélie → `{{ user_name }}`, remove biography → "learned through observation", "français" → `{{ language_name }}` |
| `src/coremind/action/executor.py` | Remove "Guillaume receives/gets" patterns, simplify transforms |
| `src/coremind/presence/scheduler.py` | Replace "Bonjour Guillaume" → `f"Bonjour {config.effective_greeting}"` |
| `src/coremind/intention/loop.py` | Replace `ZoneInfo("America/Toronto")` → `get_timezone(config)` |
| `src/coremind/conversation/handler.py` | Replace hardcoded timezone string |
| `src/coremind/dashboard/views.py` | Replace `_LOCAL_TZ` |
| `src/coremind/core/daemon.py` | Load `PersonalizationConfig` on startup, inject into prompt renderers |
| `src/coremind/core/config.py` | Add `PersonalizationConfig` to `DaemonConfig` |
| `~/.coremind/config.toml` | Add commented `[personalization]` section |
| `docs/` | Remove personal context from all docs; replace with generic examples |

### Git History (separate step, documented)

After moving personal data to config, the Git history still contains hardcoded values. Use `git filter-repo` to scrub BEFORE making the repo public.

---

## 4. Success Criteria

- [ ] No personal data (name, cats, daughter, email, city, age) in any source file under `src/`
- [ ] `config.toml` has a commented `[personalization]` section with sane defaults
- [ ] System works correctly with default (empty/generic) `[personalization]` — starts in "learning mode"
- [ ] `language = "en"` produces English output from all LLM prompts
- [ ] `language = "fr"` produces French output (backward compatible)
- [ ] Timezone from config.toml is used consistently in all components
- [ ] Greeting uses `greeting_name` (falls back to `user_name`)
- [ ] All existing tests pass with templated prompts
- [ ] Reasoning prompt says "learn from observation" instead of reading a biography

---

## 5. Effort Estimate

| Task | Time |
|------|------|
| Create `personalization/config.py` | 20 min |
| Templatize `reasoning/prompts.py` (remove biography) | 30 min |
| Templatize `intention/prompts.py` | 20 min |
| Templatize `conversation/prompts.py` (remove biography) | 20 min |
| Clean `action/executor.py` patterns | 15 min |
| Clean `presence/scheduler.py` greeting | 10 min |
| Unify timezone across 3 files | 15 min |
| Wire into `DaemonConfig` + daemon startup | 20 min |
| Unit tests | 30 min |
| Update docs | 20 min |
| **Total** | **~3 hours** |
