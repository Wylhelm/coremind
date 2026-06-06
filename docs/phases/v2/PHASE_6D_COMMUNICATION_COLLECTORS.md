# Phase 6D — Communication Collectors (Telegram, WhatsApp, Email)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A complete
**Estimated effort:** 3–4 hours

---

## 1. Goal

Build relationship graph and social routine patterns from communication metadata. After this sub-phase:

- Telegram collector reads existing OpenClaw bridge events for contact metadata.
- WhatsApp collector reads events from WhatsApp bridge (after QR pairing).
- Email collector reads GOG/IMAP plugin events for sender + subject metadata.
- All collectors produce `RawObservation` with `category="communication"`.
- **No message bodies are ever read** — only sender, recipient, timestamp, direction.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/collectors/telegram.py` | Telegram metadata collector. |
| `src/coremind/self_model/collectors/whatsapp.py` | WhatsApp metadata collector. |
| `src/coremind/self_model/collectors/email.py` | Email metadata collector (GOG/IMAP). |
| `tests/self_model/collectors/test_telegram.py` | Tests. |
| `tests/self_model/collectors/test_whatsapp.py` | Tests. |
| `tests/self_model/collectors/test_email.py` | Tests. |

---

## 3. Tasks for the Coding Agent

### 6D.1 Telegram Collector

**File:** `src/coremind/self_model/collectors/telegram.py`

```python
class TelegramCollector:
    """Extracts relationship metadata from Telegram via OpenClaw events.

    Reads WorldEvents with source containing 'openclaw' or 'telegram'
    from the L2 store. Extracts contact name, direction, timestamp.
    """

    source_id: str = "telegram"
    category: str = "communication"

    def __init__(self, world_store: WorldStore) -> None: ...

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Query L2 for Telegram-related events since timestamp.

        Returns observations with data like:
        - {"contact": "Aurélie", "direction": "received", "channel": "telegram"}
        - {"contact": "Jeff", "direction": "sent", "channel": "telegram"}
        """
```

**Implementation:** Query the existing WorldStore for events where `source` contains "openclaw" and entity type relates to messaging. Extract:
- Contact name (from entity or attribute)
- Direction (sent/received)
- Timestamp
- Conversation ID (if available)

### 6D.2 WhatsApp Collector

**File:** `src/coremind/self_model/collectors/whatsapp.py`

Same pattern as Telegram but looks for WhatsApp bridge events. May share infrastructure via a base `MessagingCollector` if patterns are identical.

### 6D.3 Email Collector

**File:** `src/coremind/self_model/collectors/email.py`

```python
class EmailCollector:
    """Extracts relationship and priority metadata from email events.

    Reads GOG plugin or Gmail IMAP plugin events from L2.
    Only processes sender and subject — never email body.
    """

    source_id: str = "email"
    category: str = "communication"

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Query L2 for email-related events since timestamp.

        Returns observations with data like:
        - {"sender": "boss@work.com", "subject": "Q3 planning", "direction": "received", "channel": "email"}
        - {"sender": "newsletter@tech.io", "subject": "...", "direction": "received", "channel": "email", "is_newsletter": true}
        """
```

---

## 4. Privacy Constraints

- **Email:** Subject + sender ONLY. Body content never enters self-model. Bodies live in L3 semantic memory (separate system).
- **Messaging:** Contact name + timestamp + direction ONLY. Message text is never read by these collectors.
- All metadata is treated as `tainted` until the extraction engine processes it.
- Extraction prompts explicitly state "you only see metadata, not content."

---

## 5. Emitted Entity Types (after extraction)

| Entity | Attributes |
| ------ | ---------- |
| `person:aurelie` | `last_contact`, `contact_frequency_days`, `primary_channel` |
| `person:jeff` | `last_contact`, `contact_frequency_days` |
| `routine:social` | `peak_hours`, `active_contacts_count` |

---

## 6. Success Criteria

1. Telegram collector returns valid `RawObservation` list from mocked WorldStore events.
2. Email collector correctly separates sender from subject.
3. No collector ever includes message body content in its output.
4. WhatsApp collector follows same interface as Telegram.
5. All tests pass with mocked WorldStore queries.

---

## 7. Explicitly Out of Scope

- Message body analysis or sentiment detection.
- Direct Telegram/WhatsApp API access (uses existing plugin events in L2).
- Notification sending.
