/**
 * gRPC server that implements `OpenClawHalf` and bridges inbound CoreMind
 * actions to the OpenClaw gateway.
 *
 * The `handlers` object lets callers inject concrete implementations for
 * each RPC — the adapter wires these to real OpenClaw SDK calls in
 * {@link registerExtension}. Here we define only the gRPC plumbing so the
 * module is transport-agnostic and testable in isolation.
 */

import * as path from "node:path";
import * as grpc from "@grpc/grpc-js";
import * as protoLoader from "@grpc/proto-loader";

const PROTO_DIR = new URL("../../proto/", import.meta.url).pathname;
const SPEC_DIR = new URL("../../../../spec/", import.meta.url).pathname;

export interface OpenClawHandlers {
  notify: (req: unknown) => Promise<unknown>;
  requestApproval: (req: unknown) => Promise<unknown>;
  invokeSkill: (req: unknown) => Promise<unknown>;
  scheduleCron: (req: unknown) => Promise<unknown>;
  cancelCron: (req: unknown) => Promise<unknown>;
  listChannels: () => Promise<{ channels: string[] }>;
  listSkills: () => Promise<{ skills: string[] }>;
  mem0Query?: (req: unknown) => Promise<unknown>;
  mem0Store?: (req: unknown) => Promise<unknown>;
}

type Callback<T> = (err: grpc.ServiceError | null, response?: T) => void;

type UnaryCall<Req, Res> = (
  call: grpc.ServerUnaryCall<Req, Res>,
  callback: Callback<Res>,
) => void;

function asyncToUnary<Req, Res>(fn: (req: Req) => Promise<Res>): UnaryCall<Req, Res> {
  return (call, callback) => {
    fn(call.request)
      .then((response) => callback(null, response))
      .catch((err: unknown) => {
        const details = err instanceof Error ? err.message : String(err);
        callback({
          name: "OpenClawHalfError",
          code: grpc.status.INTERNAL,
          message: details,
          details,
          metadata: new grpc.Metadata(),
        } as grpc.ServiceError);
      });
  };
}

export async function startRpcServer(
  address: string,
  handlers: OpenClawHandlers,
): Promise<grpc.Server> {
  const packageDef = await protoLoader.load(path.join(PROTO_DIR, "adapter.proto"), {
    keepCase: true,
    longs: String,
    enums: String,
    defaults: true,
    oneofs: true,
    includeDirs: [PROTO_DIR, SPEC_DIR],
  });
  const grpcObj = grpc.loadPackageDefinition(packageDef) as unknown as {
    coremind: { openclaw_adapter: { v1: { OpenClawHalf: { service: grpc.ServiceDefinition } } } };
  };
  const service = grpcObj.coremind.openclaw_adapter.v1.OpenClawHalf.service;

  const server = new grpc.Server();
  server.addService(service, {
    Notify: asyncToUnary(handlers.notify),
    RequestApproval: asyncToUnary(handlers.requestApproval),
    InvokeSkill: asyncToUnary(handlers.invokeSkill),
    ScheduleCron: asyncToUnary(handlers.scheduleCron),
    CancelCron: asyncToUnary(handlers.cancelCron),
    ListChannels: asyncToUnary(() => handlers.listChannels()),
    ListSkills: asyncToUnary(() => handlers.listSkills()),
    Mem0Query: asyncToUnary(handlers.mem0Query ?? (async () => ({ records: [] }))),
    Mem0Store: asyncToUnary(
      handlers.mem0Store ??
        (async () => ({
          record_id: "",
          ok: false,
          error: "mem0 backend not enabled",
        })),
    ),
    HealthCheck: asyncToUnary(async () => ({
      state: "HEALTH_STATE_OK",
      message: "openclaw half nominal",
      as_of: { seconds: Math.floor(Date.now() / 1000), nanos: 0 },
      events_processed: 0,
      actions_dispatched: 0,
    })),
  });

  await new Promise<void>((resolve, reject) => {
    server.bindAsync(address, grpc.ServerCredentials.createInsecure(), (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
  return server;
}
