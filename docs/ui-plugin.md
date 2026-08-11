# Local dashboard and Codex plugin

`capabilityhub.webui.DashboardServer` is a read-only standard-library HTTP server. It binds to `127.0.0.1` by default and serves only local bundled assets plus `GET /api/status`. The application injects a JSON-serializable snapshot callback; the UI never receives registry objects or full capability content through chat.

The snapshot may contain `providers`, `active_capabilities`, `loaded_capabilities`, `budget_remaining`, `reasoning_tier`, and `estimated_savings`. It must omit credentials, full manifests, scripts, and sensitive sections.

The repository-local Codex plugin provides one minimal dashboard-help skill. It can be
installed as an always-available help entry without loading the catalog into each chat.
Its `.mcp.json` intentionally stays empty: the Python package and its `mcp` extra are
separate runtime dependencies, and a plugin must not pretend that an uninstalled
`capabilityhub` executable exists. After installing the package, an operator may
configure `capabilityhub mcp-serve` explicitly. The plugin is therefore useful even
when the MCP runtime is disabled, and a missing runtime cannot break every Codex task.

Codex owns built-in slash commands, so this plugin does not override the global
`/help` command. Invoke the `capabilityhub-dashboard` skill by name or ask to open the
CapabilityHub dashboard. This keeps the detailed UI outside the conversation context.
