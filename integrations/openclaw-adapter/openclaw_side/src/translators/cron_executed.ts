import type { UnsignedWorldEvent } from "./oc_event_to_worldevent.js";
import type { MessageReceivedContext as TranslatorContext } from "./message_received.js";

export interface OCCronExecuted {
  kind: "cron.executed";
  cron_id: string;
  skill_name: string;
  ok?: boolean;
  timestamp: string;
}

export function translateCronExecuted(
  event: OCCronExecuted,
  ctx: TranslatorContext,
): UnsignedWorldEvent {
  return {
    id: ctx.newEventId(),
    timestamp: event.timestamp,
    source: ctx.pluginId,
    source_version: ctx.pluginVersion,
    entity: { type: "cron_job", entity_id: event.cron_id },
    attribute: "cron_executed",
    value: {
      cron_id: event.cron_id,
      skill_name: event.skill_name,
      ok: event.ok ?? true,
    },
    confidence: 1.0,
  };
}
