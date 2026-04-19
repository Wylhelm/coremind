# Audit Log Specification

**Version:** 1.0.0
**Status:** Draft
**Audience:** Core contributors, security reviewers, coding agents implementing L6

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [File Location and Retention](#2-file-location-and-retention)
3. [JSONL Format](#3-jsonl-format)
4. [Entry Schema](#4-entry-schema)
5. [Canonical Serialization](#5-canonical-serialization)
6. [Signing Procedure](#6-signing-procedure)
7. [Hash Chain](#7-hash-chain)
8. [Verification Procedure](#8-verification-procedure)
9. [Recovery Procedure](#9-recovery-procedure)
10. [Security Considerations](#10-security-considerations)

---

## 1. Purpose and Scope

The audit log is the **tamper-evident record of every side-effecting action taken by
CoreMind**. It answers the question: *"What did the system do, on whose behalf, and
was it authorised?"*

Every entry in the log corresponds to a single action executed by L6 (Action). The log
is append-only. Entries are never modified or deleted. It is the system's credibility:
if it is lost, there is no way to reconstruct what the daemon did on the user's behalf.

### 1.1 What is Logged

- Every action executed by L6, regardless of confidence category.
- Every action **rejected** because confidence was below threshold (category `uncertain`).
- Every action held for human approval, and the outcome of that approval.
- Every action that failed at execution time.

### 1.2 What is Not Logged

- `WorldEvent` observations from L1 (those are stored in SurrealDB, L2).
- Reasoning traces from L4 (those are transient, by design).
- Internal events on the `EventBus` (those are the input stream, not outcomes).

---

## 2. File Location and Retention

- **Path:** `~/.coremind/audit.log`
- **Permissions:** `600` (owner read/write only). The daemon must refuse to start if
  the file's permissions are wider than `600`.
- **Format:** UTF-8, LF line endings.
- **Rotation:** The file is never rotated automatically. Archiving is a manual
  operator decision. When archiving, the archive must be verified (§8) before the
  active file is replaced. The replacement genesis entry must chain off the final
  entry of the archived segment.
- **Backup:** Operators are strongly encouraged to replicate this file off-host.
  CoreMind does not do this automatically (user data never leaves the host by default).

---

## 3. JSONL Format

The log is a sequence of JSON lines (JSONL):

- One complete JSON object per line.
- Lines are separated by a single `\n` (LF).
- No trailing comma, no enclosing array, no blank lines between entries.
- The file may contain a trailing newline after the last entry.

Example (two entries, abbreviated):

```json
{"seq":1,"action_id":"act_01HX...","timestamp":"2026-01-15T10:00:00.000Z","signature":"ed25519:aabbcc..."}
{"seq":2,"action_id":"act_01HY...","timestamp":"2026-01-15T10:00:05.000Z","signature":"ed25519:ddeeff..."}
```

---

## 4. Entry Schema

Every journal entry is a flat JSON object with the following fields.

### 4.1 Required Fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `seq` | `integer` | Monotonically increasing sequence number, starting at `1`. Must be exactly `prev_seq + 1`. Gaps indicate a missing entry. |
| `action_id` | `string` | ULID. Globally unique identifier for this action. |
| `intent_id` | `string` | ULID. The L5 intent that triggered this action. Enables tracing from outcome back to intent. |
| `timestamp` | `string` | ISO-8601, millisecond precision, UTC, `Z` suffix. The moment L6 committed this entry. |
| `category` | `string` | One of `safe`, `optimization`, `uncertain`, `forced_approval`. Reflects the confidence gate that applied. |
| `status` | `string` | One of `executed`, `rejected`, `pending_approval`, `approved`, `denied`, `failed`. Final state of the action. |
| `operation` | `string` | Dot-notation identifier for the action type, e.g. `homeassistant.set_light_state`, `gmail.send_email`. |
| `parameters` | `object` | Input parameters passed to the effector. Must not contain secrets or raw credential material. |
| `result` | `object \| null` | Outcome returned by the effector. `null` for `rejected` or `pending_approval` entries. |
| `reversible_by` | `string \| null` | If reversible: the `action_id` that can undo this action, or a human-readable procedure string. `null` if irreversible. |
| `signature` | `string` | `ed25519:<lowercase-hex>`. See §6. |

### 4.2 Optional Fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `approval` | `object \| null` | Present when `status` is `approved` or `denied`. |
| `approval.approved_by` | `string` | The channel or user that approved/denied, e.g. `telegram:@user`. |
| `approval.decided_at` | `string` | ISO-8601 timestamp of the decision. |
| `approval.decision` | `string` | `approved` or `denied`. |
| `error` | `string \| null` | Human-readable error message when `status` is `failed`. Stack traces must not appear here. |
| `plugin_id` | `string` | The plugin that performed the action, if the action was delegated to a plugin effector. |

### 4.3 Minimal Entry Example

```json
{
  "seq": 1,
  "action_id": "act_01HX9MTNC4VGSJ1K8YN73X25RZ",
  "intent_id": "int_01HX9MTNB7YR3P0J5G2H8Q64WK",
  "timestamp": "2026-01-15T10:00:00.000Z",
  "category": "safe",
  "status": "executed",
  "operation": "homeassistant.set_light_state",
  "parameters": {
    "entity_id": "light.living_room",
    "state": "off"
  },
  "result": {
    "success": true,
    "new_state": "off"
  },
  "reversible_by": "homeassistant.set_light_state",
  "signature": "ed25519:a3f8..."
}
```

### 4.4 Forced-Approval Entry Example

```json
{
  "seq": 7,
  "action_id": "act_01HXB3QQFP9EMKJ5VR7N0Y81DT",
  "intent_id": "int_01HXB3QQE1RG4H7S8T3A6P59CX",
  "timestamp": "2026-01-15T11:42:00.000Z",
  "category": "forced_approval",
  "status": "approved",
  "operation": "gmail.send_email",
  "parameters": {
    "to": "colleague@example.com",
    "subject": "Meeting notes",
    "body_hash": "sha256:c3ab8ff13720e8ad9047dd39466b3c8974e592c2fa383d4a3960714caef0c4f2"
  },
  "result": {
    "message_id": "msg_0193a..."
  },
  "reversible_by": null,
  "approval": {
    "approved_by": "telegram:@alice",
    "decided_at": "2026-01-15T11:42:15.000Z",
    "decision": "approved"
  },
  "signature": "ed25519:b7c1..."
}
```

Note: email bodies are **never** logged verbatim. Log a content hash to enable
verification without re-exposing potentially sensitive text.

---

## 5. Canonical Serialization

The canonical form of an entry is used as the input to the signing operation. This
ensures the signature is deterministic regardless of the JSON serializer used.

### 5.1 Rules

1. **Remove the `signature` field** before serializing. The signature field is
   excluded from its own signing input.
2. **Apply RFC 8785 (JSON Canonicalization Scheme, JCS)** to the remaining object.
   JCS rules:
   - Object keys are sorted lexicographically by Unicode code point.
   - No insignificant whitespace.
   - Numbers are serialized in their shortest IEEE 754 representation.
   - Strings use `\uXXXX` escapes only for control characters, not for printable
     Unicode.
3. Encode the result as **UTF-8 bytes**.

Use the `coremind.crypto.canonical_json` module (Phase 1 deliverable). Never call
`json.dumps` directly on signing inputs.

### 5.2 Example

Given an entry (signature field omitted for clarity):

```json
{
  "seq": 1,
  "action_id": "act_01HX9MTNC4VGSJ1K8YN73X25RZ",
  "intent_id": "int_01HX9MTNB7YR3P0J5G2H8Q64WK",
  "timestamp": "2026-01-15T10:00:00.000Z",
  "category": "safe",
  "status": "executed",
  "operation": "homeassistant.set_light_state",
  "parameters": {"entity_id": "light.living_room", "state": "off"},
  "result": {"new_state": "off", "success": true},
  "reversible_by": "homeassistant.set_light_state"
}
```

After JCS, keys are sorted and whitespace removed:

```json
{"action_id":"act_01HX9MTNC4VGSJ1K8YN73X25RZ","category":"safe","intent_id":"int_01HX9MTNB7YR3P0J5G2H8Q64WK","operation":"homeassistant.set_light_state","parameters":{"entity_id":"light.living_room","state":"off"},"result":{"new_state":"off","success":true},"reversible_by":"homeassistant.set_light_state","seq":1,"status":"executed","timestamp":"2026-01-15T10:00:00.000Z"}
```

This UTF-8 byte string is then prepended to the chain material (§7) before signing.

---

## 6. Signing Procedure

Each entry is signed with the daemon's **ed25519 private key**, stored at
`~/.coremind/keys/daemon.key` (chmod 600).

### 6.1 Signing Input

```text
signing_input = canonical_bytes(entry) || prev_signature_bytes
```

Where:

- `canonical_bytes(entry)` — UTF-8 encoding of the JCS canonical form of the entry
  with the `signature` field excluded (§5).
- `prev_signature_bytes` — the raw 64-byte ed25519 signature of the previous entry,
  decoded from its hex string. For the **genesis entry** (`seq = 1`), this is 64
  zero bytes: `b'\x00' * 64`.
- `||` — byte concatenation (no separator).

### 6.2 Signature Format

```text
ed25519:<lowercase-hex-encoded-64-bytes>
```

The 64-byte signature is encoded as 128 lowercase hexadecimal characters and prefixed
with `ed25519:`.

### 6.3 Key Management

- The keypair is generated once at first daemon start and never rotated automatically.
- The public key is stored at `~/.coremind/keys/daemon.pub`.
- Key rotation requires archiving the current log (§9.2) and starting a new genesis.
- The daemon must refuse to write to the log if the private key file is missing or has
  permissions wider than `600`.

---

## 7. Hash Chain

The chain is formed by the signing procedure: each entry's signature is computed over
input that includes the previous entry's raw signature bytes. This means:

- Modifying any entry invalidates its own signature **and** every subsequent entry's
  signature (because the chain input shifts).
- Inserting or deleting entries breaks the `seq` sequence and shifts all subsequent
  signatures.
- Reordering entries is detectable via the broken `seq` sequence.

### 7.1 Chain Anchoring

The genesis entry (`seq = 1`) uses 64 zero bytes as the previous signature sentinel.
This sentinel is fixed by this specification; it must never be changed.

### 7.2 Sub-chain after Archival

When a log segment is archived and a new file is started (§9.2), the new genesis entry
must include:

- `seq` continuing from the last archived entry (e.g., if the archive ends at `seq =
  500`, the new file starts at `seq = 501`).
- `prev_signature_bytes` set to the last archived entry's signature bytes (not the
  zero sentinel).

The archived file and the active file together form a single verifiable chain.

---

## 8. Verification Procedure

The `coremind audit verify` command (Phase 1) executes this procedure.

### 8.1 Algorithm

```python
prev_sig_bytes = b'\x00' * 64   # genesis sentinel
expected_seq = 1

for each line in audit.log:
    entry = parse_json(line)

    # 1. Sequence check
    assert entry["seq"] == expected_seq, f"Gap at seq {expected_seq}"

    # 2. Parse the declared signature
    declared_sig_hex = entry["signature"].removeprefix("ed25519:")
    declared_sig_bytes = bytes.fromhex(declared_sig_hex)

    # 3. Reconstruct signing input
    entry_without_sig = {k: v for k, v in entry.items() if k != "signature"}
    canonical = jcs_encode(entry_without_sig)   # UTF-8 bytes
    signing_input = canonical + prev_sig_bytes

    # 4. Verify the signature
    public_key.verify(signing_input, declared_sig_bytes)  # raises on failure

    prev_sig_bytes = declared_sig_bytes
    expected_seq += 1
```

### 8.2 Output

On success, the command prints:

```text
audit.log: OK (N entries, chain intact)
```

On failure, it prints the first failure point and stops. It never attempts to repair
or skip the broken entry.

### 8.3 Partial Verification

To verify only a range of entries (e.g., after archival), pass the final signature of
the preceding segment as the initial `prev_sig_bytes`. The `coremind audit verify`
command accepts `--from-sig <hex>` for this purpose.

---

## 9. Recovery Procedure

### 9.1 Broken Chain Detection

A broken chain is detected when:

- An entry's signature fails verification.
- A `seq` gap is found.
- An entry cannot be parsed as valid JSON.

### 9.2 Mandatory Response

**CoreMind must never auto-repair a broken chain.** Any attempt to silently fix or
skip broken entries destroys the tamper-evidence property the chain exists to provide.

When a broken chain is detected, the daemon must:

1. **Stop writing to the log immediately.** No new entries may be appended.
2. **Quarantine the log file.** Move `~/.coremind/audit.log` to
   `~/.coremind/audit.log.quarantine.<timestamp>` (read-only, chmod 400). Do not
   delete or truncate it.
3. **Emit a `meta-event`** on the `EventBus` with `attribute = "audit.chain_broken"`
   and `confidence = 1.0`. This surfaces the failure to the operator via L7.
4. **Halt L6 (Action).** No further autonomous actions may be taken until the
   operator has acknowledged and resolved the situation. Pending approvals are
   cancelled.
5. **Present a diagnostic** to the operator: the seq number where the chain broke,
   the expected signature, and the found signature.

### 9.3 Operator Resolution

After investigating, the operator may:

- **Accept the quarantined log as-is** and start a new log file. The new file begins
  at `seq = 1` with the genesis sentinel. The audit gap is the operator's
  acknowledged responsibility.
- **Replace the daemon key** (e.g., if the key was compromised) and start a new log.
  The old quarantined file retains its original signatures, which can still be verified
  with the old public key.

Under no circumstances should the operator delete the quarantined file. It is evidence.

### 9.4 Corruption vs. Tampering

Broken chains have two non-exclusive causes:

- **Corruption** — hardware failure, incomplete write, file system error.
- **Tampering** — an attacker modified an entry to hide a malicious action.

The recovery procedure is identical for both. Distinguishing them is a forensic task
for the operator, outside the scope of this specification.

---

## 10. Security Considerations

### 10.1 Secrets in the Log

The `parameters` and `result` fields must never contain:

- API keys, tokens, passwords, or other credentials.
- Raw message bodies that may contain sensitive user content.
- Personal information beyond what is strictly necessary to identify the action.

For content that must be referenced but not exposed, log a `sha256:<hex>` content
hash instead of the value (see §4.4 for an example).

### 10.2 Physical Security

ed25519 signatures prove that the entry was written by the holder of the daemon's
private key. They do not prevent an attacker with full file-system access from
deleting the entire log file. The log's tamper-evidence is effective against
**post-hoc modification** of existing entries; it does not protect against deletion
of the file itself. Off-host backup mitigates this.

### 10.3 Key Confidentiality

If the daemon private key is compromised, an attacker can forge valid-looking entries
and extend the chain. Key compromise is detectable only if a trusted external backup
of the public key hash exists. Operators are encouraged to record the SHA-256 of
`daemon.pub` in a location outside the host.

### 10.4 Timing

The `timestamp` field reflects when L6 committed the entry, which may differ slightly
from when the action was executed (e.g., network latency, effector round-trip). The
timestamp must never be back-dated. Monotonicity of timestamps within the chain is
enforced by verification: a timestamp that precedes the previous entry's timestamp
must be flagged as a warning (not a chain-break error, since clock skew is possible).

---

## Related Documents

- [WorldEvent Specification](worldevent.md) — the observation format feeding L2
- [plugin.proto](plugin.proto) — gRPC contract carrying `WorldEvent` from plugins
- [Architecture §3.6](../docs/ARCHITECTURE.md) — L6 Action layer design
- [Architecture §7.3](../docs/ARCHITECTURE.md) — storage context for the audit log
