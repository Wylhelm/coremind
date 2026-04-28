"""Hash-chained, signed audit journal for the action layer (L6).

Every side effect the daemon produces is committed to this journal *before*
dispatching to the effector plugin, so that a failure after dispatch never
leaves the audit trail out of sync with intent.

File layout
-----------

One JSON object per line at ``~/.coremind/audit.log`` (owner-only, chmod 600).
Each line is an immutable :class:`_JournalEntry` with:

- ``seq`` — monotonically increasing sequence number starting at 1.
- ``prev_hash`` — the ``entry_hash`` of the previous entry (or 64 zeros at
  genesis).
- ``payload`` — the serialised :class:`Action` (or meta-event; see
  :meth:`ActionJournal.append_meta`).
- ``entry_hash`` — SHA-256 of the canonical JSON of every field above.
- ``signature`` — base64 ed25519 signature of ``entry_hash`` by the daemon.

Operations
----------

- :meth:`ActionJournal.append` writes a full :class:`Action` entry.
- :meth:`ActionJournal.append_meta` writes a meta-event (used for
  ``security.category.override_blocked``, ``approval.*``, etc.).
- :meth:`ActionJournal.update_result` rewrites the file to record a dispatch
  outcome.  The entry's chain signature is preserved: the ``result`` field is
  NOT hashed, so a later update never breaks the chain (see
  ``_content_dict_for_hash``).
- :meth:`ActionJournal.verify` walks the chain from disk.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coremind.action.schemas import Action
from coremind.crypto.signatures import canonical_json, sign, verify
from coremind.errors import JournalError
from coremind.world.model import JsonValue

log = structlog.get_logger(__name__)

_GENESIS_HASH: str = "0" * 64
_JOURNAL_MODE: int = 0o600

type _EntryKind = Literal["action", "meta"]
type Clock = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Journal entry model
# ---------------------------------------------------------------------------


class _JournalEntry(BaseModel):
    """A single immutable entry in the journal.

    The ``entry_hash`` covers ``seq``, ``timestamp``, ``prev_hash``, ``kind``,
    and ``payload`` — every field except ``entry_hash``, ``signature``, and
    the mutable ``result`` inside an action payload.  That carve-out lets the
    dispatcher record an outcome without breaking the chain.
    """

    model_config = ConfigDict(frozen=False)

    seq: int = Field(ge=1)
    timestamp: datetime
    prev_hash: str = Field(min_length=64, max_length=64)
    kind: _EntryKind
    payload: dict[str, JsonValue]
    entry_hash: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=1)


def _content_dict_for_hash(
    *,
    seq: int,
    timestamp: datetime,
    prev_hash: str,
    kind: _EntryKind,
    payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return the hash input for a journal entry.

    For action payloads we strip the ``result`` field so :meth:`update_result`
    can rewrite outcomes without invalidating the chain.

    Args:
        seq: Sequence number.
        timestamp: Entry timestamp.
        prev_hash: Hash of the previous entry.
        kind: ``"action"`` or ``"meta"``.
        payload: The entry payload.

    Returns:
        A dict suitable for :func:`canonical_json`.
    """
    scrub: dict[str, JsonValue] = dict(payload)
    if kind == "action":
        scrub.pop("result", None)
    return {
        "seq": seq,
        "timestamp": timestamp.isoformat(),
        "prev_hash": prev_hash,
        "kind": kind,
        "payload": scrub,
    }


def _compute_entry_hash(
    *,
    seq: int,
    timestamp: datetime,
    prev_hash: str,
    kind: _EntryKind,
    payload: dict[str, JsonValue],
) -> str:
    """SHA-256 of the canonical JSON of the hashable entry content."""
    content = _content_dict_for_hash(
        seq=seq,
        timestamp=timestamp,
        prev_hash=prev_hash,
        kind=kind,
        payload=payload,
    )
    return hashlib.sha256(canonical_json(cast("dict[str, object]", content))).hexdigest()


