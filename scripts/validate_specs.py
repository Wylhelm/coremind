"""Validate CoreMind spec files.

Phase 0 spec validation:
1. `spec/worldevent.schema.json` must be a valid JSON Schema (Draft 2020-12).
2. Every example payload embedded in `spec/worldevent.md` must validate
   against that schema.

Run via: ``just spec-validate``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("❌ jsonschema is not installed. Run: just setup", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "spec" / "worldevent.schema.json"
PROSE_PATH = REPO_ROOT / "spec" / "worldevent.md"

# Example payloads in the prose spec are marked as fenced JSON blocks
# immediately following a heading that contains "Example".
EXAMPLE_BLOCK_RE = re.compile(
    r"###[^\n]*Example[^\n]*\n+```json\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _fail(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def _load_schema() -> dict:
    if not SCHEMA_PATH.exists():
        _fail(f"Schema not found: {SCHEMA_PATH}")
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"Schema is not valid JSON: {exc}")
        raise  # unreachable — keeps mypy happy


def _extract_examples() -> list[dict]:
    if not PROSE_PATH.exists():
        print(f"⚠️  Prose spec not found: {PROSE_PATH} — skipping example validation.")
        return []
    text = PROSE_PATH.read_text(encoding="utf-8")
    blocks = EXAMPLE_BLOCK_RE.findall(text)
    examples: list[dict] = []
    for i, raw in enumerate(blocks, 1):
        try:
            examples.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            _fail(f"Example #{i} in {PROSE_PATH.name} is not valid JSON: {exc}")
    return examples


def main() -> int:
    schema = _load_schema()

    # 1. Meta-schema validation
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        _fail(f"{SCHEMA_PATH.name} is not a valid JSON Schema: {exc}")
    print(f"✅ {SCHEMA_PATH.name} is a valid Draft 2020-12 JSON Schema.")

    # 2. Example validation
    examples = _extract_examples()
    if not examples:
        print(
            "⚠️  No example payloads found in prose spec. "
            "Add 'Example' blocks in spec/worldevent.md before Phase 1."
        )
        return 0

    validator = Draft202012Validator(schema)
    failures = 0
    for i, example in enumerate(examples, 1):
        errors = sorted(validator.iter_errors(example), key=lambda e: e.path)
        if errors:
            failures += 1
            print(f"❌ Example #{i} failed validation:")
            for err in errors:
                loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
                print(f"   - at {loc}: {err.message}")
        else:
            print(f"✅ Example #{i} validates.")

    if failures:
        _fail(f"{failures} example(s) failed validation.")

    print(f"\n🎯 All {len(examples)} example(s) validate against {SCHEMA_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
