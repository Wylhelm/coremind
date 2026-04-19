# WorldEvent Specification

**Version:** 1.0.0
**Status:** Draft
**Audience:** Plugin authors, core contributors, coding agents

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Field Reference](#2-field-reference)
3. [Canonical JSON Serialization](#3-canonical-json-serialization)
4. [Payload Examples](#4-payload-examples)
5. [Version Compatibility](#5-version-compatibility)
6. [Design Decisions](#6-design-decisions)

---

## 1. Purpose and Scope

A `WorldEvent` is the atomic unit of information in CoreMind. Every observation about
the world — a room's temperature changing, an email arriving, a financial transaction
completing — enters the system as a `WorldEvent` produced by a plugin (L1) and consumed
by the World Model (L2).

### 1.1 Where It Flows

```text
Plugin (L1)
  │  emits WorldEvent
  ▼
EventBus (in-process)
  │  fan-out
  ▼
World Model ingest task (L2)  ←  sole writer to L2 event table
```

- **Produced by:** L1 plugins, only. No other layer may fabricate `WorldEvent` objects.
- **Consumed by:** L2 (World Model) as its sole input stream.
- **Transport:** The gRPC plugin protocol carries `WorldEvent` messages from plugin
  process to daemon (see `spec/plugin.proto`). Inside the daemon, the `EventBus`
  distributes them.
- **Immutability:** Events are permanent. Once written, they are never mutated or
  deleted. The World Model is a derived view over the immutable event stream.

### 1.2 What a WorldEvent Is Not

- **Not a command.** Commands flow from L5 (Intention) through L6 (Action).
- **Not a query.** Queries flow from L4/L5 to L2/L3.
- **Not a log line.** Structured logs are for human operators. WorldEvents are
  machine-readable facts for the cognitive layers.
- **Not a raw signal.** Plugins are responsible for converting raw sensor data into
  named attributes before emitting. L2 never sees raw bytes or vendor-specific formats.

---

## 2. Field Reference

### 2.1 Required Fields

| Field | Type | Constraints | Description |
| ----- | ---- | ----------- | ----------- |
| `id` | `string` | ULID format | Globally unique, time-sortable identifier. Enables deduplication and time-ordered storage without a separate timestamp index. |
| `timestamp` | `string` | ISO-8601, millisecond precision, UTC, `Z` suffix | The moment the observation was made by the sensor or data source, **not** the daemon ingest time. Plugins must not back-date events. |
| `source` | `string` | Reverse-domain notation, e.g. `plugin.homeassistant` | Stable plugin identifier. Must match the `id` declared in the plugin's `PluginManifest`. |
| `source_version` | `string` | Semver `MAJOR.MINOR.PATCH` | The plugin's own version at time of emission. Used for audit traceability and compatibility gating. |
| `signature` | `string` | `ed25519:<lowercase-hex>` | ed25519 signature of the canonical form of this event with the `signature` field itself excluded. See §3. |
| `entity` | `object` | See §2.2 | The subject of the observation. |
| `attribute` | `string` | Lowercase, dot-notation, e.g. `temperature`, `balance.available` | The property of the entity being reported. Must be stable across plugin versions within the same `MAJOR`. |
| `value` | `boolean \| number \| string \| object \| array` | JSON-serializable | The observed value. `null` is permitted to report that a previously known value is no longer observed. |
| `confidence` | `number` | `[0.0, 1.0]` | Probability that the observation is accurate. Deterministic sensors (on/off switches) should use `1.0`. Noisy sensors must use a value derived from their uncertainty model. |

### 2.2 The `entity` Object

```json
{
  "entity": {
    "type": "room",
    "id": "living_room"
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `type` | `string` | Entity class. Canonical values: `room`, `person`, `device`, `project`, `task`, `transaction`, `email`, `calendar_event`, `document`. New types must be proposed via the RFC process. |
| `id` | `string` | Stable identifier within the type namespace. The combination `(type, id)` uniquely identifies an entity across all plugins and all time. |

Entity `id` values are defined by the plugin but must be stable across plugin restarts and
upgrades. Use the upstream system's native identifier where possible — for example, the
Home Assistant `entity_id`, a GitHub issue number, or an IMAP `Message-ID`.

The combined key `entity.type + "/" + entity.id` is the canonical entity reference used
by L2 for graph node lookup.

### 2.3 Optional Fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `unit` | `string` | SI or conventional unit for numeric `value`, e.g. `°C`, `kWh`, `USD`, `lux`, `%`. Omit for dimensionless quantities and non-numeric values. |
| `delta` | `object` | Change since the immediately preceding event for this `(entity, attribute)` pair. See §2.4. |
| `context` | `object` | Supporting context for the reasoning layers. See §2.5. |

### 2.4 The `delta` Object

The `delta` object is optional but **strongly recommended** for quantitative attributes.
It enables L2 to detect anomalies without re-reading history and reduces the query load
on the time-series store.

```json
{
  "delta": {
    "absolute": -2.1,
    "relative_pct": -5.3,
    "previous_value": 39.7
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `absolute` | `number` | `current_value − previous_value`. Negative indicates a decrease. Only meaningful for numeric `value`. |
| `relative_pct` | `number` | `(absolute / abs(previous_value)) × 100`. Must be omitted when `previous_value` is `0` to avoid division by zero. |
| `previous_value` | `any` | The value from the immediately preceding event for this `(entity, attribute)`. May be any JSON type, matching the type of `value`. |

All three sub-fields are optional individually. A minimal `delta` may carry only
`previous_value` for non-numeric attributes (e.g. a state transition: `"previous_value": "off"`).

### 2.5 The `context` Object

```json
{
  "context": {
    "trend_window": "24h",
    "trend_direction": "rising",
    "related_entities": [
      { "type": "device", "id": "thermostat_main" }
    ],
    "tags": ["hvac", "energy"]
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `trend_window` | `string` | Duration string (e.g. `"1h"`, `"24h"`, `"7d"`) over which `trend_direction` was computed. |
| `trend_direction` | `string` | One of `"rising"`, `"falling"`, `"stable"`, `"volatile"`. Computed by the plugin over the `trend_window`. |
| `related_entities` | `array` | Other `{ type, id }` pairs causally or contextually related to this event. Used by L2 to maintain or reinforce relationship edges in the graph. |
| `tags` | `array` | Free-form string labels. Must be lowercase, hyphen-separated (no spaces), e.g. `"hvac"`, `"requires-review"`. Used for filtering and coarse categorisation. |

---

## 3. Canonical JSON Serialization

The `signature` field is computed over a deterministic serialization of the event object.
This section is normative — implementations must follow it exactly.

### 3.1 Signing Procedure

1. Take the full event object as a JSON-serializable Python dict (or equivalent).
2. **Remove** the `signature` field from the object. It must not be present during signing.
3. Serialize to canonical JSON per **RFC 8785** (JSON Canonicalization Scheme, JCS).
4. Encode the result as UTF-8 bytes.
5. Sign with the plugin's ed25519 private key.
6. Encode the 64-byte signature as lowercase hexadecimal (128 hex characters).
7. Set `signature` to the string `"ed25519:<hex>"`.

### 3.2 RFC 8785 Canonicalization Rules

RFC 8785 defines a canonical form for JSON. The rules relevant to `WorldEvent` are:

- Object keys are sorted by Unicode code point order (lexicographic on UTF-16 code units).
- No insignificant whitespace (no spaces, no newlines).
- Numbers serialized in their shortest representation; no trailing zeros; no leading zeros
  except for the integer part of numbers with absolute value less than 1.
- Unicode characters outside the Basic Multilingual Plane are encoded as surrogate pairs
  per the RFC; control characters use `\uXXXX` escapes.

Use a compliant JCS library. Do not hand-roll canonicalization.

### 3.3 Verification Procedure

1. Extract and preserve the `signature` value.
2. Remove the `signature` field from the received object.
3. Canonicalize the remaining object with RFC 8785.
4. Decode the hex string following `"ed25519:"` to 64 raw bytes.
5. Verify the signature against the UTF-8 canonical form using the plugin's registered
   public key (from `PluginManifest.public_key`).
6. **Reject** any event where verification fails. Do not pass it to L2. Emit a
   `meta-event` on the bus indicating the rejected event's `id` and `source`.

### 3.4 Key Management

- Each plugin generates its own ed25519 keypair at first startup.
- The public key is declared in `PluginManifest.public_key` (base64url-encoded,
  32 bytes raw).
- The daemon verifies each new plugin's manifest signature before accepting any events
  from that plugin.
- Key rotation requires submitting an updated manifest. The daemon enforces a one-hour
  cooldown before trusting the new key. Events signed with the old key remain valid
  for the duration of their retention period.
- Private keys are stored in `~/.coremind/secrets/`, chmod 600. They are never
  included in events, logs, or configuration exports.

---

## 4. Payload Examples

The three examples below span the minimum and maximum surface of the schema. The JSON
Schema at `spec/worldevent.schema.json` must validate all three without errors.

### 4.1 Minimum Payload Example

A bare-minimum event with no optional fields. Valid, but carries minimal context.
Appropriate for discrete state changes from high-confidence sensors.

```json
{
  "id": "01HX4KZDRF4N3J8VT7P2M9Q000",
  "timestamp": "2026-04-19T14:30:00.000Z",
  "source": "plugin.homeassistant",
  "source_version": "1.0.0",
  "signature": "ed25519:a3f8c2d1e9b74a5f6c3d8e2f1a9b4c7d0e3f8a1b6c9d2e5f8a3b6c1d4e7f0a2b5c8d1e4f7a0b3c6d9e2f5a8b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3e6f9a2b5c8d",
  "entity": {
    "type": "device",
    "id": "motion_sensor_hallway"
  },
  "attribute": "state",
  "value": "detected",
  "confidence": 1.0
}
```

### 4.2 Typical Payload Example

A quantitative sensor reading with delta and trend context. This is the most common shape
emitted by environmental monitoring plugins.

```json
{
  "id": "01HX4KZDRF4N3J8VT7P2M9Q001",
  "timestamp": "2026-04-19T14:30:00.123Z",
  "source": "plugin.homeassistant",
  "source_version": "1.2.3",
  "signature": "ed25519:b4e9d3c2f0a7b5e8c1d6e9f2a0b3c6d9e2f5a8b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3e6f9a2b5c8d1e4f7a0b3c6d9e2f5a8b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3",
  "entity": {
    "type": "room",
    "id": "living_room"
  },
  "attribute": "temperature",
  "value": 21.3,
  "unit": "°C",
  "confidence": 0.98,
  "delta": {
    "absolute": 0.4,
    "relative_pct": 1.9,
    "previous_value": 20.9
  },
  "context": {
    "trend_window": "1h",
    "trend_direction": "rising",
    "related_entities": [
      { "type": "device", "id": "thermostat_main" }
    ],
    "tags": ["hvac", "comfort"]
  }
}
```

### 4.3 Maximum Payload Example

A complex financial event with a structured object `value`, full delta, and multiple
related entities. Demonstrates that `value` may be an object and that `delta.previous_value`
mirrors the `value` type.

```json
{
  "id": "01HX4KZDRF4N3J8VT7P2M9Q002",
  "timestamp": "2026-04-19T14:30:00.456Z",
  "source": "plugin.budget",
  "source_version": "2.1.0",
  "signature": "ed25519:c5f0e4d3a1b8c6f9d2e7f0a3b6c9d0e3f6a9b2c5d8e1f4a7b0c3d6e9f2a5b8c1d4e7f0a3b6c9d2e5f8a1b4c7d0e3f6a9b2c5d8e1f4a7b0c3d6e9f2a5b8c1d4e7",
  "entity": {
    "type": "transaction",
    "id": "txn_20260419_143000_456"
  },
  "attribute": "balance.available",
  "value": {
    "amount": 4823.17,
    "currency": "CAD",
    "account": "chequing_primary"
  },
  "unit": "CAD",
  "confidence": 0.99,
  "delta": {
    "absolute": -127.50,
    "relative_pct": -2.57,
    "previous_value": {
      "amount": 4950.67,
      "currency": "CAD",
      "account": "chequing_primary"
    }
  },
  "context": {
    "trend_window": "7d",
    "trend_direction": "falling",
    "related_entities": [
      { "type": "person", "id": "user_self" },
      { "type": "task", "id": "budget_review_april_2026" }
    ],
    "tags": ["finance", "spending", "requires-review"]
  }
}
```

---

## 5. Version Compatibility

### 5.1 Schema Versioning

This document and `spec/worldevent.schema.json` are versioned together using semver.
The current schema version is **`1.0.0`**, declared in the `$id` of the JSON Schema file.

Schema versioning is independent of the CoreMind daemon version and of plugin
`source_version` values.

### 5.2 `source_version` Semantics

The `source_version` field carries the **plugin's** semver at emission time. It is not
the WorldEvent schema version.

Its purposes:

- **Audit traceability:** know exactly which plugin build produced a given event.
- **Compatibility gating:** the daemon may be configured to refuse events from plugins
  below a minimum `source_version`, enforcing upgrade policies.
- **Debugging:** attribute semantics sometimes shift between plugin versions; the version
  is necessary context for correct historical interpretation.

`source_version` must follow semver strictly (`MAJOR.MINOR.PATCH`). Pre-release suffixes
(e.g. `1.0.0-beta.1`) are allowed but the daemon will treat them as lower than the
corresponding release.

### 5.3 Breaking vs. Non-Breaking Changes to the Schema

**Non-breaking (minor or patch bump to `1.x.x`):**

- Adding new optional fields to `WorldEvent`, `entity`, `delta`, or `context`.
- Adding new canonical `entity.type` values.
- Adding new `trend_direction` values.
- Widening an accepted type (e.g. `string` → `string | number` for `value`).

**Breaking (major bump, migration required):**

- Removing or renaming any required field.
- Narrowing an accepted type.
- Changing the canonical JSON serialization algorithm (RFC 8785 is locked for `1.x`).
- Changing the signature scheme (ed25519 is locked for `1.x`).
- Changing the ULID requirement for `id`.

When a breaking change is required:

1. Increment the schema major version in `spec/worldevent.schema.json` and this document.
2. Write a migration guide at `docs/migrations/worldevent-vN-to-vM.md`.
3. Support reading the previous major version for one full release cycle before removing it.
4. Update `spec/worldevent.schema.json`, this document, and any code that references
   the old field names.

### 5.4 Forward Compatibility

Consumers must silently ignore unknown fields. A reader built against schema `1.0.0`
must accept events that carry additional fields introduced in `1.1.0` without error.

This rule applies to the daemon ingest path, L2 store adapters, and all tests that
deserialize `WorldEvent` objects.

### 5.5 Backward Compatibility

A plugin writing schema `1.1.0` events must still emit all fields that were required
in `1.0.0`. Optional fields that existed in `1.0.0` remain optional in `1.1.0`.

---

## 6. Design Decisions

### Why ULID for `id`?

ULIDs are time-sortable (first 48 bits encode millisecond timestamp), globally unique
without coordination, URL-safe, and 128-bit (collision probability negligible). Unlike
UUIDs v4, ULID-sorted storage gives time-ordered reads for free, which matters for the
L2 event table's primary access pattern.

### Why ed25519 for signatures?

ed25519 is fast (both sign and verify), has compact keys and signatures (32 and 64 bytes
respectively), and is immune to the timing side-channels that affect RSA and ECDSA when
implemented naively. The `cryptography` library's ed25519 implementation is well-audited.
Keys are also simple to manage: a plugin has exactly one keypair.

### Why separate `entity.type` and `entity.id`?

A flat `entity_id` string forces every consumer to parse the entity class out of the ID
(e.g. splitting on `:`). Separating the fields enables typed graph queries
(`"all rooms with temperature > 22°C"`) without string manipulation and prevents silent
ID collisions across entity classes.

### Why `attribute` as a free-form string rather than an enum?

Plugins observe heterogeneous properties that cannot be exhaustively enumerated at schema
design time. A dot-notation string with naming conventions (`noun.noun`, all lowercase)
gives enough structure for querying without requiring a schema change every time a plugin
reports a new property.

### Why `confidence` at the event level rather than per-field?

An observation is typically trustworthy or not as a whole — a sensor reading is either
reliable or suspect. Per-field confidence would triple the schema complexity for marginal
benefit. If a single sub-field of a complex `value` object has different confidence from
the rest, emit two separate events with distinct `attribute` paths.
