# /add-plugin — Scaffold a new CoreMind plugin

Act as the **Integrator** (see `/integrator`).

Scaffold a new plugin following the project's plugin skeleton. Before writing code, confirm the following:

- Plugin name and purpose
- Sensor, effector, or bidirectional
- External system being integrated (name + version range)
- Entity types the plugin will produce
- Attributes it will emit
- Operations it will accept (if effector)
- Declared permissions
- Secrets it requires

Ask the user if any of these are unclear.

Then:
1. Create the directory structure under `plugins/<name>/`.
2. Write a valid `manifest.toml` with all required fields.
3. Write a `main.py` that connects to the daemon via the gRPC contract.
4. Write a `collector.py` that implements the external-system integration.
5. Write translators in `translators.py` with **pure functions**.
6. Write tests covering:
   - Translators (with fixtures)
   - Manifest validation
   - Error paths (external system down, bad input)
7. Update `docs/INTEGRATIONS.md` with the new plugin.
8. Run `just lint && just test`.

Honor every plugin invariant from the `/integrator` command:
- Signed events, declared permissions, bounded buffers, graceful degradation, reversibility, no credentials in events.
