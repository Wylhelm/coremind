/**
 * Reference implementation of {@link EventSigner} for the OpenClaw-side
 * extension.
 *
 * The canonical encoding MUST match the Python side's
 * `coremind_plugin_openclaw.translators.sign_event()`. See
 * `translators.py` module docstring for the full contract and
 * `tests/integrations/openclaw_adapter/golden_signature.json` for the
 * frozen cross-runtime vector.
 *
 * Rules (replicated briefly here):
 *  - Field names are proto snake_case (keepCase on the TS side,
 *    preserving_proto_field_name on the Py side).
 *  - `google.protobuf.Timestamp` → RFC 3339 string (e.g. "2026-04-19T20:14:02Z").
 *  - `bytes` (only `signature`) → stripped BEFORE canonicalisation.
 *  - int64/uint64 → JSON string (not relevant for WorldEvent today, but
 *    match the Python `MessageToDict` default if added later).
 *  - JCS (RFC 8785) sorts keys lexicographically and trims whitespace.
 *
 * This implementation uses Node's built-in `node:crypto` — no native deps.
 */

import * as crypto from "node:crypto";
import canonicalize from "./jcs.js";
import type { EventSigner } from "./event_bridge.js";
import type { UnsignedWorldEvent } from "./translators/oc_event_to_worldevent.js";

export interface SignedWorldEvent extends UnsignedWorldEvent {
  /** Base64 ed25519 signature over the JCS-canonical, signature-less object. */
  signature: Buffer;
}

/**
 * Build a signer that signs with *privateKey* (a Node ed25519 KeyObject).
 * See {@link loadPrivateKeyFromSeed} for creating one from a 32-byte seed.
 */
export function createEventSigner(privateKey: crypto.KeyObject): EventSigner {
  return {
    async signAndPack(translated: Record<string, unknown>): Promise<SignedWorldEvent> {
      // Defensive copy; never sign with a stray `signature` field.
      const unsigned = { ...translated } as Record<string, unknown>;
      delete unsigned.signature;
      const canonical = canonicalize(unsigned);
      const sig = crypto.sign(null, Buffer.from(canonical, "utf-8"), privateKey);
      return { ...(unsigned as unknown as UnsignedWorldEvent), signature: sig };
    },
  };
}

/**
 * Construct an ed25519 private key from a raw 32-byte seed.
 *
 * OpenClaw-side deployments typically load the seed from OpenClaw's secrets
 * store (keyed by the plugin id the CoreMind side registered with). See
 * `docs/SETUP.md` §3.5 for the provisioning scheme.
 */
export function loadPrivateKeyFromSeed(seed: Buffer): crypto.KeyObject {
  if (seed.length !== 32) {
    throw new Error(`ed25519 seed must be 32 bytes, got ${seed.length}`);
  }
  // RFC 8410 PKCS#8 DER prefix for ed25519 private keys, followed by the
  // 32-byte seed. Avoids depending on third-party PKCS#8 builders.
  const prefix = Buffer.from("302e020100300506032b657004220420", "hex");
  const der = Buffer.concat([prefix, seed]);
  return crypto.createPrivateKey({ key: der, format: "der", type: "pkcs8" });
}
