"""Process entry point for the CoreMind-side OpenClaw adapter plugin.

Wires together all components:

* Loads the runtime permission scope from the plugin's config TOML.
* Starts the :class:`CoreMindHalfServer` on its own Unix socket so the
  OpenClaw extension can push events.
* Starts a :class:`CoreMindPluginServicer` gRPC server so the CoreMind daemon
  can call Identify / Start / Stop / HealthCheck / InvokeAction.
* Opens an :class:`OpenClawGrpcClient` so outbound actions can reach OpenClaw.
* Maintains a :class:`DaemonForwarder` to the CoreMind daemon's CoreMindHost
  socket with an on-disk buffer for degraded mode.

The process runs until SIGTERM or a Stop RPC is received.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tomllib
from dataclasses import dataclass
from pathlib import Path

import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coremind.crypto.signatures import ensure_plugin_keypair
from coremind.plugin_api._generated import plugin_pb2_grpc
from coremind_plugin_openclaw.action_dispatcher import ActionDispatcher, PermissionScope
from coremind_plugin_openclaw.openclaw_client import OpenClawGrpcClient
from coremind_plugin_openclaw.plugin_side import CoreMindPluginServicer
from coremind_plugin_openclaw.server import CoreMindHalfServer, DaemonForwarder

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.openclaw_adapter"
KEY_STORE_ID: str = "coremind_plugin_openclaw_adapter"
DEFAULT_CONFIG_PATH: Path = Path.home() / ".coremind" / "plugins" / "openclaw_adapter.toml"
_RUN_DIR: Path = Path.home() / ".coremind" / "run"
DEFAULT_DAEMON_SOCKET: Path = _RUN_DIR / "plugin_host.sock"
# Default sockets live under the user's private run directory (chmod 700 at
# startup). Avoid world-writable /tmp paths: another local user could
# bind-squat the path while the adapter is down or observe that it is up.
DEFAULT_COREMIND_HALF_ADDR: str = f"unix://{_RUN_DIR / 'openclaw-half.sock'}"
DEFAULT_PLUGIN_GRPC_ADDR: str = f"unix://{_RUN_DIR / 'openclaw-plugin.sock'}"
DEFAULT_OPENCLAW_ADDR: str = f"unix://{_RUN_DIR / 'openclaw-adapter.sock'}"
_RUN_DIR_MODE: int = 0o700


@dataclass(frozen=True)
class AdapterConfig:
    """Runtime configuration loaded from the adapter's TOML file."""

    daemon_socket: Path = DEFAULT_DAEMON_SOCKET
    coremind_half_address: str = DEFAULT_COREMIND_HALF_ADDR
    plugin_grpc_address: str = DEFAULT_PLUGIN_GRPC_ADDR
    openclaw_address: str = DEFAULT_OPENCLAW_ADDR
    allowed_channels: tuple[str, ...] = ("*",)
    allowed_skills: tuple[str, ...] = ("*",)
    cron_manage: bool = True

    @property
    def scope(self) -> PermissionScope:
        return PermissionScope(
            allowed_channels=self.allowed_channels,
            allowed_skills=self.allowed_skills,
            cron_manage=self.cron_manage,
        )

    @property
    def scoped_permissions(self) -> list[str]:
        perms: list[str] = ["network:local"]
        for pat in self.allowed_channels:
            perms.append(f"openclaw:channels:{pat}")
        for pat in self.allowed_skills:
            perms.append(f"openclaw:skills:{pat}")
        if self.cron_manage:
            perms.append("openclaw:cron:manage")
        return perms


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AdapterConfig:
    """Load :class:`AdapterConfig` from *path*, returning defaults when absent."""
    if not path.exists():
        log.info("openclaw_adapter.config_default", reason="no config file", path=str(path))
        return AdapterConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return AdapterConfig(
        daemon_socket=Path(data.get("daemon_socket", str(DEFAULT_DAEMON_SOCKET))).expanduser(),
        coremind_half_address=str(data.get("coremind_half_address", DEFAULT_COREMIND_HALF_ADDR)),
        plugin_grpc_address=str(data.get("plugin_grpc_address", DEFAULT_PLUGIN_GRPC_ADDR)),
        openclaw_address=str(data.get("openclaw_address", DEFAULT_OPENCLAW_ADDR)),
        allowed_channels=tuple(data.get("allowed_channels", ["*"])),
        allowed_skills=tuple(data.get("allowed_skills", ["*"])),
        cron_manage=bool(data.get("cron_manage", True)),
    )