# ---------------------------------------------------------------------------
# ActionJournal
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class ActionJournal:
    """Append-only, hash-chained, signed audit journal.

    Args:
        path: Path to the JSONL file.
        private_key: Daemon ed25519 private key used to sign every entry.
        public_key: Corresponding public key used by :meth:`verify`.
        clock: Injectable clock.
    """

    def __init__(
        self,
        path: Path,
        private_key: Ed25519PrivateKey,
        public_key: Ed25519PublicKey,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._path = path
        self._private_key = private_key
        self._public_key = public_key
        self._clock = clock
        self._lock = asyncio.Lock()
        self._seq: int = 0
        self._prev_hash: str = _GENESIS_HASH
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Replay the on-disk journal to initialise ``seq`` and ``prev_hash``.

        Also enforces the chmod-600 invariant on the file if it exists.
        Idempotent — repeated calls are safe.

        Raises:
            JournalError: On any integrity or I/O failure.
        """
        async with self._lock:
            self._seq = 0
            self._prev_hash = _GENESIS_HASH
            self._loaded = True

            if not self._path.exists():
                return

            _assert_mode(self._path)

            try:
                content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            except OSError as exc:
                raise JournalError(f"cannot read audit journal: {self._path}") from exc

            for lineno, line in enumerate(content.splitlines(), start=1):
                if not line.strip():
                    continue
                entry = _parse_line(line, lineno)
                self._verify_entry(entry, lineno)
                self._seq = entry.seq
                self._prev_hash = entry.entry_hash

    # ------------------------------------------------------------------
    # Append operations
    # ------------------------------------------------------------------

    async def append(self, action: Action) -> Action:
        """Sign and append *action* to the journal, returning the signed copy.

        Mutates ``action.signature`` with the action-level signature covering
        everything except the signature and mutable result fields.  Then
        writes a new hash-chained line with the daemon's signature on the
        entry hash.

        Args:
            action: The action to journal.  Its ``signature`` is set in-place.

        Returns:
            The same ``Action`` instance, now carrying a signature.

        Raises:
            JournalError: If the journal has not been loaded or the write fails.
        """
        if not self._loaded:
            raise JournalError("ActionJournal.load() must be called before append()")

        action.signature = self._sign_action(action)
        payload = _action_to_payload(action)
        await self._write_entry("action", payload)
        log.info(
            "action.journaled",
            action_id=action.id,
            intent_id=action.intent_id,
            category=action.category,
            operation=action.operation,
        )
        return action

    async def append_meta(
        self,
        meta_type: str,
        payload: dict[str, JsonValue],
    ) -> None:
        """Append a meta-event entry.

        Used for non-action events that still require auditability:
        ``security.category.override_blocked``, ``approval.requested``,
        ``approval.response``, ``approval.expired``, etc.

        Args:
            meta_type: Dot-qualified meta-event type.
            payload: Structured payload.  The caller is responsible for
                keeping it JSON-serialisable.
        """
        if not self._loaded:
            raise JournalError("ActionJournal.load() must be called before append_meta()")

        body: dict[str, JsonValue] = {"type": meta_type, "data": payload}
        await self._write_entry("meta", body)
        log.info("action.meta_journaled", meta_type=meta_type)

    async def update_result(self, action: Action) -> None:
        """Record the execution ``result`` for an already-journaled action.

        Rewrites the journal file in place with the single matching entry's
        ``result`` field updated; the chain hashes are preserved because
        ``result`` is excluded from the hash input.

        Args:
            action: The same :class:`Action` instance previously passed to
                :meth:`append`, now carrying a :class:`ActionResult`.

        Raises:
            JournalError: If the action is not found in the journal.
        """
        if action.result is None:
            raise JournalError(f"action {action.id!r} has no result; nothing to update")

        async with self._lock:
            if not self._path.exists():
                raise JournalError(f"journal {self._path} missing; cannot update")

            content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            lines = content.splitlines()
            updated: list[str] = []
            found = False

            for line in lines:
                if not line.strip():
                    updated.append(line)
                    continue
                entry = _parse_line(line, len(updated) + 1)
                if entry.kind == "action" and entry.payload.get("id") == action.id:
                    new_payload = _action_to_payload(action)
                    entry.payload = new_payload
                    updated.append(entry.model_dump_json())
                    found = True
                else:
                    updated.append(line)

            if not found:
                raise JournalError(f"action {action.id!r} not found in journal")

            await asyncio.to_thread(_atomic_rewrite, self._path, "\n".join(updated) + "\n")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify(self) -> VerifyReport:  # noqa: PLR0911 — each return reports a distinct integrity failure mode
        """Walk the full journal end-to-end and report on its integrity.

        Returns:
            A :class:`VerifyReport` with the entry count and the first break
            encountered, if any.
        """
        if not self._path.exists():
            return VerifyReport(entries=0, ok=True, broken_at=None, reason=None)

        try:
            content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        except OSError as exc:
            return VerifyReport(entries=0, ok=False, broken_at=None, reason=str(exc))

        seq = 0
        prev_hash = _GENESIS_HASH
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = _parse_line(line, lineno)
            except JournalError as exc:
                return VerifyReport(entries=seq, ok=False, broken_at=lineno, reason=str(exc))

            if entry.seq != seq + 1:
                return VerifyReport(
                    entries=seq,
                    ok=False,
                    broken_at=lineno,
                    reason=f"sequence gap: expected {seq + 1}, got {entry.seq}",
                )
            if entry.prev_hash != prev_hash:
                return VerifyReport(
                    entries=seq,
                    ok=False,
                    broken_at=lineno,
                    reason="prev_hash mismatch",
                )
            expected = _compute_entry_hash(
                seq=entry.seq,
                timestamp=entry.timestamp,
                prev_hash=entry.prev_hash,
                kind=entry.kind,
                payload=entry.payload,
            )
            if entry.entry_hash != expected:
                return VerifyReport(
                    entries=seq,
                    ok=False,
                    broken_at=lineno,
                    reason="entry_hash mismatch",
                )
            try:
                sig_bytes = base64.b64decode(entry.signature)
            except (ValueError, TypeError):
                return VerifyReport(
                    entries=seq,
                    ok=False,
                    broken_at=lineno,
                    reason="signature not base64",
                )
            if not verify(
                entry.entry_hash.encode("ascii"),
                sig_bytes,
                self._public_key,
            ):
                return VerifyReport(
                    entries=seq,
                    ok=False,
                    broken_at=lineno,
                    reason="daemon signature invalid",
                )
            if entry.kind == "action":
                action_check = _check_action_signature(entry.payload, self._public_key)
                if action_check is not None:
                    return VerifyReport(
                        entries=seq,
                        ok=False,
                        broken_at=lineno,
                        reason=action_check,
                    )
            seq = entry.seq
            prev_hash = entry.entry_hash

        return VerifyReport(entries=seq, ok=True, broken_at=None, reason=None)

    async def read_all(self) -> list[_JournalEntry]:
        """Return every entry from disk, newest last.  Test helper."""
        if not self._path.exists():
            return []
        content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        out: list[_JournalEntry] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            out.append(_parse_line(line, lineno))
        return out

    async def read_recent(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[_JournalEntry]:
        """Return entries newest-first, optionally bounded by ``since``.

        Args:
            limit: Maximum number of entries to return.  Must be ``>= 1``.
            since: When provided, drop entries strictly older than this
                timestamp before applying ``limit``.

        Returns:
            Up to ``limit`` entries ordered newest-first.  An empty list is
            returned when the journal file does not yet exist.
        """
        entries = await self.read_all()
        if since is not None:
            entries = [e for e in entries if e.timestamp >= since]
        # ``read_all`` returns oldest-first; the dashboard wants newest-first.
        entries.reverse()
        return entries[:limit]

    async def find_action(self, action_id: str) -> Action | None:
        """Return the :class:`Action` with ``id == action_id`` or ``None``.

        Scans the journal for the most recent entry matching ``action_id``
        and reconstructs an :class:`Action` from the payload.  Meta-events
        are skipped.
        """
        entries = await self.read_all()
        for entry in reversed(entries):
            if entry.kind != "action":
                continue
            if entry.payload.get("id") == action_id:
                return Action.model_validate(entry.payload)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sign_action(self, action: Action) -> str:
        """Return the base64 ed25519 signature of *action*'s canonical form."""
        payload = _action_to_payload(action)
        payload.pop("signature", None)
        payload.pop("result", None)
        sig = sign(canonical_json(cast("dict[str, object]", payload)), self._private_key)
        return base64.b64encode(sig).decode("ascii")

    def _verify_entry(self, entry: _JournalEntry, lineno: int) -> None:
        """Raise :class:`JournalError` if *entry* breaks the chain."""
        if entry.seq != self._seq + 1:
            raise JournalError(
                f"sequence gap at line {lineno}: expected {self._seq + 1}, got {entry.seq}"
            )
        if entry.prev_hash != self._prev_hash:
            raise JournalError(f"hash chain broken at line {lineno}")
        expected = _compute_entry_hash(
            seq=entry.seq,
            timestamp=entry.timestamp,
            prev_hash=entry.prev_hash,
            kind=entry.kind,
            payload=entry.payload,
        )
        if entry.entry_hash != expected:
            raise JournalError(f"entry_hash mismatch at line {lineno}")
        try:
            sig_bytes = base64.b64decode(entry.signature)
        except (ValueError, TypeError) as exc:
            raise JournalError(f"signature not base64 at line {lineno}") from exc
        if not verify(
            entry.entry_hash.encode("ascii"),
            sig_bytes,
            self._public_key,
        ):
            raise JournalError(f"daemon signature invalid at line {lineno}")
        if entry.kind == "action":
            failure = _check_action_signature(entry.payload, self._public_key)
            if failure is not None:
                raise JournalError(f"{failure} at line {lineno}")

    async def _write_entry(
        self,
        kind: _EntryKind,
        payload: dict[str, JsonValue],
    ) -> None:
        """Hold the lock, build a hash-chained entry, and append it."""
        async with self._lock:
            if not self._loaded:
                raise JournalError("journal not loaded")

            now = self._clock()
            seq = self._seq + 1
            entry_hash = _compute_entry_hash(
                seq=seq,
                timestamp=now,
                prev_hash=self._prev_hash,
                kind=kind,
                payload=payload,
            )
            sig_bytes = sign(entry_hash.encode("ascii"), self._private_key)
            sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
            entry = _JournalEntry(
                seq=seq,
                timestamp=now,
                prev_hash=self._prev_hash,
                kind=kind,
                payload=payload,
                entry_hash=entry_hash,
                signature=sig_b64,
            )

            line = entry.model_dump_json() + "\n"

            def _write() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                # Enforce chmod 600 every time we touch the file.
                with contextlib.suppress(OSError):
                    self._path.chmod(_JOURNAL_MODE)

            try:
                await asyncio.to_thread(_write)
            except OSError as exc:
                raise JournalError(f"cannot append to journal: {self._path}") from exc

            self._seq = seq
            self._prev_hash = entry_hash


# ---------------------------------------------------------------------------
# Verify report
# ---------------------------------------------------------------------------


class VerifyReport(BaseModel):
    """Result of walking the journal end-to-end."""

    model_config = ConfigDict(frozen=True)

    entries: int
    ok: bool
    broken_at: int | None
    reason: str | None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _action_to_payload(action: Action) -> dict[str, JsonValue]:
    """Convert an :class:`Action` to a JSON-safe dict suitable for the journal."""
    return action.model_dump(mode="json")


def _check_action_signature(
    payload: dict[str, JsonValue],
    public_key: Ed25519PublicKey,
) -> str | None:
    """Return ``None`` if the embedded action signature verifies, else an error.

    The signature covers every field except ``signature`` and ``result``
    (matching :meth:`ActionJournal._sign_action`).
    """
    sig_b64 = payload.get("signature")
    if not isinstance(sig_b64, str) or not sig_b64:
        return "action signature missing"
    try:
        sig_bytes = base64.b64decode(sig_b64)
    except (ValueError, TypeError):
        return "action signature not base64"
    body: dict[str, JsonValue] = dict(payload)
    body.pop("signature", None)
    body.pop("result", None)
    if not verify(canonical_json(cast("dict[str, object]", body)), sig_bytes, public_key):
        return "action signature invalid"
    return None


def _parse_line(line: str, lineno: int) -> _JournalEntry:
    """Parse one JSONL line into a :class:`_JournalEntry`."""
    try:
        raw = json.loads(line)
        return _JournalEntry.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise JournalError(f"malformed journal line {lineno}: {exc}") from exc


def _assert_mode(path: Path) -> None:
    """Raise :class:`JournalError` if *path* is group/world-readable."""
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise JournalError(
            f"audit journal {path} has insecure permissions "
            f"{stat.filemode(st.st_mode)}; expected owner-only (0600)"
        )


def _atomic_rewrite(path: Path, content: str) -> None:
    """Atomically replace *path* with *content* (chmod 600)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    tmp_path = Path(tmp)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fchmod(fd, _JOURNAL_MODE)
        os.close(fd)
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
