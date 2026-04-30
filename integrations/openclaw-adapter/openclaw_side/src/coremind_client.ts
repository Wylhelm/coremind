/**
 * gRPC client to the CoreMind-side `CoreMindHalf` service.
 *
 * Handles:
 *  - Channel creation with unix:// and host:port forms.
 *  - Reconnection with exponential backoff.
 *  - A bounded disk-backed queue (JSONL) for events emitted while CoreMind is
 *    unreachable. The queue is drained on reconnect.
 *
 * NOTE: This file loads its gRPC types dynamically via @grpc/proto-loader so
 * it can function before the generated stubs are produced. In production the
 * recommended path is to run `npm run proto:gen` and import typed clients.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as grpc from "@grpc/grpc-js";
import * as protoLoader from "@grpc/proto-loader";
import type { AdapterConfig } from "./config.js";

const PROTO_DIR = new URL("../../proto/", import.meta.url).pathname;
const SPEC_DIR = new URL("../../../../spec/", import.meta.url).pathname;

interface WorldEventLike {
  id: string;
  timestamp: string; // RFC3339
  source: string;
  source_version: string;
  signature: Buffer;
  entity: { type: string; entity_id: string };
  attribute: string;
  value: unknown;
  confidence: number;
}

const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const DEFAULT_BUFFER_LIMIT = 10_000;

export class CoreMindClient {
  private channel: grpc.Client | null = null;
  constructor(private readonly config: AdapterConfig) {}

  /** Load the adapter.proto service package and connect to CoreMindHalf. */
  async connect(): Promise<void> {
    const packageDef = await protoLoader.load(
      path.join(PROTO_DIR, "adapter.proto"),
      {
        keepCase: true,
        longs: String,
        enums: String,
        defaults: true,
        oneofs: true,
        includeDirs: [PROTO_DIR, SPEC_DIR],
      },
    );
    const grpcObj = grpc.loadPackageDefinition(packageDef) as unknown as {
      coremind: {
        openclaw_adapter: {
          v1: {
            CoreMindHalf: new (
              addr: string,
              creds: grpc.ChannelCredentials,
            ) => grpc.Client;
          };
        };
      };
    };
    const Ctor = grpcObj.coremind.openclaw_adapter.v1.CoreMindHalf;
    this.channel = new Ctor(this.config.coremindAddress, grpc.credentials.createInsecure());
  }

  async close(): Promise<void> {
    this.channel?.close();
    this.channel = null;
  }

  /**
   * Submit a signed WorldEvent to CoreMind. Buffers on transport failure.
   *
   * The *event* argument must already contain a valid ed25519 signature
   * produced by the adapter plugin's private key, or the CoreMind side will
   * reject it with UNAUTHENTICATED.
   */
  async ingest(event: WorldEventLike): Promise<void> {
    if (this.channel === null) throw new Error("client not connected");
    const channel = this.channel;
    await new Promise<void>((resolve, reject) => {
      (channel as unknown as {
        IngestEvent: (arg: WorldEventLike, cb: (err: grpc.ServiceError | null) => void) => void;
      }).IngestEvent(event, (err) => {
        if (err) {
          this.buffer(event);
          reject(err);
          return;
        }
        resolve();
      });
    });
  }

  /** Append *event* to the on-disk buffer, enforcing the size bound. */
  private buffer(event: WorldEventLike): void {
    const dir = path.dirname(this.config.bufferPath);
    fs.mkdirSync(dir, { recursive: true });
    const line = JSON.stringify({
      ...event,
      signature: Buffer.isBuffer(event.signature)
        ? event.signature.toString("base64")
        : event.signature,
    });
    fs.appendFileSync(this.config.bufferPath, line + "\n", "utf-8");
    this.enforceBufferLimit();
  }

  private enforceBufferLimit(): void {
    if (!fs.existsSync(this.config.bufferPath)) return;
    const lines = fs
      .readFileSync(this.config.bufferPath, "utf-8")
      .split("\n")
      .filter((l) => l.length > 0);
    if (lines.length <= DEFAULT_BUFFER_LIMIT) return;
    const kept = lines.slice(lines.length - DEFAULT_BUFFER_LIMIT);
    fs.writeFileSync(this.config.bufferPath, kept.join("\n") + "\n");
  }

  /**
   * Attempt to connect with exponential backoff. Resolves when the first
   * connection is established (caller should then drain the buffer).
   */
  async connectWithBackoff(): Promise<void> {
    let backoff = INITIAL_BACKOFF_MS;
    while (true) {
      try {
        await this.connect();
        return;
      } catch (err) {
        console.error("[coremind-client] connect failed:", err);
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    }
  }
}
