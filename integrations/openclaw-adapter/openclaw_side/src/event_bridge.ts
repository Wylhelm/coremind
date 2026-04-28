/**
 * Subscribes to OpenClaw's internal event bus and forwards translated events
 * as CoreMind WorldEvent dicts through {@link CoreMindClient}.
 *
 * The actual OpenClaw SDK hook points (`openclaw.events.on(...)`) are marked
 * with TODO comments — wiring them is the final step when integrating with
 * a real OpenClaw gateway. The translation and dispatch pipeline below is
 * fully functional regardless.
 */

import { ulid } from "ulid";
import type { CoreMindClient } from "./coremind_client.js";
import type { AdapterConfig } from "./config.js";
import { channelAllowed } from "./config.js";
import {
  translateEvent,
  type OpenClawEvent,
  type UnsignedWorldEvent,
} from "./translators/oc_event_to_worldevent.js";

export interface EventSigner {
  /** Sign a translated WorldEvent and return the wire object (with `signature`). */
  signAndPack(translated: UnsignedWorldEvent | Record<string, unknown>): Promise<unknown>;
}

export class EventBridge {
  constructor(
    private readonly config: AdapterConfig,
    private readonly client: CoreMindClient,
    private readonly signer: EventSigner,
  ) { }

  /** Process one OpenClaw event: validate scope, translate, sign, ingest. */
  async handle(event: OpenClawEvent): Promise<void> {
    // Channel allowlist only applies to channel-bearing events.
    if ("channel" in event && !channelAllowed(event.channel, this.config.channelAllowlist)) {
      return;
    }
    const translated = translateEvent(event, {
      pluginId: this.config.coremindPluginId,
      pluginVersion: this.config.coremindPluginVersion,
      newEventId: () => `evt_${ulid()}`,
    });
    const signed = await this.signer.signAndPack(translated);
    await this.client.ingest(signed as never);
  }
}
