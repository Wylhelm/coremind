"""Plugin lifecycle registry.

Maintains the set of currently connected plugins.  Each entry records the
plugin's manifest, its registered ed25519 public key, the last-known health
state, and a running event count.

This registry is the single source of truth used by:
- ``WorldStore`` (via the key-resolver callable) to verify event signatures.
- ``coremind plugin list`` / ``coremind plugin info`` CLI commands.
- The plugin host server when accepting or rejecting new registrations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel

from coremind.errors import CoreMindError

log = structlog.get_logger(__name__)

_SEMVER_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+$")
_PLUGIN_KIND_UNSPECIFIED: int = 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistrationError(CoreMindError):
    """Raised when a plugin fails manifest validation or is already registered."""


# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------


class PluginInfo(BaseModel):
    """Serialisable snapshot of a plugin's registry entry.

    Returned by :meth:`PluginRegistry.get_info` and consumed by the CLI.
    """

    plugin_id: str
    version: str
    display_name: str
    kind: int
    provides_entities: list[str]
    emits_attributes: list[str]
    accepts_operations: list[str]
    required_permissions: list[str]
    health_state: str
    event_count: int
    connected_at: datetime
    public_key_pem: str


# ---------------------------------------------------------------------------
# Internal record (not Pydantic — Ed25519PublicKey is not serialisable)
# ---------------------------------------------------------------------------


@dataclass
class _PluginRecord:
    """Internal in-memory entry for a registered plugin."""

    plugin_id: str
    version: str
    display_name: str
    kind: int
    provides_entities: list[str]
    emits_attributes: list[str]
    accepts_operations: list[str]
    required_permissions: list[str]
    public_key: Ed25519PublicKey
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    health_state: str = "OK"
    event_count: int = 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PluginRegistry:
    """In-memory registry of currently connected plugins.

    Thread-safety: all mutations are expected to happen from a single asyncio
    event loop; no locking is needed beyond asyncio's cooperative concurrency.

    Args:
        max_plugins: Hard cap on simultaneously connected plugins.
    """

    def __init__(self, max_plugins: int = 64) -> None:
        self._records: dict[str, _PluginRecord] = {}
        self._max_plugins = max_plugins

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(
        self,
        manifest: Any,  # plugin_pb2.PluginManifest  — typed Any to avoid heavy import in hints
        public_key: Ed25519PublicKey,
    ) -> None:
        """Validate *manifest* and add the plugin to the registry.

        Args:
            manifest: A ``PluginManifest`` proto message returned by the
                plugin's ``Identify`` RPC.
            public_key: The plugin's ed25519 public key.  Stored for later
                event-signature verification.

        Raises:
            RegistrationError: If the manifest is invalid or the plugin
                is already registered.
        """
        _validate_manifest(manifest)

        plugin_id: str = manifest.plugin_id
        if plugin_id in self._records:
            raise RegistrationError(
                f"plugin {plugin_id!r} is already registered; send Stop before re-registering"
            )
        if len(self._records) >= self._max_plugins:
            raise RegistrationError(
                f"plugin limit ({self._max_plugins}) reached; cannot register {plugin_id!r}"
            )

        self._records[plugin_id] = _PluginRecord(
            plugin_id=plugin_id,
            version=manifest.version,
            display_name=manifest.display_name,
            kind=int(manifest.kind),
            provides_entities=list(manifest.provides_entities),
            emits_attributes=list(manifest.emits_attributes),
            accepts_operations=list(manifest.accepts_operations),
            required_permissions=list(manifest.required_permissions),
            public_key=public_key,
        )
        log.info("plugin_host.registered", plugin_id=plugin_id, version=manifest.version)

    def unregister(self, plugin_id: str) -> None:
        """Remove the plugin from the registry.

        Args:
            plugin_id: The plugin identifier to remove.

        Raises:
            RegistrationError: If the plugin is not currently registered.
        """
        if plugin_id not in self._records:
            raise RegistrationError(f"plugin {plugin_id!r} is not registered")
        del self._records[plugin_id]
        log.info("plugin_host.unregistered", plugin_id=plugin_id)

    def increment_event_count(self, plugin_id: str) -> None:
        """Increment the event counter for *plugin_id* by one.

        Args:
            plugin_id: The plugin identifier whose counter to increment.
                Silently ignored when the plugin is not registered (it may
                have disconnected mid-stream).
        """
        record = self._records.get(plugin_id)
        if record is not None:
            record.event_count += 1

    def update_health(self, plugin_id: str, state: str) -> None:
        """Update the health state string for *plugin_id*.

        Args:
            plugin_id: The plugin identifier to update.
            state: Human-readable health state label (e.g. ``"OK"``,
                ``"DEGRADED"``, ``"UNHEALTHY"``).
        """
        record = self._records.get(plugin_id)
        if record is not None:
            record.health_state = state

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def resolve_key(self, plugin_id: str) -> Ed25519PublicKey | None:
        """Return the public key for *plugin_id*, or ``None`` if unknown.

        Used as the ``KeyResolver`` callable passed to :class:`WorldStore`.

        Args:
            plugin_id: Plugin source identifier from a ``WorldEventRecord``.

        Returns:
            The registered ``Ed25519PublicKey``, or ``None`` when the plugin
            is not in the registry.
        """
        record = self._records.get(plugin_id)
        return record.public_key if record is not None else None

    def is_registered(self, plugin_id: str) -> bool:
        """Return ``True`` if *plugin_id* is currently registered.

        Args:
            plugin_id: The plugin identifier to check.
        """
        return plugin_id in self._records

    def get_required_permissions(self, plugin_id: str) -> list[str] | None:
        """Return the declared ``required_permissions`` for *plugin_id*, or ``None``.

        Used by the plugin host server to enforce secret access control before
        serving a secret to a calling plugin.

        Args:
            plugin_id: The plugin identifier to look up.

        Returns:
            A copy of the permission strings declared in the plugin's manifest,
            or ``None`` when the plugin is not registered.
        """
        record = self._records.get(plugin_id)
        return list(record.required_permissions) if record is not None else None

    def list_plugins(self) -> list[PluginInfo]:
        """Return a snapshot of all registered plugins suitable for CLI display.

        Returns:
            Sorted list of :class:`PluginInfo` models (sorted by plugin_id).
        """
        return sorted(
            (_record_to_info(r) for r in self._records.values()),
            key=lambda p: p.plugin_id,
        )

    def get_info(self, plugin_id: str) -> PluginInfo | None:
        """Return the :class:`PluginInfo` for *plugin_id*, or ``None``.

        Args:
            plugin_id: The plugin identifier to look up.
        """
        record = self._records.get(plugin_id)
        return _record_to_info(record) if record is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_manifest(manifest: Any) -> None:
    """Validate a ``PluginManifest`` proto message.

    Version format is strict ``MAJOR.MINOR.PATCH`` only; pre-release identifiers
    (e.g. ``1.0.0-alpha``) and build metadata (e.g. ``1.0.0+build.1``) are not
    accepted by design to keep the version field unambiguous across tooling.

    Args:
        manifest: ``PluginManifest`` to validate.

    Raises:
        RegistrationError: If any required field is invalid.
    """
    if not manifest.plugin_id:
        raise RegistrationError("manifest.plugin_id must not be empty")

    if not manifest.display_name:
        raise RegistrationError("manifest.display_name must not be empty")

    if not _SEMVER_RE.match(manifest.version):
        raise RegistrationError(
            f"manifest.version {manifest.version!r} is not a valid semver string "
            "(expected MAJOR.MINOR.PATCH)"
        )

    if int(manifest.kind) == _PLUGIN_KIND_UNSPECIFIED:
        raise RegistrationError("manifest.kind must not be PLUGIN_KIND_UNSPECIFIED")


def _record_to_info(record: _PluginRecord) -> PluginInfo:
    """Convert an internal ``_PluginRecord`` to a serialisable :class:`PluginInfo`.

    Args:
        record: The internal record to convert.

    Returns:
        A :class:`PluginInfo` model suitable for CLI display.
    """
    pub_pem = record.public_key.public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return PluginInfo(
        plugin_id=record.plugin_id,
        version=record.version,
        display_name=record.display_name,
        kind=record.kind,
        provides_entities=record.provides_entities,
        emits_attributes=record.emits_attributes,
        accepts_operations=record.accepts_operations,
        required_permissions=record.required_permissions,
        health_state=record.health_state,
        event_count=record.event_count,
        connected_at=record.connected_at,
        public_key_pem=pub_pem,
    )
