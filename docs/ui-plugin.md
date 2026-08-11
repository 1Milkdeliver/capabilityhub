# Local dashboard and Codex plugin

`capabilityhub.webui.DashboardServer` is a read-only standard-library HTTP server. It binds to `127.0.0.1` by default and serves only local bundled assets plus `GET /api/status`. The application injects a JSON-serializable snapshot callback; the UI never receives registry objects or full capability content through chat.

The snapshot may contain `providers`, `active_capabilities`, `loaded_capabilities`, `budget_remaining`, `reasoning_tier`, and `estimated_savings`. It must omit credentials, full manifests, scripts, and sensitive sections.

The repository-local Codex plugin provides one minimal dashboard-help skill. Its `.mcp.json` intentionally contains an empty `mcpServers` object: CapabilityHub does not yet expose a CLI command that can start the dashboard, so declaring one would be invalid. Wire a real, tested CLI command before adding an MCP server entry. The scaffold is not installed into a user profile.
