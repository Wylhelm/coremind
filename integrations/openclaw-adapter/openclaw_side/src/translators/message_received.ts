/**
 * Translator: OpenClaw "message.received" → CoreMind WorldEvent (wire shape).
 *
 * Produces a dict whose structure matches
 * `coremind_plugin_openclaw.translators.translate_message_received()` on the
 * Python side:
 *
 *   { id, timestamp, source, source_version, entity, attribute, value,
 *     confidence, signature? }
 *
 * The full message body is *never* placed on the wire: only a truncated
 * excerpt and the length. The full body stays in OpenClaw's mem0 store
 * and is only surfaced to CoreMind on demand via a Mem0Query.
 */

import { MESSAGE_EXCERPT_MAX_CHARS } from "./constants.js";
import type { UnsignedWorldEvent } from "./oc_event_to_worldevent.js";

export interface OCMessageReceived {
  kind: "message.received";
  channel: string;
  chat_id: string;
  sender_id: string;
  sender_name: string;
  text: string;
  has_media?: boolean;
  timestamp: string; // ISO 8601
}

export interface MessageReceivedContext {
  pluginId: string;
  pluginVersion: string;
  newEventId: () => string;
}

export function translateMessageReceived(
  event: OCMessageReceived,
  ctx: MessageReceivedContext,
): UnsignedWorldEvent {
  const excerpt = event.text.slice(0, MESSAGE_EXCERPT_MAX_CHARS);
  const truncated = event.text.length > MESSAGE_EXCERPT_MAX_CHARS;
  return {
    id: ctx.newEventId(),
    timestamp: event.timestamp,
    source: ctx.pluginId,
    source_version: ctx.pluginVersion,
    entity: { type: "conversation", entity_id: event.chat_id },
    attribute: "message_received",
    value: {
      from: { id: event.sender_id, display_name: event.sender_name },
      text_excerpt: excerpt,
      length_chars: event.text.length,
      has_media: event.has_media ?? false,
      truncated,
    },
    confidence: 1.0,
  };
}
