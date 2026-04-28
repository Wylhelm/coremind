import type { UnsignedWorldEvent } from "./oc_event_to_worldevent.js";
import type { MessageReceivedContext as TranslatorContext } from "./message_received.js";

export type ApprovalOutcome = "approved" | "denied" | "timeout" | "cancelled";

export interface OCApprovalResponded {
  kind: "approval.responded";
  approval_id: string;
  outcome: ApprovalOutcome;
  feedback?: string;
  timestamp: string;
}

export function translateApprovalResponded(
  event: OCApprovalResponded,
  ctx: TranslatorContext,
): UnsignedWorldEvent {
  return {
    id: ctx.newEventId(),
    timestamp: event.timestamp,
    source: ctx.pluginId,
    source_version: ctx.pluginVersion,
    entity: { type: "approval", entity_id: event.approval_id },
    attribute: "approval_responded",
    value: {
      approval_id: event.approval_id,
      outcome: event.outcome.toLowerCase(),
      feedback: event.feedback ?? "",
    },
    confidence: 1.0,
  };
}
