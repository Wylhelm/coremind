/**
 * Minimal RFC 8785 (JCS) canonicalization.
 *
 * Matches the output of Python's `jcs.canonicalize()` for the subset of
 * JSON values we actually sign in the adapter (strings, booleans, finite
 * numbers, arrays, nested objects). Not a full JCS implementation — does
 * not attempt to round-trip exotic floats (NaN/Infinity are rejected, as
 * per JCS). Keep this deliberately small: the cross-runtime signature
 * contract is only as trustworthy as this function.
 *
 * Rules implemented:
 *  - Object keys sorted lexicographically (UTF-16 code unit order, which
 *    matches Python's default string ordering for ASCII keys).
 *  - No insignificant whitespace.
 *  - Strings re-encoded via `JSON.stringify` (ECMA-404 compliant; JCS
 *    §3.2.2 requires this form).
 *  - Numbers serialised with JavaScript's default shortest form, matching
 *    ECMA-262 `ToString(Number)` which JCS §3.2.2.3 normatively requires.
 *
 * CAUTION: Floats that round-trip differently between Python and JS
 * (uncommon, but possible for some doubles) will produce divergent bytes.
 * WorldEvent currently only uses `confidence: float`; keep it to simple
 * values (0.0–1.0, short decimals) until we switch to a vetted JCS lib.
 */

export default function canonicalize(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) {
    throw new TypeError("JCS: undefined is not representable");
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("JCS: NaN/Infinity are not representable");
    }
    // `JSON.stringify` for finite numbers uses ECMA-262 ToString, matching
    // the JCS requirement.
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalize).join(",") + "]";
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== undefined,
    );
    entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return (
      "{" +
      entries.map(([k, v]) => JSON.stringify(k) + ":" + canonicalize(v)).join(",") +
      "}"
    );
  }
  throw new TypeError(`JCS: unsupported value type: ${typeof value}`);
}
