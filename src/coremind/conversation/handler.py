"""Conversation Handler — the brain of CoreMind's conversational ability.

Receives text messages from the user (via any notification adapter),
generates responses using the configured LLM, and maintains conversation
state across sessions.

This is the core of Pillar #1 (Natural Conversation) for CoreMind v0.3.0.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from coremind.conversation.prompts import (
    CONVERSATION_CONTEXT_PROMPT,
    CONVERSATION_SYSTEM_PROMPT,
    INTENT_CONVERSATION_PROMPT,
)
from coremind.conversation.schemas import (
    Conversation,
    ConversationContext,
    Message,
    MessageRole,
)
from coremind.conversation.store import ConversationStore
from coremind.reasoning.llm import LLM

log = structlog.get_logger(__name__)

# How many messages back to include in conversation history for LLM context
MAX_CONTEXT_MESSAGES = 20
# Max conversation active time before auto-archive (seconds) — 24h
CONVERSATION_TTL_SECONDS = 86400


class ConversationHandler:
    """The conversational brain of CoreMind.

    Handles inbound text messages, generates responses via LLM, and
    maintains conversation state.

    Args:
        llm: The LLM instance for response generation.
        store: Persistence for conversations.
        get_narrative: Optional callback to fetch current narrative state text.
        max_context_messages: Max messages to include in LLM context.
    """

    def __init__(
        self,
        llm: LLM,
        store: ConversationStore | None = None,
        *,
        get_narrative: Callable[[], Awaitable[str]] | None = None,
        max_context_messages: int = MAX_CONTEXT_MESSAGES,
    ) -> None:
        self._llm = llm
        self._store = store or ConversationStore()
        self._get_narrative = get_narrative
        self._max_context = max_context_messages

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_message(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        intent_id: str | None = None,
        intent_description: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, Conversation]:
        """Process an incoming text message and return a response.

        Args:
            text: The user's text message.
            conversation_id: Existing conversation to continue, or None for new.
            intent_id: Optional L5 intent this conversation is about.
            intent_description: Human-readable description of the intent.
            user_id: Optional user identifier for the responding user.

        Returns:
            A (response_text, conversation) tuple.
        """
        # Load or create conversation
        conversation: Conversation
        if conversation_id:
            existing = await self._store.load(conversation_id)
            if existing and existing.active:
                conversation = existing
            else:
                conversation = Conversation(
                    conversation_id=conversation_id,
                    intent_id=intent_id,
                )
        else:
            cid = f"conv_{uuid.uuid4().hex[:12]}"
            conversation = Conversation(
                conversation_id=cid,
                intent_id=intent_id,
            )

        # Record user message
        user_msg = Message(
            role=MessageRole.USER,
            text=text,
            message_id=user_id,
        )
        conversation.add_message(user_msg)

        # Build context
        narrative_text = ""
        if self._get_narrative:
            try:
                narrative_text = await self._get_narrative()
            except Exception:
                log.warning("conversation.narrative_fetch_failed", exc_info=True)

        # Select the right prompt
        if intent_id and intent_description and not conversation.messages[:-1]:
            # This is the first reply to an intent notification
            prompt = INTENT_CONVERSATION_PROMPT.format(
                intent_description=intent_description,
                user_reply=text,
                conversation_history="(new conversation)",
            )
        else:
            history = conversation.conversation_text(max_messages=self._max_context)
            narrative_block = narrative_text if narrative_text else "(no current narrative context)"
            prompt = CONVERSATION_CONTEXT_PROMPT.format(
                system_prompt=CONVERSATION_SYSTEM_PROMPT,
                narrative_context=narrative_block,
                conversation_history=history,
                user_message=text,
            )

        # Generate response
        response_text = await self._generate_response(prompt)

        # Record CoreMind message
        cm_msg = Message(
            role=MessageRole.COREMIND,
            text=response_text,
        )
        conversation.add_message(cm_msg)

        # Save
        await self._store.save(conversation)

        log.info(
            "conversation.turn_complete",
            conversation_id=conversation.conversation_id,
            message_count=conversation.message_count,
        )

        return response_text, conversation

    async def get_active_context(self) -> ConversationContext:
        """Build the current conversation context for reasoning cycles."""
        active = await self._store.list_active()
        narrative = ""
        if self._get_narrative:
            try:
                narrative = await self._get_narrative()
            except Exception:
                pass

        return ConversationContext(
            active_conversations=active,
            narrative_state_text=narrative,
        )

    async def archive_old_conversations(self, ttl_seconds: int = CONVERSATION_TTL_SECONDS) -> int:
        """Archive conversations older than TTL. Returns count archived."""
        active = await self._store.list_active()
        now = datetime.now(UTC)
        count = 0
        for conv in active:
            age = (now - conv.updated_at).total_seconds()
            if age > ttl_seconds:
                await self._store.archive(conv.conversation_id)
                count += 1
                log.info("conversation.auto_archived", conversation_id=conv.conversation_id, age_s=age)
        return count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _generate_response(self, prompt: str) -> str:
        """Call the LLM to generate a conversational response."""
        try:
            from pydantic import BaseModel, Field

            class TextResponse(BaseModel):
                response: str = Field(min_length=1)

            result = await self._llm.complete_structured(
                layer="reasoning_fast",
                system=CONVERSATION_SYSTEM_PROMPT,
                user=prompt,
                response_model=TextResponse,
                max_tokens=300,
            )
            return result.response.strip()
        except Exception as exc:
            log.error("conversation.llm_error", error=str(exc))
            return (
                "Désolé, j'ai du mal à formuler une réponse pour l'instant. "
                "Peut-être qu'on peut essayer autrement?"
            )
