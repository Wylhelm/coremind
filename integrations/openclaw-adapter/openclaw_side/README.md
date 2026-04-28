# @coremind/openclaw-adapter — OpenClaw extension

TypeScript/Node.js extension that plugs into the OpenClaw gateway and bridges
its activity into CoreMind via gRPC. See `../docs/SETUP.md` for installation.

## Build

```bash
npm install
npm run build
```

## Design

This package is one half of a two-piece adapter. Both halves are versioned
together under `coremind.openclaw_adapter.v1`. See `../proto/adapter.proto`
for the contract.

- Inbound: OpenClaw events → `EventBridge` → `CoreMindClient.ingest()`
- Outbound: CoreMind actions → `OpenClawHalf` gRPC server → `OpenClawHost`

## Dependency injection

`src/openclaw_extension.ts` consumes a narrow `OpenClawHost` interface. Real
integrations implement it via `@openclaw/plugin-sdk`; tests substitute an
in-memory fake.
