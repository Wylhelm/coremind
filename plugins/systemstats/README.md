# coremind-plugin-systemstats

A CoreMind sensor plugin that collects CPU usage, memory usage, and system uptime from the local host and forwards signed `WorldEvent`s to the CoreMind daemon every 30 seconds.

## Requirements

- Python 3.12+
- A running CoreMind daemon (`coremind daemon start`)
- `psutil` (installed as a dependency)

## Installation

```bash
cd plugins/systemstats
pip install -e .
```

Or install directly from the workspace root after `just setup`:

```bash
pip install -e plugins/systemstats
```

## Usage

Start the plugin in a second terminal while the daemon is running:

```bash
python -m coremind_plugin_systemstats
```

Or via the installed entry point:

```bash
coremind-plugin-systemstats
```

## Events emitted

Every 30 seconds the plugin emits three `WorldEvent`s on entity `{type: "host", id: "<hostname>"}`:

| Attribute        | Type  | Description                         |
|------------------|-------|-------------------------------------|
| `cpu_percent`    | float | CPU utilisation (0–100 %)           |
| `memory_percent` | float | Virtual memory utilisation (0–100 %) |
| `uptime_seconds` | int   | Seconds elapsed since last boot     |

## Key management

On first run the plugin generates an ed25519 keypair stored at:

```
~/.coremind/keys/plugins/coremind_plugin_systemstats.ed25519      (chmod 600)
~/.coremind/keys/plugins/coremind_plugin_systemstats.ed25519.pub
```

The daemon must have this public key loaded in its plugin registry before it can
accept the plugin's signed events.  In Phase 1 the daemon reads all keys from
`~/.coremind/keys/plugins/` on startup.

## Manifest

Static metadata is declared in `coremind_plugin_systemstats/manifest.toml` and
mirrors the fields in `PluginManifest` from the plugin protocol.
