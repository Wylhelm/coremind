import datetime

from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
import plugin_pb2 as _plugin_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class HealthState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HEALTH_STATE_UNSPECIFIED: _ClassVar[HealthState]
    HEALTH_STATE_OK: _ClassVar[HealthState]
    HEALTH_STATE_DEGRADED: _ClassVar[HealthState]
    HEALTH_STATE_UNHEALTHY: _ClassVar[HealthState]

class ApprovalOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    APPROVAL_OUTCOME_UNSPECIFIED: _ClassVar[ApprovalOutcome]
    APPROVAL_OUTCOME_APPROVED: _ClassVar[ApprovalOutcome]
    APPROVAL_OUTCOME_DENIED: _ClassVar[ApprovalOutcome]
    APPROVAL_OUTCOME_TIMEOUT: _ClassVar[ApprovalOutcome]
    APPROVAL_OUTCOME_CANCELLED: _ClassVar[ApprovalOutcome]
HEALTH_STATE_UNSPECIFIED: HealthState
HEALTH_STATE_OK: HealthState
HEALTH_STATE_DEGRADED: HealthState
HEALTH_STATE_UNHEALTHY: HealthState
APPROVAL_OUTCOME_UNSPECIFIED: ApprovalOutcome
APPROVAL_OUTCOME_APPROVED: ApprovalOutcome
APPROVAL_OUTCOME_DENIED: ApprovalOutcome
APPROVAL_OUTCOME_TIMEOUT: ApprovalOutcome
APPROVAL_OUTCOME_CANCELLED: ApprovalOutcome

class Health(_message.Message):
    __slots__ = ("state", "message", "as_of", "events_processed", "actions_dispatched")
    STATE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AS_OF_FIELD_NUMBER: _ClassVar[int]
    EVENTS_PROCESSED_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_DISPATCHED_FIELD_NUMBER: _ClassVar[int]
    state: HealthState
    message: str
    as_of: _timestamp_pb2.Timestamp
    events_processed: int
    actions_dispatched: int
    def __init__(self, state: _Optional[_Union[HealthState, str]] = ..., message: _Optional[str] = ..., as_of: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., events_processed: _Optional[int] = ..., actions_dispatched: _Optional[int] = ...) -> None: ...