def _ensure_private_run_dir(path: Path) -> None:
    """Create *path* (and parents) with 0o700 perms if it does not exist."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_RUN_DIR_MODE)


def _schemas_dir() -> Path:
    """Return the directory holding operation JSON Schemas."""
    return Path(__file__).parent / "schemas"


async def run(config: AdapterConfig, private_key: Ed25519PrivateKey) -> None:
    """Start all sub-servers and run until signalled."""
    # Private run directory for Unix sockets (chmod 700).
    _ensure_private_run_dir(_RUN_DIR)

    # --- Outbound client to OpenClaw side ---
    openclaw_client = OpenClawGrpcClient(config.openclaw_address)

    # --- Daemon forwarder ---
    # Offline queueing lives on the OpenClaw-side producer; see server.py.
    forwarder = DaemonForwarder(
        host_socket=config.daemon_socket,
        plugin_id=PLUGIN_ID,
    )

    # --- CoreMindHalf: receives events from OpenClaw extension ---
    coremind_half = CoreMindHalfServer(
        config.coremind_half_address,
        plugin_public_key=private_key.public_key(),
        plugin_id=PLUGIN_ID,
        forwarder=forwarder,
    )

    # --- CoreMindPlugin: daemon-facing lifecycle/action server ---
    dispatcher = ActionDispatcher(
        client=openclaw_client,
        scope=config.scope,
        schema_dir=_schemas_dir(),
    )
    plugin_servicer = CoreMindPluginServicer(
        dispatcher=dispatcher,
        scoped_permissions=config.scoped_permissions,
    )
    plugin_server = grpc.aio.server()
    plugin_pb2_grpc.add_CoreMindPluginServicer_to_server(plugin_servicer, plugin_server)  # type: ignore[no-untyped-call]
    if config.plugin_grpc_address.startswith("unix://"):
        sock_path = Path(config.plugin_grpc_address.removeprefix("unix://")).expanduser()
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():  # noqa: ASYNC240 — startup-only, local FS
            sock_path.unlink()  # noqa: ASYNC240 — startup-only, local FS
    plugin_server.add_insecure_port(config.plugin_grpc_address)
    await plugin_server.start()
    log.info("openclaw_adapter.plugin_rpc_started", address=config.plugin_grpc_address)

    await coremind_half.start()

    # Opportunistic connection attempts; failures are not fatal.
    try:
        await forwarder.connect()
    except (OSError, grpc.RpcError):
        log.warning("openclaw_adapter.daemon_initial_connect_failed")
    try:
        await openclaw_client.connect()
    except (OSError, grpc.RpcError):
        log.warning("openclaw_adapter.openclaw_initial_connect_failed")

    # --- Signal handling ---
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal(signum: int) -> None:
        log.info("openclaw_adapter.signal", signum=signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, int(sig))

    log.info("openclaw_adapter.ready", plugin_id=PLUGIN_ID, pid=os.getpid())
    await stop_event.wait()

    # --- Shutdown ---
    log.info("openclaw_adapter.shutting_down")
    await coremind_half.stop()
    await plugin_server.stop(5.0)
    await openclaw_client.close()
    await forwarder.close()
    log.info("openclaw_adapter.stopped")


def main() -> None:
    """Process entry point."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
    )
    config = load_config()
    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    asyncio.run(run(config, private_key))
