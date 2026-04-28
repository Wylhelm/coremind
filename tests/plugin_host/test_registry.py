"""Tests for the plugin registry (src/coremind/plugin_host/registry.py)."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from coremind.plugin_api._generated import plugin_pb2
from coremind.plugin_host.registry import PluginInfo, PluginRegistry, RegistrationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def public_key() -> Ed25519PublicKey:
    """Generate a fresh ed25519 public key for each test."""
    return Ed25519PrivateKey.generate().public_key()


def _make_manifest(
    plugin_id: str = "coremind.plugin.test",
    version: str = "1.0.0",
    display_name: str = "Test Plugin",
    kind: plugin_pb2.PluginKind = plugin_pb2.PLUGIN_KIND_SENSOR,
    provides_entities: list[str] | None = None,
    emits_attributes: list[str] | None = None,
) -> plugin_pb2.PluginManifest:
    """Build a valid PluginManifest for testing."""
    return plugin_pb2.PluginManifest(
        plugin_id=plugin_id,
        version=version,
        display_name=display_name,
        kind=kind,
        provides_entities=provides_entities or ["host"],
        emits_attributes=emits_attributes or ["cpu_percent"],
    )


@pytest.fixture()
def registry() -> PluginRegistry:
    """Return a fresh PluginRegistry instance."""
    return PluginRegistry()


# ---------------------------------------------------------------------------
# Registration: happy path
# ---------------------------------------------------------------------------


def test_register_valid_plugin(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """A plugin with a valid manifest is accepted and listed."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    assert registry.is_registered("coremind.plugin.test")


