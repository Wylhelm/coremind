/**
 * Registration helper that plugs the adapter into the OpenClaw gateway.
 *
 * In a real deployment this module imports `@openclaw/plugin-sdk` and calls
 * its registration hooks. Those calls are kept behind a narrow interface so
 * the adapter core can be unit-tested without the SDK installed.
 */

import type { AdapterConfig } from "./config.js";
import type { EventBridge } from "./event_bridge.js";
import type { OpenClawHandlers } from "./rpc_server.js";

/**
 * Shape of the minimal OpenClaw SDK surface the adapter depends on.
 *
 * Implementations live outside this package (the real SDK or an in-memory
 * test fake). This keeps the adapter testable without pulling `@openclaw/plugin-sdk`.
 */
export interface OpenClawHost {
  onEvent(kind: string, handler: (event: unknown) => void | Promise<void>): void;
  sendMessage(channel: string, target: string, text: string): Promise<{ messageId: string }>;
  requestApproval(args: {
    channel: string;
    target: string;
    prompt: string;
    timeoutSeconds?: number;
  }): Promise<{ outcome: "approved" | "denied" | "timeout" | "cancelled"; feedback?: string }>;
  invokeSkill(name: string, params: Record<string, unknown>): Promise<Record<string, unknown>>;
  scheduleCron(args: {
    id: string;
    expression: string;
    skillName: string;
    parameters: Record<string, unknown>;
    description?: string;
  }): Promise<void>;
  cancelCron(id: string): Promise<void>;
  listChannels(): Promise<string[]>;
  listSkills(): Promise<string[]>;
}

/** Wire *host* to the adapter's event bridge and handler map. */
export function buildHandlers(host: OpenClawHost): OpenClawHandlers {
  return {
    notify: async (req: unknown) => {
      const { channel, target, text } = req as { channel: string; target: string; text: string };
      const res = await host.sendMessage(channel, target, text);
      return { delivered: true, message_id: res.messageId, error: "" };
    },
    requestApproval: async (req: unknown) => {
      const r = req as {
        approval_id: string;
        channel: string;
        target: string;
        prompt: string;
        timeout_seconds?: number;
      };
      const outcome = await host.requestApproval({
        channel: r.channel,
        target: r.target,
        prompt: r.prompt,
        timeoutSeconds: r.timeout_seconds,
      });
      return {
        outcome: `APPROVAL_OUTCOME_${outcome.outcome.toUpperCase()}`,
        approval_id: r.approval_id,
        feedback: outcome.feedback ?? "",
      };
    },
    invokeSkill: async (req: unknown) => {
      const r = req as {
        skill_name: string;
        parameters?: Record<string, unknown>;
        call_id?: string;
      };
      const output = await host.invokeSkill(r.skill_name, r.parameters ?? {});
      return { call_id: r.call_id ?? "", ok: true, output, error: "" };
    },
    scheduleCron: async (req: unknown) => {
      const r = req as {
        cron_id: string;
        expression: string;
        skill_name: string;
        parameters?: Record<string, unknown>;
        description?: string;
      };
      await host.scheduleCron({
        id: r.cron_id,
        expression: r.expression,
        skillName: r.skill_name,
        parameters: r.parameters ?? {},
        description: r.description,
      });
      return { scheduled: true, cron_id: r.cron_id, error: "" };
    },
    cancelCron: async (req: unknown) => {
      const r = req as { cron_id: string };
      await host.cancelCron(r.cron_id);
      return { cancelled: true, error: "" };
    },
    listChannels: async () => ({ channels: await host.listChannels() }),
    listSkills: async () => ({ skills: await host.listSkills() }),
  };
}

/** Subscribe to OpenClaw events and forward them through *bridge*. */
export function wireEventSubscriptions(host: OpenClawHost, bridge: EventBridge): void {
  for (const kind of ["message.received", "skill.invoked", "cron.executed", "approval.responded"]) {
    host.onEvent(kind, async (raw) => {
      try {
        await bridge.handle(raw as never);
      } catch (err) {
        console.error(`[openclaw-adapter] event ${kind} failed:`, err);
      }
    });
  }
}

export type { AdapterConfig };
