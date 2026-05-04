"""Procedural memory: versioned rule store with JSONL hash-chaining (L3, procedural layer).

Stores, matches, and maintains conditional rules produced by humans or the
reflection layer.  Each mutation is appended as a hash-chained JSONL entry,
providing a tamper-evident history of all rule changes.

The JSONL journal format mirrors the audit log specification: every entry
contains a sequence number, a timestamp, a pointer to the previous entry's
hash (``prev_hash``), the operation, a JSON payload, and the SHA-256 hash of
the canonical serialisation of the entry content (excluding ``entry_hash``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import operator as _op_module
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coremind.crypto.signatures import canonical_json
from coremind.errors import ProceduralMemoryError
from coremind.world.model import JsonValue

log = structlog.get_logger(__name__)

# Genesis sentinel — the ``prev_hash`` of the first (seq=1) entry.
_GENESIS_HASH: str = "0" * 64

# Numeric comparison operators keyed by DSL op string.
_NUMERIC_OPS: dict[str, Callable[[float, float], bool]] = {
    "gt": _op_module.gt,
    "gte": _op_module.ge,
    "lt": _op_module.lt,
    "lte": _op_module.le,
}

type Clock = Callable[[], datetime]
type _Op = Literal["add", "reinforce", "deprecate"]


def _utc_now() -> datetime:
    """Return the current UTC time.

    Injected as the default clock so tests can substitute a deterministic one.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Public domain model
# ---------------------------------------------------------------------------