def test_list_plugins_returns_info(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """list_plugins returns a PluginInfo for every registered plugin."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    infos = registry.list_plugins()
    assert len(infos) == 1
    info = infos[0]
    assert isinstance(info, PluginInfo)
    assert info.plugin_id == "coremind.plugin.test"
    assert info.version == "1.0.0"
    assert info.health_state == "OK"
    assert info.event_count == 0


def test_get_info_returns_correct_plugin(
    registry: PluginRegistry, public_key: Ed25519PublicKey
) -> None:
    """get_info returns the matching PluginInfo for a known plugin_id."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    info = registry.get_info("coremind.plugin.test")
    assert info is not None
    assert info.display_name == "Test Plugin"


def test_get_info_returns_none_for_unknown(registry: PluginRegistry) -> None:
    """get_info returns None for unregistered plugin_id."""
    assert registry.get_info("coremind.plugin.missing") is None


# ---------------------------------------------------------------------------
# Registration: validation failures
# ---------------------------------------------------------------------------


def test_empty_plugin_id_rejected(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """A manifest with empty plugin_id raises RegistrationError."""
    manifest = _make_manifest(plugin_id="")
    with pytest.raises(RegistrationError, match="plugin_id"):
        registry.register(manifest, public_key)


def test_empty_display_name_rejected(
    registry: PluginRegistry, public_key: Ed25519PublicKey
) -> None:
    """A manifest with empty display_name raises RegistrationError."""
    manifest = _make_manifest(display_name="")
    with pytest.raises(RegistrationError, match="display_name"):
        registry.register(manifest, public_key)


@pytest.mark.parametrize(
    "version",
    ["1.0", "1.0.0.0", "v1.0.0", "latest", "", "1.0.0-alpha"],
)
def test_invalid_semver_rejected(
    registry: PluginRegistry, public_key: Ed25519PublicKey, version: str
) -> None:
    """A manifest with a non-semver version string raises RegistrationError."""
    manifest = _make_manifest(version=version)
    with pytest.raises(RegistrationError, match="semver"):
        registry.register(manifest, public_key)


def test_unspecified_kind_rejected(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """A manifest with PLUGIN_KIND_UNSPECIFIED raises RegistrationError."""
    manifest = _make_manifest(kind=plugin_pb2.PLUGIN_KIND_UNSPECIFIED)
    with pytest.raises(RegistrationError, match="PLUGIN_KIND_UNSPECIFIED"):
        registry.register(manifest, public_key)


# ---------------------------------------------------------------------------
# Duplicate registration
# ---------------------------------------------------------------------------


def test_duplicate_registration_rejected(
    registry: PluginRegistry, public_key: Ed25519PublicKey
) -> None:
    """Registering the same plugin_id twice raises RegistrationError."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    with pytest.raises(RegistrationError, match="already registered"):
        registry.register(manifest, public_key)


# ---------------------------------------------------------------------------
# Unregistration
# ---------------------------------------------------------------------------


def test_unregister_removes_plugin(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """After unregister, is_registered returns False."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    registry.unregister("coremind.plugin.test")
    assert not registry.is_registered("coremind.plugin.test")


def test_unregister_unknown_raises(registry: PluginRegistry) -> None:
    """Calling unregister for an unknown plugin_id raises RegistrationError."""
    with pytest.raises(RegistrationError, match="not registered"):
        registry.unregister("coremind.plugin.ghost")


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def test_resolve_key_returns_registered_key(
    registry: PluginRegistry, public_key: Ed25519PublicKey
) -> None:
    """resolve_key returns the exact key provided at registration time."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    resolved = registry.resolve_key("coremind.plugin.test")
    assert resolved is public_key


def test_resolve_key_returns_none_for_unknown(registry: PluginRegistry) -> None:
    """resolve_key returns None for unregistered plugin_id."""
    assert registry.resolve_key("coremind.plugin.unknown") is None


# ---------------------------------------------------------------------------
# Counters and health
# ---------------------------------------------------------------------------


def test_increment_event_count(registry: PluginRegistry, public_key: Ed25519PublicKey) -> None:
    """increment_event_count increases the event_count in PluginInfo."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    registry.increment_event_count("coremind.plugin.test")
    registry.increment_event_count("coremind.plugin.test")
    info = registry.get_info("coremind.plugin.test")
    assert info is not None
    assert info.event_count == 2


def test_increment_event_count_unknown_is_noop(registry: PluginRegistry) -> None:
    """increment_event_count for an unregistered plugin is a silent no-op."""
    registry.increment_event_count("coremind.plugin.ghost")  # must not raise


def test_update_health_changes_state(
    registry: PluginRegistry, public_key: Ed25519PublicKey
) -> None:
    """update_health persists the new health state string."""
    manifest = _make_manifest()
    registry.register(manifest, public_key)
    registry.update_health("coremind.plugin.test", "DEGRADED")
    info = registry.get_info("coremind.plugin.test")
    assert info is not None
    assert info.health_state == "DEGRADED"


# ---------------------------------------------------------------------------
# Max plugins cap
# ---------------------------------------------------------------------------


def test_max_plugins_cap_enforced() -> None:
    """Registering more than max_plugins plugins raises RegistrationError."""
    reg = PluginRegistry(max_plugins=2)
    for i in range(2):
        key = Ed25519PrivateKey.generate().public_key()
        reg.register(_make_manifest(plugin_id=f"coremind.plugin.test{i}"), key)

    extra_key = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(RegistrationError, match="limit"):
        reg.register(_make_manifest(plugin_id="coremind.plugin.overflow"), extra_key)


# ---------------------------------------------------------------------------
# List ordering
# ---------------------------------------------------------------------------


def test_list_plugins_sorted_by_id() -> None:
    """list_plugins returns plugins sorted alphabetically by plugin_id."""
    reg = PluginRegistry()
    for pid in ("coremind.plugin.zzz", "coremind.plugin.aaa", "coremind.plugin.mmm"):
        key = Ed25519PrivateKey.generate().public_key()
        reg.register(_make_manifest(plugin_id=pid), key)

    ids = [p.plugin_id for p in reg.list_plugins()]
    assert ids == sorted(ids)
