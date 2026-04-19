import datetime

from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PluginKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_KIND_UNSPECIFIED: _ClassVar[PluginKind]
    PLUGIN_KIND_SENSOR: _ClassVar[PluginKind]
    PLUGIN_KIND_EFFECTOR: _ClassVar[PluginKind]
    PLUGIN_KIND_BIDIRECTIONAL: _ClassVar[PluginKind]
    PLUGIN_KIND_MODEL_BACKEND: _ClassVar[PluginKind]

class TrendDirection(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TREND_DIRECTION_UNSPECIFIED: _ClassVar[TrendDirection]
    TREND_DIRECTION_RISING: _ClassVar[TrendDirection]
    TREND_DIRECTION_FALLING: _ClassVar[TrendDirection]
    TREND_DIRECTION_STABLE: _ClassVar[TrendDirection]
    TREND_DIRECTION_VOLATILE: _ClassVar[TrendDirection]

class HealthState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HEALTH_STATE_UNSPECIFIED: _ClassVar[HealthState]
    HEALTH_STATE_OK: _ClassVar[HealthState]
    HEALTH_STATE_DEGRADED: _ClassVar[HealthState]
    HEALTH_STATE_UNHEALTHY: _ClassVar[HealthState]

class ActionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTION_STATUS_UNSPECIFIED: _ClassVar[ActionStatus]
    ACTION_STATUS_OK: _ClassVar[ActionStatus]
    ACTION_STATUS_NOOP: _ClassVar[ActionStatus]
    ACTION_STATUS_TRANSIENT_FAILURE: _ClassVar[ActionStatus]
    ACTION_STATUS_PERMANENT_FAILURE: _ClassVar[ActionStatus]
    ACTION_STATUS_REJECTED_INVALID_SIGNATURE: _ClassVar[ActionStatus]

class LogLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LOG_LEVEL_UNSPECIFIED: _ClassVar[LogLevel]
    LOG_LEVEL_DEBUG: _ClassVar[LogLevel]
    LOG_LEVEL_INFO: _ClassVar[LogLevel]
    LOG_LEVEL_WARNING: _ClassVar[LogLevel]
    LOG_LEVEL_ERROR: _ClassVar[LogLevel]
    LOG_LEVEL_CRITICAL: _ClassVar[LogLevel]
PLUGIN_KIND_UNSPECIFIED: PluginKind
PLUGIN_KIND_SENSOR: PluginKind
PLUGIN_KIND_EFFECTOR: PluginKind
PLUGIN_KIND_BIDIRECTIONAL: PluginKind
PLUGIN_KIND_MODEL_BACKEND: PluginKind
TREND_DIRECTION_UNSPECIFIED: TrendDirection
TREND_DIRECTION_RISING: TrendDirection
TREND_DIRECTION_FALLING: TrendDirection
TREND_DIRECTION_STABLE: TrendDirection
TREND_DIRECTION_VOLATILE: TrendDirection
HEALTH_STATE_UNSPECIFIED: HealthState
HEALTH_STATE_OK: HealthState
HEALTH_STATE_DEGRADED: HealthState
HEALTH_STATE_UNHEALTHY: HealthState
ACTION_STATUS_UNSPECIFIED: ActionStatus
ACTION_STATUS_OK: ActionStatus
ACTION_STATUS_NOOP: ActionStatus
ACTION_STATUS_TRANSIENT_FAILURE: ActionStatus
ACTION_STATUS_PERMANENT_FAILURE: ActionStatus
ACTION_STATUS_REJECTED_INVALID_SIGNATURE: ActionStatus
LOG_LEVEL_UNSPECIFIED: LogLevel
LOG_LEVEL_DEBUG: LogLevel
LOG_LEVEL_INFO: LogLevel
LOG_LEVEL_WARNING: LogLevel
LOG_LEVEL_ERROR: LogLevel
LOG_LEVEL_CRITICAL: LogLevel

class PluginManifest(_message.Message):
    __slots__ = ("plugin_id", "version", "display_name", "kind", "provides_entities", "emits_attributes", "accepts_operations", "required_permissions", "license", "author", "min_daemon_version")
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PROVIDES_ENTITIES_FIELD_NUMBER: _ClassVar[int]
    EMITS_ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    ACCEPTS_OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    MIN_DAEMON_VERSION_FIELD_NUMBER: _ClassVar[int]
    plugin_id: str
    version: str
    display_name: str
    kind: PluginKind
    provides_entities: _containers.RepeatedScalarFieldContainer[str]
    emits_attributes: _containers.RepeatedScalarFieldContainer[str]
    accepts_operations: _containers.RepeatedScalarFieldContainer[str]
    required_permissions: _containers.RepeatedScalarFieldContainer[str]
    license: str
    author: str
    min_daemon_version: str
    def __init__(self, plugin_id: _Optional[str] = ..., version: _Optional[str] = ..., display_name: _Optional[str] = ..., kind: _Optional[_Union[PluginKind, str]] = ..., provides_entities: _Optional[_Iterable[str]] = ..., emits_attributes: _Optional[_Iterable[str]] = ..., accepts_operations: _Optional[_Iterable[str]] = ..., required_permissions: _Optional[_Iterable[str]] = ..., license: _Optional[str] = ..., author: _Optional[str] = ..., min_daemon_version: _Optional[str] = ...) -> None: ...

class PluginConfig(_message.Message):
    __slots__ = ("config_toml", "host_address", "log_level")
    CONFIG_TOML_FIELD_NUMBER: _ClassVar[int]
    HOST_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    LOG_LEVEL_FIELD_NUMBER: _ClassVar[int]
    config_toml: str
    host_address: str
    log_level: LogLevel
    def __init__(self, config_toml: _Optional[str] = ..., host_address: _Optional[str] = ..., log_level: _Optional[_Union[LogLevel, str]] = ...) -> None: ...

class WorldEvent(_message.Message):
    __slots__ = ("id", "timestamp", "source", "source_version", "signature", "entity", "attribute", "value", "unit", "delta", "confidence", "context")
    ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VERSION_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    id: str
    timestamp: _timestamp_pb2.Timestamp
    source: str
    source_version: str
    signature: bytes
    entity: EntityRef
    attribute: str
    value: _struct_pb2.Value
    unit: str
    delta: Delta
    confidence: float
    context: EventContext
    def __init__(self, id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., source: _Optional[str] = ..., source_version: _Optional[str] = ..., signature: _Optional[bytes] = ..., entity: _Optional[_Union[EntityRef, _Mapping]] = ..., attribute: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., unit: _Optional[str] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ..., confidence: _Optional[float] = ..., context: _Optional[_Union[EventContext, _Mapping]] = ...) -> None: ...

class EntityRef(_message.Message):
    __slots__ = ("type", "entity_id")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_ID_FIELD_NUMBER: _ClassVar[int]
    type: str
    entity_id: str
    def __init__(self, type: _Optional[str] = ..., entity_id: _Optional[str] = ...) -> None: ...

class Delta(_message.Message):
    __slots__ = ("absolute", "relative_pct", "previous_value")
    ABSOLUTE_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_PCT_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_VALUE_FIELD_NUMBER: _ClassVar[int]
    absolute: float
    relative_pct: float
    previous_value: _struct_pb2.Value
    def __init__(self, absolute: _Optional[float] = ..., relative_pct: _Optional[float] = ..., previous_value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...

class EventContext(_message.Message):
    __slots__ = ("trend_window", "trend_direction", "related_entities", "tags")
    TREND_WINDOW_FIELD_NUMBER: _ClassVar[int]
    TREND_DIRECTION_FIELD_NUMBER: _ClassVar[int]
    RELATED_ENTITIES_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    trend_window: str
    trend_direction: TrendDirection
    related_entities: _containers.RepeatedCompositeFieldContainer[EntityRef]
    tags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, trend_window: _Optional[str] = ..., trend_direction: _Optional[_Union[TrendDirection, str]] = ..., related_entities: _Optional[_Iterable[_Union[EntityRef, _Mapping]]] = ..., tags: _Optional[_Iterable[str]] = ...) -> None: ...

class HealthStatus(_message.Message):
    __slots__ = ("state", "message", "last_event_at", "events_emitted", "actions_attempted", "actions_failed")
    STATE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_AT_FIELD_NUMBER: _ClassVar[int]
    EVENTS_EMITTED_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_ATTEMPTED_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FAILED_FIELD_NUMBER: _ClassVar[int]
    state: HealthState
    message: str
    last_event_at: _timestamp_pb2.Timestamp
    events_emitted: int
    actions_attempted: int
    actions_failed: int
    def __init__(self, state: _Optional[_Union[HealthState, str]] = ..., message: _Optional[str] = ..., last_event_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., events_emitted: _Optional[int] = ..., actions_attempted: _Optional[int] = ..., actions_failed: _Optional[int] = ...) -> None: ...

class ActionRequest(_message.Message):
    __slots__ = ("action_id", "intent_id", "dispatched_at", "operation", "parameters", "daemon_signature", "intent_confidence")
    ACTION_ID_FIELD_NUMBER: _ClassVar[int]
    INTENT_ID_FIELD_NUMBER: _ClassVar[int]
    DISPATCHED_AT_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    DAEMON_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    INTENT_CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    action_id: str
    intent_id: str
    dispatched_at: _timestamp_pb2.Timestamp
    operation: str
    parameters: _struct_pb2.Struct
    daemon_signature: bytes
    intent_confidence: float
    def __init__(self, action_id: _Optional[str] = ..., intent_id: _Optional[str] = ..., dispatched_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., operation: _Optional[str] = ..., parameters: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., daemon_signature: _Optional[bytes] = ..., intent_confidence: _Optional[float] = ...) -> None: ...

class ActionResult(_message.Message):
    __slots__ = ("action_id", "status", "message", "output", "completed_at", "reversed_by_operation", "reversal_parameters")
    ACTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    REVERSED_BY_OPERATION_FIELD_NUMBER: _ClassVar[int]
    REVERSAL_PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    action_id: str
    status: ActionStatus
    message: str
    output: _struct_pb2.Struct
    completed_at: _timestamp_pb2.Timestamp
    reversed_by_operation: str
    reversal_parameters: _struct_pb2.Struct
    def __init__(self, action_id: _Optional[str] = ..., status: _Optional[_Union[ActionStatus, str]] = ..., message: _Optional[str] = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., reversed_by_operation: _Optional[str] = ..., reversal_parameters: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SecretRequest(_message.Message):
    __slots__ = ("secret_name",)
    SECRET_NAME_FIELD_NUMBER: _ClassVar[int]
    secret_name: str
    def __init__(self, secret_name: _Optional[str] = ...) -> None: ...

class SecretResponse(_message.Message):
    __slots__ = ("secret_value",)
    SECRET_VALUE_FIELD_NUMBER: _ClassVar[int]
    secret_value: str
    def __init__(self, secret_value: _Optional[str] = ...) -> None: ...

class LogEntry(_message.Message):
    __slots__ = ("timestamp", "level", "message", "fields")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    timestamp: _timestamp_pb2.Timestamp
    level: LogLevel
    message: str
    fields: _struct_pb2.Struct
    def __init__(self, timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., level: _Optional[_Union[LogLevel, str]] = ..., message: _Optional[str] = ..., fields: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
