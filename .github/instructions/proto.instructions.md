---
name: "Protobuf Standards"
description: "Conventions for .proto files (plugin API and adapters)"
applyTo: "**/*.proto"
---

# Protobuf Conventions

## File header

Every `.proto` file starts with:

```protobuf
syntax = "proto3";
package coremind.<area>.v1;

// Short description of what this file defines.
// Versioned: bump the package suffix (v1 → v2) for breaking changes.
```

## Naming

- Messages: `PascalCase` (e.g. `WorldEvent`, `PluginManifest`)
- Fields: `snake_case` (e.g. `entity_type`, `created_at`)
- Enums: `PascalCase`, values `UPPER_SNAKE` (e.g. `Severity.SEVERITY_HIGH`)
- Services: `PascalCase` (e.g. `CoreMindPlugin`)
- RPCs: `PascalCase` verb-first (e.g. `Identify`, `IngestEvent`)

## Field numbering

- 1–15: reserved for the most frequently used fields (single-byte tag)
- 16–2047: less hot fields
- Never re-use a deprecated field number. Mark `reserved <n>` with a comment explaining the deprecation.

## Semantic rules

- Use `google.protobuf.Timestamp` for timestamps (not `string`, not `int64`)
- Use `bytes` for binary data (signatures, keys)
- `string` fields that are IDs should have a naming suffix: `_id`, `_ref`, `_uri`
- Nullable fields: wrap in `optional` (proto3 explicit-optional) when absence is semantically different from default

## Versioning

- Add fields freely — new fields are backwards compatible.
- Never change a field's type or number.
- Never rename a field — add a new one and deprecate the old.
- Deprecated fields: keep them in the file with `[deprecated = true]` for at least one minor version.

## Comments

Every message and field has a line comment. Every RPC has a block comment explaining its contract, side effects, and error modes.

```protobuf
// WorldEvent is the atomic observation unit flowing from plugins
// into the CoreMind World Model. Every event is cryptographically
// signed by the emitting plugin.
message WorldEvent {
  // ULID, sortable by generation time.
  string id = 1;

  // When the observation was made, not when it was received.
  google.protobuf.Timestamp timestamp = 2;

  // ... etc
}
```

## After changing a .proto

- Regenerate stubs: `just proto-gen`
- Commit generated files alongside the `.proto` change in the same commit.
- CI verifies that generated files match the source. Do not push without regenerating.
