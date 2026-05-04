"""Conversation store — JSONL-backed persistence for conversations.

Each conversation is stored as one JSONL file in ~/.coremind/conversations/.
Active conversations are kept in memory for fast access.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from coremind.conversation.schemas import Conversation, Message

log = structlog.get_logger(__name__)

DEFAULT_CONVERSATIONS_DIR = Path.home() / ".coremind" / "conversations"


class ConversationStorePort(Protocol):
    """Protocol for conversation persistence (testable)."""

    async def save(self, conversation: Conversation) -> None: ...
    async def load(self, conversation_id: str) -> Conversation | None: ...
    async def list_active(self) -> list[Conversation]: ...
    async def archive(self, conversation_id: str) -> None: ...


class ConversationStore:
    """JSONL-backed conversation store.

    Each conversation is a file: ``{conversation_id}.jsonl``.
    Active conversations are tracked via an ``active.json`` index.
    """

    def __init__(self, conversations_dir: Path = DEFAULT_CONVERSATIONS_DIR) -> None:
        self._dir = conversations_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "active.json"
        self._cache: dict[str, Conversation] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def save(self, conversation: Conversation) -> None:
        """Persist a conversation to its JSONL file."""
        filepath = self._dir / f"{conversation.conversation_id}.jsonl"
        lines: list[str] = []
        for msg in conversation.messages:
            lines.append(
                json.dumps(
                    {
                        "role": msg.role.value,
                        "text": msg.text,
                        "timestamp": msg.timestamp.isoformat(),
                        "message_id": msg.message_id,
                    },
                    ensure_ascii=False,
                )
            )
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._cache[conversation.conversation_id] = conversation
        await self._update_index()

    async def load(self, conversation_id: str) -> Conversation | None:
        """Load a conversation from disk, or return cached version."""
        if conversation_id in self._cache:
            return self._cache[conversation_id]

        filepath = self._dir / f"{conversation_id}.jsonl"
        if not filepath.exists():
            return None

        messages: list[Message] = []
        created_at: datetime | None = None
        for line in filepath.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                msg = Message(
                    role=data["role"],
                    text=data["text"],
                    timestamp=datetime.fromisoformat(data.get("timestamp", "")),
                    message_id=data.get("message_id"),
                )
                messages.append(msg)
                if created_at is None:
                    created_at = msg.timestamp
            except (json.JSONDecodeError, KeyError, ValueError):
                log.warning("conversation.bad_line", file=filepath.name)
                continue

        conv = Conversation(
            conversation_id=conversation_id,
            messages=messages,
            created_at=created_at or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._cache[conversation_id] = conv
        return conv

    async def list_active(self) -> list[Conversation]:
        """Return all active conversations from the index."""
        if not self._index_path.exists():
            return []
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            active_ids = data.get("active", [])
        except (json.JSONDecodeError, KeyError):
            return []

        result: list[Conversation] = []
        for cid in active_ids:
            conv = await self.load(cid)
            if conv and conv.active:
                result.append(conv)
        return result

    async def archive(self, conversation_id: str) -> None:
        """Mark a conversation as archived."""
        conv = await self.load(conversation_id)
        if conv:
            conv.active = False
            await self.save(conv)
        self._cache.pop(conversation_id, None)
        await self._update_index()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _update_index(self) -> None:
        """Rebuild the active conversation index."""
        active_ids = [
            cid
            for cid, conv in self._cache.items()
            if conv.active
        ]
        self._index_path.write_text(
            json.dumps({"active": active_ids, "updated": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )
