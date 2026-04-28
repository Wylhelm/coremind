# OpenClaw Adapter for CoreMind

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** CoreMind operators who also run an OpenClaw gateway

---

A bidirectional adapter that lets CoreMind observe OpenClaw activity
(messages, skill invocations, cron runs, approvals) and dispatch actions
(notifications, approval prompts, skill invocations, cron entries) through
OpenClaw's existing channel and skill surface.

## Layout

| Path | Role |
|---|---|
| `proto/adapter.proto` | gRPC contract (`CoreMindHalf` + `OpenClawHalf`) |
| `coremind_side/` | Python plugin running inside CoreMind |
| `openclaw_side/` | TypeScript extension running inside OpenClaw |
| `docs/SETUP.md` | End-user installation guide |

## Data flow

```
OpenClaw gateway                CoreMind daemon
  │                                │
  │  message.received              │
  ├── translate → sign ────────────▶ CoreMindHalf.IngestEvent
  │                                │   (verify signature)
  │                                └── EmitEvent → EventBus → L2/L3
  │
  │                               CoreMind action
  │                                │
  │  OpenClawHalf.Notify  ◀────────┤  action_dispatcher
  ├── OpenClaw channel send        │   (JSON Schema + scope)
  ▼
```

See `docs/SETUP.md` for installation and `proto/adapter.proto` for the full
RPC contract.
