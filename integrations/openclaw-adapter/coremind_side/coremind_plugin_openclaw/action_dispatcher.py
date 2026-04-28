"""CoreMind Action → OpenClaw RPC dispatcher with permission narrowing.

This module validates incoming effector actions against a JSON Schema, checks
them against the adapter's configured permission scope (which may be narrower
than the manifest's ``required_permissions``), and translates them into
calls on the :class:`OpenClawClient`.

The dispatcher is transport-agnostic: the caller passes in an
``OpenClawClient`` protocol implementation and the action params.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog
from google.protobuf.struct_pb2 import Struct

from coremind_plugin_openclaw._generated import adapter_pb2

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DispatchError(Exception):
    """Base class for dispatcher errors."""


class PermissionDeniedError(DispatchError):
    """Raised when the action is outside the configured permission scope."""


class UnknownOperationError(DispatchError):
    """Raised when the operation name is not registered with the dispatcher."""


class InvalidParametersError(DispatchError):
    """Raised when the action parameters fail JSON Schema validation."""


# ---------------------------------------------------------------------------
# OpenClaw client protocol (transport boundary)
# ---------------------------------------------------------------------------


class OpenClawClient(Protocol):
    """Protocol implemented by any transport to the OpenClaw half.

    Declaring a narrow protocol here lets us test the dispatcher against an
    in-memory fake without standing up a gRPC server.
    """

    async def notify(self, request: adapter_pb2.NotifyRequest) -> adapter_pb2.NotifyResult: ...

    async def request_approval(
        self, request: adapter_pb2.ApprovalRequest
    ) -> adapter_pb2.ApprovalResult: ...

    async def invoke_skill(
        self, request: adapter_pb2.SkillInvocation
    ) -> adapter_pb2.SkillResult: ...

    async def schedule_cron(
        self, request: adapter_pb2.CronScheduleRequest
    ) -> adapter_pb2.CronScheduleResult: ...

    async def cancel_cron(
        self, request: adapter_pb2.CronCancelRequest
    ) -> adapter_pb2.CronCancelResult: ...


# ---------------------------------------------------------------------------
# Permission scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionScope:
    """The live permission scope configured for this adapter instance.

    Values are glob patterns — ``telegram`` matches only the literal channel
    name, ``*`` matches anything, ``telegram*`` matches any channel whose
    identifier starts with ``telegram``.

    The scope is always a **subset** of the manifest's ``required_permissions``.
    Narrowing is expressed via the adapter's config TOML.
    """

    allowed_channels: tuple[str, ...] = ("*",)
    allowed_skills: tuple[str, ...] = ("*",)
    cron_manage: bool = True

    def allows_channel(self, channel: str) -> bool:
        """Return ``True`` if *channel* matches any allowed-channel glob."""
        return any(fnmatch.fnmatchcase(channel, pat) for pat in self.allowed_channels)

    def allows_skill(self, skill: str) -> bool:
        """Return ``True`` if *skill* matches any allowed-skill glob."""
        return any(fnmatch.fnmatchcase(skill, pat) for pat in self.allowed_skills)


# ---------------------------------------------------------------------------
# Schema loading & validation
# ---------------------------------------------------------------------------


@dataclass
class _OperationSpec:
    """Static per-operation configuration held by the dispatcher."""

    name: str
    schema: dict[str, Any]
    reversible: bool | str = False
    reversed_by_operation: str | None = None


def _load_schemas(schema_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all ``*.json`` schemas in *schema_dir* keyed by $id's trailing segment."""
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir.glob("*.json")):
        with path.open("rb") as fh:
            schema = json.load(fh)
        # Derive the operation name from $id (last dotted segment).
        sid: str = schema.get("$id", path.stem)
        op_suffix = sid.rsplit(".", 1)[-1]
        schemas[f"openclaw.{op_suffix}"] = schema
    return schemas


