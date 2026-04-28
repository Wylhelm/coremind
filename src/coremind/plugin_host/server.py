"""gRPC server implementing the CoreMindHost service.

The :class:`PluginHostServer` starts a gRPC server on a Unix domain socket and
registers the :class:`_CoreMindHostServicer` that plugins call back on.

Plugins connect to this server to:
- Emit out-of-band events (outside of the ``Start`` stream).
- Retrieve secrets from the daemon's secrets store.
- Forward structured log entries to the daemon's log stream.

The server does **not** initiate connections to plugins; in Phase 1 plugins are
started manually and connect to the daemon's socket themselves.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import structlog
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict

from coremind.core.event_bus import EventBus
from coremind.crypto.signatures import canonical_json, verify
from coremind.errors import CoreMindError
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind.plugin_host.registry import PluginRegistry
from coremind.world.model import EntityRef, WorldEventRecord

log = structlog.get_logger(__name__)

# Callable that maps secret name → plaintext value, or None when not found.
type SecretsResolver = Callable[[str], str | None]


# ---------------------------------------------------------------------------
# Helper: proto → domain model conversion
# ---------------------------------------------------------------------------


def _proto_value_to_python(proto_value: Any) -> Any:  # noqa: PLR0911 — all oneof kinds need a distinct return
    """Convert a ``google.protobuf.Value`` to a plain Python value.

    Args:
        proto_value: A ``google.protobuf.Value`` message.

    Returns:
        The equivalent Python object (str, float, bool, None, dict, or list).
    """
    kind = proto_value.WhichOneof("kind")
    if kind == "null_value":
        return None
    if kind == "number_value":
        return proto_value.number_value
    if kind == "string_value":
        return proto_value.string_value
    if kind == "bool_value":
        return proto_value.bool_value
    if kind == "struct_value":
        return MessageToDict(proto_value.struct_value)
    if kind == "list_value":
        return [_proto_value_to_python(v) for v in proto_value.list_value.values]
    return None


def _proto_event_to_record(proto_event: Any) -> WorldEventRecord:
    """Convert a ``WorldEvent`` proto message to a :class:`WorldEventRecord`.

    Args:
        proto_event: A ``plugin_pb2.WorldEvent`` message.

    Returns:
        A :class:`WorldEventRecord` suitable for publishing on the EventBus.
    """
    ts: datetime = proto_event.timestamp.ToDatetime(tzinfo=UTC)
    sig_bytes: bytes = proto_event.signature
    sig_b64: str | None = base64.b64encode(sig_bytes).decode() if sig_bytes else None

    return WorldEventRecord(
        id=proto_event.id,
        timestamp=ts,
        source=proto_event.source,
        source_version=proto_event.source_version,
        signature=sig_b64,
        entity=EntityRef(
            type=proto_event.entity.type,
            id=proto_event.entity.entity_id,
        ),
        attribute=proto_event.attribute,
        value=_proto_value_to_python(proto_event.value),
        confidence=proto_event.confidence,
        unit=proto_event.unit or None,
    )


# ---------------------------------------------------------------------------
# gRPC servicer implementation
# ---------------------------------------------------------------------------


class _CoreMindHostServicer(plugin_pb2_grpc.CoreMindHostServicer):
    """Async implementation of the ``CoreMindHost`` gRPC service.

    Args:
        registry: The live plugin registry used to validate event sources
            and manage plugin lifecycle state.
        event_bus: The in-process event bus; accepted events are published here.
        secrets_resolver: Callable mapping a secret name to its plaintext value,
            or ``None`` when the secret is not present in the store.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        event_bus: EventBus,
        secrets_resolver: SecretsResolver,
        plugin_keys_dir: Path = Path.home() / ".coremind" / "keys" / "plugins",
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._secrets_resolver = secrets_resolver
        self._plugin_keys_dir = plugin_keys_dir

    async def RequestSecret(  # noqa: N802
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        """Return the plaintext value of a named secret.

        The caller must identify itself via the ``x-plugin-id`` gRPC metadata
        header, and the named secret must appear in the plugin's declared
        ``required_permissions`` as ``"secrets:<secret_name>"``.

        Args:
            request: ``SecretRequest`` with ``secret_name`` field.
            context: gRPC servicer context for setting response codes.

        Returns:
            ``SecretResponse`` with the plaintext ``secret_value``.
        """
        secret_name: str = request.secret_name
        if not secret_name:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "secret_name must not be empty",
            )
            return plugin_pb2.SecretResponse()

        raw_meta: list[tuple[str, str | bytes]] = list(context.invocation_metadata() or [])
        metadata: dict[str, str] = {
            k: v.decode() if isinstance(v, bytes) else v for k, v in raw_meta
        }
        caller_plugin_id: str = metadata.get("x-plugin-id", "")
        if not caller_plugin_id:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "x-plugin-id gRPC metadata header is required",
            )
            return plugin_pb2.SecretResponse()

        permissions = self._registry.get_required_permissions(caller_plugin_id)
        if permissions is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"plugin {caller_plugin_id!r} is not registered",
            )
            return plugin_pb2.SecretResponse()

        required_perm = f"secrets:{secret_name}"
        if required_perm not in permissions:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"plugin {caller_plugin_id!r} has not declared permission {required_perm!r}",
            )
            return plugin_pb2.SecretResponse()

        value = self._secrets_resolver(secret_name)
        if value is None:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"secret {secret_name!r} not found in store",
            )
            return plugin_pb2.SecretResponse()

        log.debug(
            "plugin_host.secret_served",
            plugin_id=caller_plugin_id,
            secret_name=secret_name,
        )
        return plugin_pb2.SecretResponse(secret_value=value)

    async def EmitEvent(  # noqa: N802, PLR0911 — one return per guard clause + happy path
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        """Accept an out-of-band event, verify it, and publish it on the EventBus.

        The event's ``source`` field must correspond to a registered plugin and
        the ed25519 ``signature`` must verify against that plugin's public key
        over the JCS-canonical form of the event (all fields except
        ``signature`` itself, serialised with snake_case field names).

        Args:
            request: ``WorldEvent`` proto message.
            context: gRPC servicer context.

        Returns:
            ``google.protobuf.Empty``.
        """
        if not request.id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "WorldEvent.id must not be empty",
            )
            return empty_pb2.Empty()
        if not request.source:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "WorldEvent.source must not be empty",
            )
            return empty_pb2.Empty()
        if not request.attribute:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "WorldEvent.attribute must not be empty",
            )
            return empty_pb2.Empty()

        public_key = self._registry.resolve_key(request.source)
        if public_key is None:
            # Auto-register: try to load the plugin's public key from the
            # standard key directory (~/.coremind/keys/plugins/*.ed25519).
            # Key filenames use underscores, plugin IDs use dots.
            key_id = request.source.replace(".", "_")
            key_path = self._plugin_keys_dir / f"{key_id}.ed25519"
            try:
                from cryptography.hazmat.primitives.serialization import (
                    load_pem_private_key,
                )

                # The private key is in PEM format; load it and
                # derive the public key.
                private_key_bytes = key_path.read_bytes()
                private_key = load_pem_private_key(private_key_bytes, None)
                auto_key = private_key.public_key()
                self._registry.register(
                    manifest=plugin_pb2.PluginManifest(
                        plugin_id=request.source,
                        version=request.source_version or "0.0.0",
                        display_name=request.source,
                        kind=plugin_pb2.PLUGIN_KIND_SENSOR,
                    ),
                    public_key=auto_key,
                )
                public_key = auto_key
                log.info(
                    "plugin_host.auto_registered",
                    plugin_id=request.source,
                    key_file=str(key_path),
                )
            except Exception:
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    f"plugin {request.source!r} is not registered",
                )
                return empty_pb2.Empty()

        if public_key is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"plugin {request.source!r} is not registered",
            )
            return empty_pb2.Empty()

        sig_bytes: bytes = request.signature
        if not sig_bytes:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "WorldEvent.signature must not be empty",
            )
            return empty_pb2.Empty()

        event_dict = MessageToDict(request, preserving_proto_field_name=True)
        event_dict.pop("signature", None)
        canonical_payload = canonical_json(event_dict)
        if not verify(canonical_payload, sig_bytes, public_key):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "WorldEvent signature verification failed",
            )
            return empty_pb2.Empty()

        try:
            record = _proto_event_to_record(request)
        except Exception as exc:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"malformed WorldEvent: {exc}",
            )
            return empty_pb2.Empty()

        await self._event_bus.publish(record)
        self._registry.increment_event_count(record.source)
        log.debug(
            "plugin_host.event_emitted",
            event_id=record.id,
            source=record.source,
            attribute=record.attribute,
        )
        return empty_pb2.Empty()

    async def Log(  # noqa: N802
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        """Forward a structured log entry from a plugin to the daemon log stream.

        Args:
            request: ``LogEntry`` proto message.
            context: gRPC servicer context.

        Returns:
            ``google.protobuf.Empty``.
        """
        level_map = {
            plugin_pb2.LOG_LEVEL_DEBUG: "debug",
            plugin_pb2.LOG_LEVEL_INFO: "info",
            plugin_pb2.LOG_LEVEL_WARNING: "warning",
            plugin_pb2.LOG_LEVEL_ERROR: "error",
            plugin_pb2.LOG_LEVEL_CRITICAL: "critical",
        }
        level_name = level_map.get(request.level, "info")
        extra_fields: dict[str, object] = {}
        if request.fields:
            extra_fields = MessageToDict(request.fields)

        raw_meta: list[tuple[str, str | bytes]] = list(context.invocation_metadata() or [])
        meta: dict[str, str] = {k: v.decode() if isinstance(v, bytes) else v for k, v in raw_meta}
        plugin_id: str = meta.get("x-plugin-id", "unknown")
        log_method = getattr(log, level_name, log.info)
        log_method(
            "plugin.log",
            plugin_id=plugin_id,
            message=request.message,
            **extra_fields,
        )
        return empty_pb2.Empty()


# ---------------------------------------------------------------------------
# Server lifecycle wrapper
# ---------------------------------------------------------------------------


class PluginHostServer:
    """Lifecycle wrapper around the CoreMindHost gRPC server.

    Starts a gRPC server on a Unix domain socket and registers the
    :class:`_CoreMindHostServicer`.  The server runs until :meth:`stop` is
    called or the process exits.

    Args:
        socket_path: Filesystem path for the Unix domain socket
            (e.g. ``~/.coremind/run/plugin_host.sock``).
        registry: Plugin registry shared with the daemon.
        event_bus: Event bus to publish received events onto.
        secrets_resolver: Callable for secret lookups (may return ``None``).
        max_workers: Thread-pool size for gRPC I/O (default: 4).
        graceful_shutdown_timeout: Seconds to wait for in-flight RPCs on stop.
    """

    def __init__(
        self,
        socket_path: Path,
        registry: PluginRegistry,
        event_bus: EventBus,
        secrets_resolver: SecretsResolver,
        max_workers: int = 4,
        graceful_shutdown_timeout: float = 5.0,
    ) -> None:
        self._socket_path = socket_path
        self._registry = registry
        self._event_bus = event_bus
        self._secrets_resolver = secrets_resolver
        self._max_workers = max_workers
        self._graceful_shutdown_timeout = graceful_shutdown_timeout
        self._server: grpc.aio.Server | None = None

    async def start(self) -> None:
        """Create the Unix socket directory, bind, and start the gRPC server.

        Raises:
            CoreMindError: If the server cannot bind to the socket path.
        """
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket from a previous run.
        if self._socket_path.exists():
            self._socket_path.unlink()

        servicer = _CoreMindHostServicer(
            registry=self._registry,
            event_bus=self._event_bus,
            secrets_resolver=self._secrets_resolver,
        )

        self._server = grpc.aio.server()
        plugin_pb2_grpc.add_CoreMindHostServicer_to_server(servicer, self._server)  # type: ignore[no-untyped-call]  # generated

        address = f"unix://{self._socket_path}"
        try:
            self._server.add_insecure_port(address)
            await self._server.start()
        except Exception as exc:
            raise CoreMindError(f"plugin host could not bind to {address!r}") from exc

        log.info("plugin_host.started", socket=str(self._socket_path))

    async def stop(self) -> None:
        """Gracefully stop the gRPC server.

        Waits up to ``graceful_shutdown_timeout`` seconds for in-flight RPCs
        to complete before forcibly closing.
        """
        if self._server is None:
            return
        await self._server.stop(self._graceful_shutdown_timeout)
        self._server = None
        log.info("plugin_host.stopped")

    async def wait_for_termination(self) -> None:
        """Block until the server has terminated.

        Intended for use in the daemon's main loop when running the server
        as a long-lived task.
        """
        if self._server is not None:
            await self._server.wait_for_termination()
