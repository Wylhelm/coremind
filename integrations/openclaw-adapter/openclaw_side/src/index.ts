/**
 * Entry point for the OpenClaw extension.
 *
 * This module boots the extension inside an OpenClaw gateway:
 *   1. Loads config from the gateway's plugin-config store.
 *   2. Opens the gRPC client to the CoreMind-side CoreMindHalf service.
 *   3. Starts the OpenClawHalf gRPC server (CoreMind → OpenClaw actions).
 *   4. Subscribes to OpenClaw events and forwards them via CoreMindClient.
 *
 * The actual signing of WorldEvents is delegated to an `EventSigner`
 * implementation — typically backed by a keypair managed by OpenClaw's
 * secrets system.
 */

import { CoreMindClient } from "./coremind_client.js";
import { DEFAULT_CONFIG, type AdapterConfig } from "./config.js";
import { EventBridge, type EventSigner } from "./event_bridge.js";
import { buildHandlers, wireEventSubscriptions, type OpenClawHost } from "./openclaw_extension.js";
import { startRpcServer } from "./rpc_server.js";

export async function start(
  host: OpenClawHost,
  signer: EventSigner,
  overrides: Partial<AdapterConfig> = {},
): Promise<{ stop: () => Promise<void> }> {
  const config = { ...DEFAULT_CONFIG, ...overrides };

  const client = new CoreMindClient(config);
  await client.connectWithBackoff();

  const bridge = new EventBridge(config, client, signer);
  const handlers = buildHandlers(host);
  const server = await startRpcServer(config.rpcListen, handlers);

  wireEventSubscriptions(host, bridge);

  return {
    stop: async () => {
      await new Promise<void>((resolve) => server.tryShutdown(() => resolve()));
      await client.close();
    },
  };
}

export type { AdapterConfig, OpenClawHost, EventSigner };
