# coremind-plugin-homeassistant

Home Assistant integration for CoreMind.

Connects to a Home Assistant instance via its WebSocket API, subscribes to
state change events, and emits signed `WorldEvent`s for motion, temperature,
humidity, light states, and anything matching the configured entity prefixes.

## Configuration

See [`coremind_plugin_homeassistant/config.toml`](./coremind_plugin_homeassistant/config.toml).

```toml
[homeassistant]
base_url = "http://localhost:8123"
access_token_ref = "secrets:ha_token"
entity_prefixes = ["sensor.", "light.", "binary_sensor."]
```

The access token is resolved via the CoreMind secrets store (`~/.coremind/secrets/`).