class Rule(BaseModel):
    """A conditional rule stored in procedural memory.

    Rules encode "if this context, propose this action" knowledge.  They are
    produced by humans (``source="human"``) or the reflection layer
    (``source="reflection"``) and are reinforced or deprecated based on
    observed outcomes.

    The ``trigger`` dict follows the trigger DSL evaluated by
    :func:`_evaluate_trigger`.  The ``action`` dict is an opaque proposal
    consumed by L5/L6 and is not interpreted here.

    ``confidence`` is the creator's a-priori reliability estimate and is not
    modified by reinforcement.  ``success_rate`` is the empirically-observed
    running mean of successful outcomes, updated by :meth:`ProceduralMemory.reinforce`.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    created_at: datetime
    description: str = Field(min_length=1)
    trigger: dict[str, JsonValue]
    action: dict[str, JsonValue]
    confidence: float = Field(ge=0.0, le=1.0)
    applied_count: int = Field(ge=0, default=0)
    success_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    source: Literal["human", "reflection"]


# ---------------------------------------------------------------------------
# Internal journal models
# ---------------------------------------------------------------------------


class _EntryContent(BaseModel):
    """Hashable content of a journal entry — every field except ``entry_hash``.

    Serialised via ``model_dump(mode="json")`` → :func:`canonical_json` to
    produce the bytes that are SHA-256 hashed into ``entry_hash``.
    """

    seq: int
    timestamp: datetime
    prev_hash: str
    op: _Op
    payload: dict[str, JsonValue]


class _JournalEntry(_EntryContent):
    """A single immutable line in the procedural memory JSONL journal."""

    entry_hash: str


# ---------------------------------------------------------------------------
# Hash-chain helpers
# ---------------------------------------------------------------------------


def _compute_entry_hash(content: _EntryContent) -> str:
    """SHA-256 hash of the canonical JSON of *content*.

    Args:
        content: The entry content (everything except ``entry_hash``).

    Returns:
        Lowercase hex SHA-256 digest.
    """
    raw = canonical_json(content.model_dump(mode="json"))
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Trigger DSL evaluation
# ---------------------------------------------------------------------------


def _apply_op(context: dict[str, JsonValue], field: str, op: str, value: JsonValue) -> bool:
    """Evaluate a single trigger condition against *context*.

    Supported operators: ``eq``, ``neq``, ``gt``, ``gte``, ``lt``, ``lte``,
    ``contains``, ``exists``.

    Args:
        context: Flat key-value map representing the current world state slice.
        field: The context key to test.
        op: Operator string.
        value: Operand (ignored for ``exists``).

    Returns:
        Result of applying the operator.
    """
    if op == "exists":
        return field in context

    ctx_val = context.get(field)

    if op in {"eq", "neq"}:
        eq_result = ctx_val == value
        return eq_result if op == "eq" else not eq_result

    num_fn = _NUMERIC_OPS.get(op)
    if num_fn is not None:
        try:
            return num_fn(float(ctx_val), float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            log.warning(
                "procedural.trigger.non_numeric",
                field=field,
                op=op,
                ctx_val=ctx_val,
                cmp_val=value,
            )
            return False

    if op == "contains":
        in_list = isinstance(ctx_val, list) and value in ctx_val
        in_str = isinstance(ctx_val, str) and isinstance(value, str) and value in ctx_val
        return in_list or in_str

    log.warning("procedural.trigger.unknown_op", op=op)
    return False


def _evaluate_trigger(trigger: dict[str, JsonValue], context: dict[str, JsonValue]) -> bool:
    """Evaluate a trigger specification dict against a context dict.

    Trigger format::

        {
            "conditions": [
                {"field": "attribute", "op": "eq",  "value": "temperature"},
                {"field": "value",     "op": "gt",  "value": 25.0},
            ],
            "logic": "all"   # "all" = AND (default), "any" = OR
        }

    An absent or empty ``conditions`` list means the trigger always fires.

    Args:
        trigger: Trigger specification from :attr:`Rule.trigger`.
        context: Flat key-value map representing the current world state slice.

    Returns:
        ``True`` if the trigger matches the context.
    """
    raw_conditions = trigger.get("conditions")
    if not raw_conditions:
        return True
    if not isinstance(raw_conditions, list):
        log.warning("procedural.trigger.invalid_conditions_type", trigger=trigger)
        return False

    logic = trigger.get("logic", "all")
    results: list[bool] = []

    for raw in raw_conditions:
        if not isinstance(raw, dict):
            log.warning("procedural.trigger.invalid_condition", condition=raw)
            results.append(False)
            continue

        field = raw.get("field")
        op = raw.get("op")
        value = raw.get("value")

        if not isinstance(field, str) or not isinstance(op, str):
            log.warning("procedural.trigger.missing_field_or_op", condition=raw)
            results.append(False)
            continue

        results.append(_apply_op(context, field, op, value))

    if logic == "any":
        return any(results)
    return all(results)


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------


class ProceduralMemory:
    """Versioned rule store backed by a hash-chained JSONL file.

    Rules are appended to a JSONL journal on every mutation.  The current
    state of each rule is maintained in-memory by replaying the journal at
    startup.  The hash chain provides tamper-evidence for the full history.

    File format: one JSON object per line, each with fields ``seq``,
    ``timestamp``, ``prev_hash``, ``op``, ``payload``, ``entry_hash``.

    Args:
        store_path: Path to the JSONL journal file (created on first write).
        clock: Injectable clock; defaults to UTC now.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._path = store_path
        self._clock = clock
        self._lock = asyncio.Lock()
        self._rules: dict[str, Rule] = {}
        self._deprecated: set[str] = set()
        self._seq: int = 0
        self._prev_hash: str = _GENESIS_HASH

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Replay the journal from disk to rebuild in-memory state.

        Idempotent — calling this more than once resets and fully replays.
        Creates an empty journal if the file does not yet exist.

        Raises:
            ProceduralMemoryError: If the journal is unreadable, unparseable,
                or fails the hash-chain integrity check.
        """
        async with self._lock:
            self._rules = {}
            self._deprecated = set()
            self._seq = 0
            self._prev_hash = _GENESIS_HASH

            if not self._path.exists():
                return

            try:
                content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            except OSError as exc:
                raise ProceduralMemoryError(
                    f"Cannot read procedural journal: {self._path}"
                ) from exc

            for lineno, line in enumerate(content.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    entry = _JournalEntry.model_validate(raw)
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise ProceduralMemoryError(
                        f"Journal parse error at line {lineno}: {exc}"
                    ) from exc

                self._verify_chain(entry, lineno)
                self._apply_entry(entry)
                self._seq = entry.seq
                self._prev_hash = entry.entry_hash

            log.info(
                "procedural.loaded",
                rules=len(self._rules),
                deprecated=len(self._deprecated),
                seq=self._seq,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _verify_chain(self, entry: _JournalEntry, lineno: int) -> None:
        """Assert that the hash chain is intact for *entry*.

        Args:
            entry: The entry whose chain links to verify.
            lineno: Source line number for diagnostic messages.

        Raises:
            ProceduralMemoryError: On any chain integrity violation.
        """
        if entry.seq != self._seq + 1:
            raise ProceduralMemoryError(
                f"Sequence gap at line {lineno}: expected seq={self._seq + 1}, got {entry.seq}"
            )
        if entry.prev_hash != self._prev_hash:
            raise ProceduralMemoryError(
                f"Hash chain broken at line {lineno}: "
                f"expected prev_hash={self._prev_hash!r}, got {entry.prev_hash!r}"
            )
        content = _EntryContent(
            seq=entry.seq,
            timestamp=entry.timestamp,
            prev_hash=entry.prev_hash,
            op=entry.op,
            payload=entry.payload,
        )
        expected = _compute_entry_hash(content)
        if entry.entry_hash != expected:
            raise ProceduralMemoryError(
                f"Entry hash mismatch at line {lineno}: "
                f"expected {expected!r}, got {entry.entry_hash!r}"
            )

    def _apply_entry(self, entry: _JournalEntry) -> None:
        """Update in-memory state by applying a single journal entry.

        Args:
            entry: The validated entry to apply.
        """
        if entry.op == "add":
            rule = Rule.model_validate(entry.payload)
            self._rules[rule.id] = rule

        elif entry.op == "reinforce":
            rule_id = str(entry.payload.get("rule_id", ""))
            success = bool(entry.payload.get("success", False))
            if rule_id in self._rules and rule_id not in self._deprecated:
                rule = self._rules[rule_id]
                new_applied = rule.applied_count + 1
                new_success_rate = (
                    rule.success_rate * rule.applied_count + (1.0 if success else 0.0)
                ) / new_applied
                self._rules[rule_id] = rule.model_copy(
                    update={
                        "applied_count": new_applied,
                        "success_rate": round(new_success_rate, 6),
                    }
                )

        elif entry.op == "deprecate":
            rule_id = str(entry.payload.get("rule_id", ""))
            if rule_id in self._rules:
                self._deprecated.add(rule_id)

    async def _append(self, op: _Op, payload: dict[str, JsonValue]) -> None:
        """Append a new hash-chained entry to the JSONL journal.

        Must be called while ``self._lock`` is held by the caller.

        Args:
            op: Operation type.
            payload: Structured data for the operation.

        Raises:
            ProceduralMemoryError: If the journal file cannot be written.
        """
        now = self._clock()
        new_seq = self._seq + 1

        content = _EntryContent(
            seq=new_seq,
            timestamp=now,
            prev_hash=self._prev_hash,
            op=op,
            payload=payload,
        )
        entry_hash = _compute_entry_hash(content)
        entry = _JournalEntry(**content.model_dump(), entry_hash=entry_hash)
        line = entry.model_dump_json() + "\n"

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ProceduralMemoryError(
                f"Cannot append to procedural journal: {self._path}"
            ) from exc

        self._apply_entry(entry)
        self._seq = new_seq
        self._prev_hash = entry_hash

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, rule: Rule) -> None:
        """Persist a new rule to the journal and activate it in memory.

        Args:
            rule: The rule to add.  Its ``id`` must be unique within this
                store.

        Raises:
            ProceduralMemoryError: If a rule with the same ``id`` already
                exists, or if the journal write fails.
        """
        async with self._lock:
            if rule.id in self._rules:
                raise ProceduralMemoryError(f"Rule with id={rule.id!r} already exists.")
            payload = cast(dict[str, JsonValue], rule.model_dump(mode="json"))
            await self._append("add", payload)
            log.info("procedural.rule.added", rule_id=rule.id, source=rule.source)

    async def list_active_rules(self) -> list[Rule]:
        """Return all currently active (non-deprecated) rules.

        Satisfies the :class:`coremind.reflection.rule_learner.RuleSource`
        protocol so the reflection layer can read procedural rules without
        depending on the memory module directly.

        Returns:
            Active rules, highest confidence first.
        """
        async with self._lock:
            active = [
                r for rid, r in self._rules.items() if rid not in self._deprecated
            ]
            active.sort(key=lambda r: r.confidence, reverse=True)
            return active

    async def match(self, context: dict[str, JsonValue]) -> list[Rule]:
        """Return all active rules whose trigger matches the given context.

        Deprecated rules are excluded.  Results are sorted by confidence
        descending so the most-reliable rules come first.

        Args:
            context: Flat key-value map representing the slice of world state
                to evaluate triggers against.

        Returns:
            Matching active rules, highest confidence first.
        """
        async with self._lock:
            matched: list[Rule] = []
            for rule_id, rule in self._rules.items():
                if rule_id in self._deprecated:
                    continue
                if _evaluate_trigger(rule.trigger, context):
                    matched.append(rule)
            matched.sort(key=lambda r: r.confidence, reverse=True)
            return matched

    async def reinforce(self, rule_id: str, success: bool) -> None:
        """Record an outcome for a rule, updating its running success rate.

        Only ``success_rate`` and ``applied_count`` are updated.  ``confidence``
        is the creator's a-priori belief and is not modified by reinforcement;
        callers should consult ``success_rate`` for empirical performance.
        ``success_rate`` is updated as a cumulative mean:
        ``new_rate = (old_rate * old_count + outcome) / new_count``.

        Args:
            rule_id: The ID of the active rule to reinforce.
            success: ``True`` if the rule's proposed action led to a
                successful outcome.

        Raises:
            ProceduralMemoryError: If no active (non-deprecated) rule with
                *rule_id* exists, or if the journal write fails.
        """
        async with self._lock:
            if rule_id not in self._rules or rule_id in self._deprecated:
                raise ProceduralMemoryError(f"No active rule with id={rule_id!r}.")
            payload: dict[str, JsonValue] = {"rule_id": rule_id, "success": success}
            await self._append("reinforce", payload)
            log.info(
                "procedural.rule.reinforced",
                rule_id=rule_id,
                success=success,
                confidence=self._rules[rule_id].confidence,
            )

    async def deprecate(self, rule_id: str, reason: str) -> None:
        """Mark a rule as deprecated so it is excluded from future matching.

        Args:
            rule_id: The ID of the rule to deprecate.
            reason: Human-readable justification (stored in the journal).

        Raises:
            ProceduralMemoryError: If no rule with *rule_id* exists, or if
                the rule is already deprecated, or if the journal write fails.
        """
        async with self._lock:
            if rule_id not in self._rules:
                raise ProceduralMemoryError(f"No rule with id={rule_id!r}.")
            if rule_id in self._deprecated:
                raise ProceduralMemoryError(f"Rule {rule_id!r} is already deprecated.")
            payload: dict[str, JsonValue] = {"rule_id": rule_id, "reason": reason}
            await self._append("deprecate", payload)
            log.info("procedural.rule.deprecated", rule_id=rule_id, reason=reason)
