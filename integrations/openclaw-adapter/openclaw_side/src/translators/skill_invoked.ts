import type { UnsignedWorldEvent } from "./oc_event_to_worldevent.js";
import type { MessageReceivedContext as TranslatorContext } from "./message_received.js";

export interface OCSkillInvoked {
  kind: "skill.invoked";
  skill_name: string;
  call_id: string;
  invoker?: string;
  parameters?: Record<string, unknown>;
  timestamp: string;
}

export function translateSkillInvoked(
  event: OCSkillInvoked,
  ctx: TranslatorContext,
): UnsignedWorldEvent {
  const keys = Object.keys(event.parameters ?? {}).sort();
  return {
    id: ctx.newEventId(),
    timestamp: event.timestamp,
    source: ctx.pluginId,
    source_version: ctx.pluginVersion,
    entity: { type: "skill_run", entity_id: event.call_id },
    attribute: "skill_invoked",
    value: {
      skill_name: event.skill_name,
      call_id: event.call_id,
      invoker: event.invoker ?? "openclaw",
      parameters_keys: keys,
    },
    confidence: 1.0,
  };
}
