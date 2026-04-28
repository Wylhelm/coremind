/**
 * Runtime configuration for the OpenClaw-side adapter.
 *
 * Values come from the OpenClaw gateway's plugin configuration system; the
 * config_schema in manifest.json is authoritative for what's accepted.
 */

import * as os from "node:os";
import * as path from "node:path";

export interface AdapterConfig {
  /** gRPC address of the CoreMind-side CoreMindHalf service. */
  coremindAddress: string;
  /** gRPC address this extension listens on for OpenClawHalf RPCs. */
  rpcListen: string;
  /** Disk-backed queue for events emitted while CoreMind is unreachable. */
  bufferPath: string;
  /** Channels this adapter is permitted to surface/send on. `*` = all. */
  channelAllowlist: string[];
  /** Skills this adapter may expose to CoreMind. `*` = all. */
  skillAllowlist: string[];
  /** Plugin identifier CoreMind expects to see in the WorldEvent.source. */
  coremindPluginId: string;
  /** Semantic version stamped on emitted WorldEvents. */
  coremindPluginVersion: string;
}

/**
 * Return the private run directory for adapter sockets.
 *
 * Prefers `$XDG_RUNTIME_DIR/openclaw` (per-user, chmod 700 by systemd) and
 * falls back to `~/.openclaw/run` when XDG_RUNTIME_DIR is unset (e.g.
 * headless containers). Avoids world-writable /tmp paths where another
 * local user could bind-squat or observe that the adapter is up.
 */
function runtimeDir(): string {
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg && xdg.length > 0) return path.join(xdg, "openclaw");
  return path.join(os.homedir(), ".openclaw", "run");
}

function stateDir(): string {
  // Per-user state. Queue survives reboots; /tmp or XDG_RUNTIME_DIR do not.
  return path.join(os.homedir(), ".openclaw", "state", "coremind-adapter");
}

const _RT = runtimeDir();
const _STATE = stateDir();

export const DEFAULT_CONFIG: AdapterConfig = {
  coremindAddress: `unix://${path.join(_RT, "coremind-openclaw-half.sock")}`,
  rpcListen: `unix://${path.join(_RT, "openclaw-adapter.sock")}`,
  bufferPath: path.join(_STATE, "events.jsonl"),
  channelAllowlist: ["*"],
  skillAllowlist: ["*"],
  coremindPluginId: "coremind.plugin.openclaw_adapter",
  coremindPluginVersion: "0.1.0",
};

export { runtimeDir, stateDir };

/** Return `true` iff *channel* matches any entry in *allowlist* (glob). */
export function channelAllowed(channel: string, allowlist: string[]): boolean {
  return allowlist.some((pat) => globMatch(channel, pat));
}

/** Return `true` iff *skill* matches any entry in *allowlist* (glob). */
export function skillAllowed(skill: string, allowlist: string[]): boolean {
  return allowlist.some((pat) => globMatch(skill, pat));
}

/**
 * Minimal fnmatch-style glob: supports `*` matching any character sequence
 * and exact literal matches. No brace expansion, no character classes.
 */
function globMatch(text: string, pattern: string): boolean {
  if (pattern === "*") return true;
  if (!pattern.includes("*")) return text === pattern;
  const parts = pattern.split("*");
  let index = 0;
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i];
    if (part === "") continue;
    const found = text.indexOf(part, index);
    if (found === -1) return false;
    if (i === 0 && found !== 0) return false;
    index = found + part.length;
  }
  if (!pattern.endsWith("*") && index !== text.length) return false;
  return true;
}
