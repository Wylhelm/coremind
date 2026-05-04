"""Conversation schemas — messages, turns, conversation state.

A Conversation is a threaded exchange between CoreMind and the user.
Conversations can be initiated by either party and persist across
reasoning cycles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MessageRole(StrEnum):
    """Who sent this message."""

    USER = "user"
    COREMIND = "coremind"
    SYSTEM = "system"


class Message(BaseModel):
    """A single message in a conversation."""

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    text: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    message_id: str | None = None  # Channel-specific message id (Telegram msg_id)


class Conversation(BaseModel):
    """A thread of messages between CoreMind and the user.

    Can be linked to a specific intent_id for contextual conversations
    (e.g., discussing a particular suggestion) or be an open-ended chat.
    """

    model_config = ConfigDict(frozen=False)

    conversation_id: str = Field(min_length=1)
    intent_id: str | None = None  # Optional link to an L5 intent
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active: bool = True  # False when conversation is closed/archived
    summary: str = ""  # LLM-generated summary of the conversation

    @property
    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def add_message(self, message: Message) -> None:
        self.messages.append(message)
        self.updated_at = datetime.now(UTC)

    def conversation_text(self, max_messages: int = 20) -> str:
        """Return the conversation as formatted text for LLM context."""
        recent = self.messages[-max_messages:]
        lines: list[str] = []
        for msg in recent:
            prefix = (
                "👤 User"
                if msg.role == MessageRole.USER
                else "🤖 CoreMind" if msg.role == MessageRole.COREMIND
                else "📋"
            )
            lines.append(f"{prefix}: {msg.text}")
        return "\n".join(lines)


class InboundTextMessage(BaseModel):
    """A text message received from a notification channel."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    conversation_id: str | None = None
    responder: str = ""
    channel: str = ""
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reply_to_message_id: str | None = None


class ConversationContext(BaseModel):
    """Full context injected into reasoning and conversation prompts."""

    model_config = ConfigDict(frozen=False)

    active_conversations: list[Conversation] = Field(default_factory=list)
    recent_user_messages: list[Message] = Field(default_factory=list)
    narrative_state_text: str = ""  # From Pillar 4 narrative identity

    def recent_context_text(self) -> str:
        """Build a compact context block for LLM prompts."""
        parts: list[str] = []
        if self.narrative_state_text:
            parts.append(f"## Narrative Context\n{self.narrative_state_text}")
        if self.active_conversations:
            parts.append("## Active Conversations")
            for conv in self.active_conversations[:3]:
                parts.append(f"### {conv.conversation_id}")
                parts.append(conv.conversation_text(max_messages=10))
        if self.recent_user_messages:
            parts.append("## Recent User Messages")
            for msg in self.recent_user_messages[-5:]:
                parts.append(f"- {msg.text[:200]}")
        return "\n\n".join(parts)
