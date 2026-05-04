"""CoreMind Conversation Layer — Pillar #1 (Natural Conversation).

Transforms CoreMind from a one-way notification system into a two-way
conversational intelligence. The user can reply to notifications with
text messages, initiate conversations, and have natural back-and-forth
exchanges with CoreMind.
"""

from coremind.conversation.handler import ConversationHandler
from coremind.conversation.prompts import CONVERSATION_SYSTEM_PROMPT
from coremind.conversation.schemas import (
    Conversation,
    ConversationContext,
    Message,
    MessageRole,
)
from coremind.conversation.store import ConversationStore

__all__ = [
    "CONVERSATION_SYSTEM_PROMPT",
    "Conversation",
    "ConversationContext",
    "ConversationHandler",
    "ConversationStore",
    "Message",
    "MessageRole",
]
