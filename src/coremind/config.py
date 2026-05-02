"""Daemon configuration loading.

Configuration is resolved in this precedence order (highest first):

1. Environment variables (``COREMIND_*`` prefix).
2. ``~/.coremind/config.toml``.
3. Built-in defaults.
"""

from __future__ import annotations

import os
import tomllib
from datetime import time
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

_CONFIG_FILE = Path.home() / ".coremind" / "config.toml"


class IntentionConfig(BaseModel):
    """Configuration for the L5 intention loop."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    interval_seconds: int = Field(default=600, ge=10)
    max_questions: int = Field(default=5, ge=1)
    user_ask_classes: list[str] = Field(default_factory=list)
    min_salience: float = Field(default=0.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ActionConfig(BaseModel):
    """Configuration for the L6 action layer."""

    model_config = ConfigDict(frozen=True)

    suggest_grace_seconds: int = Field(default=30, ge=0)
    approval_ttl_seconds: int = Field(default=24 * 3600, ge=60)


class TelegramConfig(BaseModel):
    """Telegram notification adapter configuration."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    bot_token_secret: str = "telegram_bot_token"  # noqa: S105 — secret key name, not the secret
    chat_id: str = ""


class NotifyConfig(BaseModel):
    """Notification routing configuration."""

    model_config = ConfigDict(frozen=True)

    primary: str = "dashboard"
    fallbacks: list[str] = Field(default_factory=list)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class QuietHoursConfig(BaseModel):
    """Quiet-hours policy configuration."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    timezone: str = "UTC"
    quiet_start: time = Field(default=time(23, 0))
    quiet_end: time = Field(default=time(7, 0))


class DashboardConfig(BaseModel):
    """Configuration for the read-only web dashboard (Phase 4, Task 4.6).

    The dashboard binds to loopback by default.  ``/api/approvals`` requires
    a bearer token (loaded from ``api_token_secret``) and a request origin
    in :attr:`allowed_origins`; without them, approval submissions fail
    closed (the dashboard remains otherwise read-only).
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=9900, ge=1, le=65535)
    # Name of the secret holding the dashboard's bearer token.  Resolved by
    # the daemon's secrets resolver (file under ``~/.coremind/secrets/`` or
    # an environment override) at startup.
    api_token_secret: str = "dashboard_api_token"  # noqa: S105 — secret key name, not the secret
    operator_id: str = "operator"
    operator_display_name: str = "Operator"
    # Origins permitted on inbound approval requests.  Defaults to the
    # loopback origin matching the bind address; operators reverse-proxying
    # the dashboard on a different origin must list it explicitly.
    allowed_origins: tuple[str, ...] = ()


class LLMLayerConfig(BaseModel):
    """Per-layer LLM routing configuration."""

    model_config = ConfigDict(frozen=True)

    model: str = "ollama/mistral-large-3:675b-cloud"
    max_tokens: int = Field(default=2048, ge=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class EmbeddingLLMConfig(BaseModel):
    """Configuration for the embedding model.

    Attributes:
        model: Model name (e.g. ``nomic-embed-text``).
        provider: Provider name (e.g. ``ollama``).
        url: Base URL for the embedding provider API.
        dimension: Expected vector dimensionality.
    """

    model_config = ConfigDict(frozen=True)

    model: str = "nomic-embed-text"
    provider: str = "ollama"
    url: str = "http://10.0.0.175:11434"
    dimension: int = 768


class LLMConfig(BaseModel):
    """LLM routing configuration for all cognitive layers."""

    model_config = ConfigDict(frozen=True)

    intention: LLMLayerConfig = Field(default_factory=LLMLayerConfig)
    reasoning: LLMLayerConfig = Field(default_factory=LLMLayerConfig)
    reflection: LLMLayerConfig = Field(default_factory=LLMLayerConfig)
    embedding: EmbeddingLLMConfig = Field(default_factory=EmbeddingLLMConfig)


class DaemonConfig(BaseModel):
    """Validated daemon configuration.

    All fields carry sensible defaults so the daemon runs out-of-the-box
    against a locally started SurrealDB instance.
    """

    world_db_url: str = Field(default="ws://127.0.0.1:8000/rpc")
    world_db_username: str = Field(default="root")
    world_db_password: str = Field(default="root")
    plugin_socket: Path = Field(
        default_factory=lambda: Path.home() / ".coremind" / "run" / "plugin_host.sock"
    )
    max_plugins: int = Field(default=64, ge=1)
    intent_store_path: Path = Field(
        default_factory=lambda: Path.home() / ".coremind" / "intents.jsonl"
    )
    audit_log_path: Path = Field(default_factory=lambda: Path.home() / ".coremind" / "audit.log")
    intention: IntentionConfig = Field(default_factory=IntentionConfig)
    action: ActionConfig = Field(default_factory=ActionConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


def load_config() -> DaemonConfig:
    """Load and return the daemon configuration.

    Reads ``~/.coremind/config.toml`` when present. Individual keys are then
    overridden by ``COREMIND_*`` environment variables.

    Returns:
        A validated :class:`DaemonConfig` instance.
    """
    raw: dict[str, object] = {}

    if _CONFIG_FILE.exists():
        try:
            raw = tomllib.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            log.debug("config.loaded", path=str(_CONFIG_FILE))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("config.load_failed", path=str(_CONFIG_FILE), error=str(exc))

    _apply_env_overrides(raw)
    return DaemonConfig.model_validate(raw)


def _apply_env_overrides(raw: dict[str, object]) -> None:
    """Mutate *raw* with any ``COREMIND_*`` environment variable overrides.

    Args:
        raw: The mutable config dict to update in-place.
    """
    env_map: dict[str, str] = {
        "COREMIND_WORLD_DB_URL": "world_db_url",
        "COREMIND_WORLD_DB_USERNAME": "world_db_username",
        "COREMIND_WORLD_DB_PASSWORD": "world_db_password",
        "COREMIND_PLUGIN_SOCKET": "plugin_socket",
        "COREMIND_MAX_PLUGINS": "max_plugins",
    }
    for env_key, config_key in env_map.items():
        value = os.environ.get(env_key)
        if value is not None:
            raw[config_key] = value