class NotifyRequest(_message.Message):
    __slots__ = ("channel", "target", "text", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    channel: str
    target: str
    text: str
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, channel: _Optional[str] = ..., target: _Optional[str] = ..., text: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class NotifyResult(_message.Message):
    __slots__ = ("delivered", "message_id", "error")
    DELIVERED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    delivered: bool
    message_id: str
    error: str
    def __init__(self, delivered: bool = ..., message_id: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class ApprovalRequest(_message.Message):
    __slots__ = ("approval_id", "channel", "target", "prompt", "context", "timeout_seconds")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    channel: str
    target: str
    prompt: str
    context: _struct_pb2.Struct
    timeout_seconds: int
    def __init__(self, approval_id: _Optional[str] = ..., channel: _Optional[str] = ..., target: _Optional[str] = ..., prompt: _Optional[str] = ..., context: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., timeout_seconds: _Optional[int] = ...) -> None: ...

class ApprovalResult(_message.Message):
    __slots__ = ("outcome", "approval_id", "feedback", "responded_at")
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    RESPONDED_AT_FIELD_NUMBER: _ClassVar[int]
    outcome: ApprovalOutcome
    approval_id: str
    feedback: str
    responded_at: _timestamp_pb2.Timestamp
    def __init__(self, outcome: _Optional[_Union[ApprovalOutcome, str]] = ..., approval_id: _Optional[str] = ..., feedback: _Optional[str] = ..., responded_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SkillInvocation(_message.Message):
    __slots__ = ("skill_name", "parameters", "call_id")
    SKILL_NAME_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    skill_name: str
    parameters: _struct_pb2.Struct
    call_id: str
    def __init__(self, skill_name: _Optional[str] = ..., parameters: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., call_id: _Optional[str] = ...) -> None: ...

class SkillResult(_message.Message):
    __slots__ = ("call_id", "ok", "output", "error", "completed_at")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    ok: bool
    output: _struct_pb2.Struct
    error: str
    completed_at: _timestamp_pb2.Timestamp
    def __init__(self, call_id: _Optional[str] = ..., ok: bool = ..., output: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error: _Optional[str] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CronScheduleRequest(_message.Message):
    __slots__ = ("cron_id", "expression", "skill_name", "parameters", "description")
    CRON_ID_FIELD_NUMBER: _ClassVar[int]
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    SKILL_NAME_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    cron_id: str
    expression: str
    skill_name: str
    parameters: _struct_pb2.Struct
    description: str
    def __init__(self, cron_id: _Optional[str] = ..., expression: _Optional[str] = ..., skill_name: _Optional[str] = ..., parameters: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., description: _Optional[str] = ...) -> None: ...

class CronScheduleResult(_message.Message):
    __slots__ = ("scheduled", "cron_id", "error")
    SCHEDULED_FIELD_NUMBER: _ClassVar[int]
    CRON_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    scheduled: bool
    cron_id: str
    error: str
    def __init__(self, scheduled: bool = ..., cron_id: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class CronCancelRequest(_message.Message):
    __slots__ = ("cron_id",)
    CRON_ID_FIELD_NUMBER: _ClassVar[int]
    cron_id: str
    def __init__(self, cron_id: _Optional[str] = ...) -> None: ...

class CronCancelResult(_message.Message):
    __slots__ = ("cancelled", "error")
    CANCELLED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    cancelled: bool
    error: str
    def __init__(self, cancelled: bool = ..., error: _Optional[str] = ...) -> None: ...

class Mem0QueryRequest(_message.Message):
    __slots__ = ("query", "top_k", "filters")
    class FiltersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    FILTERS_FIELD_NUMBER: _ClassVar[int]
    query: str
    top_k: int
    filters: _containers.ScalarMap[str, str]
    def __init__(self, query: _Optional[str] = ..., top_k: _Optional[int] = ..., filters: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Mem0QueryResult(_message.Message):
    __slots__ = ("records",)
    RECORDS_FIELD_NUMBER: _ClassVar[int]
    records: _containers.RepeatedCompositeFieldContainer[Mem0Record]
    def __init__(self, records: _Optional[_Iterable[_Union[Mem0Record, _Mapping]]] = ...) -> None: ...

class Mem0StoreRequest(_message.Message):
    __slots__ = ("content", "metadata", "record_id")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    RECORD_ID_FIELD_NUMBER: _ClassVar[int]
    content: str
    metadata: _containers.ScalarMap[str, str]
    record_id: str
    def __init__(self, content: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ..., record_id: _Optional[str] = ...) -> None: ...

class Mem0StoreResult(_message.Message):
    __slots__ = ("record_id", "ok", "error")
    RECORD_ID_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    record_id: str
    ok: bool
    error: str
    def __init__(self, record_id: _Optional[str] = ..., ok: bool = ..., error: _Optional[str] = ...) -> None: ...

class Mem0Record(_message.Message):
    __slots__ = ("record_id", "content", "score", "metadata", "stored_at")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    RECORD_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    STORED_AT_FIELD_NUMBER: _ClassVar[int]
    record_id: str
    content: str
    score: float
    metadata: _containers.ScalarMap[str, str]
    stored_at: _timestamp_pb2.Timestamp
    def __init__(self, record_id: _Optional[str] = ..., content: _Optional[str] = ..., score: _Optional[float] = ..., metadata: _Optional[_Mapping[str, str]] = ..., stored_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ChannelList(_message.Message):
    __slots__ = ("channels",)
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    channels: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, channels: _Optional[_Iterable[str]] = ...) -> None: ...

class SkillList(_message.Message):
    __slots__ = ("skills",)
    SKILLS_FIELD_NUMBER: _ClassVar[int]
    skills: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, skills: _Optional[_Iterable[str]] = ...) -> None: ...
