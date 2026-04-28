/**
 * Dispatch an OpenClaw adapter-level event to its specific translator.
 *
 * Produces a WorldEvent-shaped dict matching the Python translators module
 * (`coremind_plugin_openclaw.translators.translate`). After translation the
 * caller signs the event with `EventSigner.signAndPack` and submits it via
 * `CoreMindClient.ingest`.
 */

import {
  translateMessageReceived,
  type MessageReceivedContext as TranslatorContext,
  type OCMessageReceived,
} from "./message_received.js";
import { translateSkillInvoked, type OCSkillInvoked } from "./skill_invoked.js";
import { translateCronExecuted, type OCCronExecuted } from "./cron_executed.js";
import { translateApprovalResponded, type OCApprovalResponded } from "./approval_event.js";

export type OpenClawEvent =
  | OCMessageReceived
  | OCSkillInvoked
  | OCCronExecuted
  | OCApprovalResponded;

export interface EntityRef {
  type: string;
  entity_id: string;
}

/**
 * Unsigned WorldEvent wire shape. Mirrors the proto fields that
 * ``MessageToDict(event, preserving_proto_field_name=True)`` produces on
 * the Python side — see translators.py for the canonical encoding contract.
 *
 * The `signature` field is intentionally absent: the signer attaches it
 * last, AFTER canonicalising the object with this exact key set.
 */
export interface UnsignedWorldEvent {
  id: string;
  timestamp: string;
  source: string;
  source_version: string;
  entity: EntityRef;
  attribute: string;
  value: Record<string, unknown>;
  confidence: number;
}

export class TranslationError extends Error { }

export function translateEvent(
  event: OpenClawEvent,
  ctx: TranslatorContext,
): UnsignedWorldEvent {
  switch (event.kind) {
    case "message.received":
      return translateMessageReceived(event, ctx);
    case "skill.invoked":
      return translateSkillInvoked(event, ctx);
    case "cron.executed":
      return translateCronExecuted(event, ctx);
    case "approval.responded":
      return translateApprovalResponded(event, ctx);
    default: {
      const _exhaustive: never = event;
      throw new TranslationError(`no translator for event kind: ${JSON.stringify(_exhaustive)}`);
    }
  }
}

export type { TranslatorContext };