def _load_reversibility(manifest_path: Path) -> dict[str, tuple[bool | str, str | None]]:
    """Extract ``(reversible, reversed_by_operation)`` per op from a manifest TOML.

    Missing file or ``accepts_operations_schemas`` table → empty dict. Used to
    wire Phase 3 approval gating into the dispatcher without re-declaring the
    reversibility policy in Python code.
    """
    if not manifest_path.exists():
        return {}
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, tuple[bool | str, str | None]] = {}
    for entry in data.get("accepts_operations_schemas", []):
        name = str(entry.get("name", ""))
        if not name:
            continue
        reversible_raw: Any = entry.get("reversible", False)
        reversible: bool | str = (
            reversible_raw if isinstance(reversible_raw, bool) else str(reversible_raw)
        )
        reversed_by = entry.get("reversed_by_operation")
        out[name] = (reversible, str(reversed_by) if reversed_by is not None else None)
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class ActionDispatcher:
    """Validates actions and forwards them to the OpenClaw half.

    Args:
        client: OpenClaw RPC client.
        scope: Configured permission scope.
        schema_dir: Directory containing operation JSON Schemas.
        manifest_path: Path to ``manifest.toml``. Used to load per-operation
            reversibility metadata. Defaults to ``schema_dir.parent / "manifest.toml"``.
    """

    client: OpenClawClient
    scope: PermissionScope
    schema_dir: Path
    manifest_path: Path | None = None
    _operations: dict[str, _OperationSpec] = field(init=False)

    def __post_init__(self) -> None:
        schemas = _load_schemas(self.schema_dir)
        manifest = self.manifest_path or (self.schema_dir.parent / "manifest.toml")
        reversibility = _load_reversibility(manifest)
        self._operations = {
            name: _OperationSpec(
                name=name,
                schema=schema,
                reversible=reversibility.get(name, (False, None))[0],
                reversed_by_operation=reversibility.get(name, (False, None))[1],
            )
            for name, schema in schemas.items()
        }

    def reversibility_of(self, operation: str) -> tuple[bool | str, str | None]:
        """Return ``(reversible, reversed_by_operation)`` for *operation*.

        Raises :class:`UnknownOperationError` if not registered. Phase 3's
        approval layer uses this to gate high-risk actions.
        """
        spec = self._operations.get(operation)
        if spec is None:
            raise UnknownOperationError(f"operation {operation!r} is not registered")
        return spec.reversible, spec.reversed_by_operation

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_params(self, operation: str, params: Mapping[str, Any]) -> None:
        """Validate *params* against the JSON Schema registered for *operation*."""
        # Local import: jsonschema is a dev dependency; we want to keep the
        # module importable without it for non-dispatch callers.
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]  # noqa: PLC0415

        spec = self._operations.get(operation)
        if spec is None:
            raise UnknownOperationError(f"operation {operation!r} is not registered")
        validator = Draft202012Validator(spec.schema)
        errors = sorted(validator.iter_errors(params), key=lambda e: e.path)
        if errors:
            messages = "; ".join(
                f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors
            )
            raise InvalidParametersError(f"{operation}: {messages}")

    def _enforce_scope(self, operation: str, params: Mapping[str, Any]) -> None:
        """Reject actions that are outside the configured permission scope."""
        if operation in {"openclaw.notify", "openclaw.approve_request"}:
            channel = str(params.get("channel", ""))
            if not self.scope.allows_channel(channel):
                raise PermissionDeniedError(
                    f"channel {channel!r} is not in allowed_channels={self.scope.allowed_channels}"
                )
        elif operation == "openclaw.invoke_skill":
            skill = str(params.get("skill_name", ""))
            if not self.scope.allows_skill(skill):
                raise PermissionDeniedError(
                    f"skill {skill!r} is not in allowed_skills={self.scope.allowed_skills}"
                )
        elif operation in {"openclaw.schedule_cron", "openclaw.cancel_cron"}:
            if not self.scope.cron_manage:
                raise PermissionDeniedError(
                    "cron management is disabled by the adapter's permission scope"
                )
            if operation == "openclaw.schedule_cron":
                skill = str(params.get("skill_name", ""))
                if not self.scope.allows_skill(skill):
                    raise PermissionDeniedError(
                        f"cron target skill {skill!r} is not in allowed_skills"
                    )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate *params*, enforce the scope, and invoke the OpenClaw half.

        Returns a JSON-safe dict describing the outcome. Exceptions are raised
        for authorization failures, unknown operations, and schema violations;
        transport errors propagate from the client.
        """
        self._validate_params(operation, params)
        self._enforce_scope(operation, params)

        log.info(
            "openclaw_adapter.dispatch",
            operation=operation,
            # Don't log free-form bodies; length is enough for observability.
            param_keys=sorted(params.keys()),
        )

        if operation == "openclaw.notify":
            return _notify_result_to_dict(await self.client.notify(_to_notify_request(params)))
        if operation == "openclaw.approve_request":
            return _approval_result_to_dict(
                await self.client.request_approval(_to_approval_request(params))
            )
        if operation == "openclaw.invoke_skill":
            return _skill_result_to_dict(
                await self.client.invoke_skill(_to_skill_invocation(params))
            )
        if operation == "openclaw.schedule_cron":
            return _cron_schedule_result_to_dict(
                await self.client.schedule_cron(_to_cron_schedule_request(params))
            )
        if operation == "openclaw.cancel_cron":
            return _cron_cancel_result_to_dict(
                await self.client.cancel_cron(_to_cron_cancel_request(params))
            )
        raise UnknownOperationError(f"operation {operation!r} has no handler")


# ---------------------------------------------------------------------------
# Proto builders
# ---------------------------------------------------------------------------


def _params_to_struct(raw: Mapping[str, Any] | None) -> Struct:
    """Convert a JSON-safe dict to a ``google.protobuf.Struct``."""
    struct = Struct()
    if raw:
        struct.update(dict(raw))
    return struct


def _to_notify_request(params: Mapping[str, Any]) -> adapter_pb2.NotifyRequest:
    req = adapter_pb2.NotifyRequest(
        channel=str(params["channel"]),
        target=str(params["target"]),
        text=str(params["text"]),
    )
    metadata = params.get("metadata") or {}
    for key, value in metadata.items():
        req.metadata[str(key)] = str(value)
    return req


def _to_approval_request(params: Mapping[str, Any]) -> adapter_pb2.ApprovalRequest:
    req = adapter_pb2.ApprovalRequest(
        approval_id=str(params["approval_id"]),
        channel=str(params["channel"]),
        target=str(params["target"]),
        prompt=str(params["prompt"]),
        timeout_seconds=int(params.get("timeout_seconds", 0)),
    )
    context = params.get("context")
    if context:
        req.context.update(dict(context))
    return req


def _to_skill_invocation(params: Mapping[str, Any]) -> adapter_pb2.SkillInvocation:
    return adapter_pb2.SkillInvocation(
        skill_name=str(params["skill_name"]),
        parameters=_params_to_struct(params.get("parameters")),
        call_id=str(params.get("call_id", "")),
    )


def _to_cron_schedule_request(params: Mapping[str, Any]) -> adapter_pb2.CronScheduleRequest:
    return adapter_pb2.CronScheduleRequest(
        cron_id=str(params["cron_id"]),
        expression=str(params["expression"]),
        skill_name=str(params["skill_name"]),
        parameters=_params_to_struct(params.get("parameters")),
        description=str(params.get("description", "")),
    )


def _to_cron_cancel_request(params: Mapping[str, Any]) -> adapter_pb2.CronCancelRequest:
    return adapter_pb2.CronCancelRequest(cron_id=str(params["cron_id"]))


# ---------------------------------------------------------------------------
# Proto → dict helpers for return values
# ---------------------------------------------------------------------------


def _notify_result_to_dict(result: adapter_pb2.NotifyResult) -> dict[str, Any]:
    return {
        "delivered": bool(result.delivered),
        "message_id": result.message_id,
        "error": result.error,
    }


def _approval_result_to_dict(result: adapter_pb2.ApprovalResult) -> dict[str, Any]:
    outcome_name = adapter_pb2.ApprovalOutcome.Name(result.outcome)
    return {
        "outcome": outcome_name.removeprefix("APPROVAL_OUTCOME_").lower(),
        "approval_id": result.approval_id,
        "feedback": result.feedback,
        "responded_at": (
            result.responded_at.ToJsonString() if result.HasField("responded_at") else None
        ),
    }


def _skill_result_to_dict(result: adapter_pb2.SkillResult) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict  # noqa: PLC0415

    return {
        "call_id": result.call_id,
        "ok": bool(result.ok),
        "output": MessageToDict(result.output) if result.HasField("output") else {},
        "error": result.error,
        "completed_at": (
            result.completed_at.ToJsonString() if result.HasField("completed_at") else None
        ),
    }


def _cron_schedule_result_to_dict(result: adapter_pb2.CronScheduleResult) -> dict[str, Any]:
    return {
        "scheduled": bool(result.scheduled),
        "cron_id": result.cron_id,
        "error": result.error,
    }


def _cron_cancel_result_to_dict(result: adapter_pb2.CronCancelResult) -> dict[str, Any]:
    return {
        "cancelled": bool(result.cancelled),
        "error": result.error,
    }
