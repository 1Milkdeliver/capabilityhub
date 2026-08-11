# Local dashboard and Codex plugin

`capabilityhub.webui.DashboardServer` is a read-only standard-library HTTP server. It binds to `127.0.0.1` by default and serves only local bundled assets plus `GET /api/status`. The application injects a JSON-serializable snapshot callback; the UI never receives registry objects or full capability content through chat.

The built-in live snapshot contains five-kind active counts, generation and freshness,
inactive and safe exclusion counts, local wiring checks, and configuration-only
connection state. It does not dial providers and omits credentials,
commands, URLs, full manifests, Skill bodies, scripts, and provider output.

The repository-local Codex plugin provides two minimal menu skills. They can be
installed as an always-available help entry without loading the catalog into each chat.
Its `.mcp.json` intentionally stays empty: the Python package and its `mcp` extra are
separate runtime dependencies, and the public plugin must not embed one developer's
absolute executable path. After installing the package, register the local runtime
explicitly:

```bash
codex mcp add capabilityhub-local -- /absolute/path/to/python -m capabilityhub.cli mcp-serve
```

Open a new Codex task after registration. The local server builds a read-only catalog
generation from approved Skill roots, enabled plugin Skill roots, configured MCP names,
the installed CapabilityHub CLI, and project `.capabilityhub/manifests`; it never
executes discovered code or reports
credential/connection values. A missing runtime therefore cannot break every Codex
task, while connected tasks can query actual inventory totals. Search checks a compact
filesystem fingerprint and publishes a new atomic generation only after inputs change.
A 250 ms window coalesces burst requests, so same-task Skill and manifest updates remain
visible without a restart or repeated scans. The local Dashboard
uses the same monitor and polls its safe status endpoint every three seconds. Inventory responses
identify fresh, partial, or stale state and expose only safe diagnostic codes/counts.

The plugin exposes `helpme` and `myskills`, so the Codex app can present `/helpme` and
`/myskills`. It does not override built-in `/help`, `/skills`, `/status`, or `/mcp`
commands. Bare `/helpme` keeps Inventory, Providers, Routing, Lifecycle, and MCP visible
with plain parenthesized explanations. Bare `/myskills` goes directly to Skill search,
inventory, loaded state, details, providers, routing, lifecycle, risks, and conflicts.
Both menus accept visible numbers, exact commands, and explicit natural-language intent.
Language settings are directly visible on the root menu. Every child menu and result
shows localized back and home controls: `/helpme back`, `/helpme home`, and, inside the
Skill area, `/myskills back`.

Stable UI text comes from bundled `zh-CN` and `en` JSON message catalogs. Language
selection follows explicit command, task, project, global, then system preference, with
English as the final fallback. Model translation is reserved for third-party prose that
the user explicitly asks to translate; it is never written back into the catalogs
automatically. Detailed state remains in the dashboard, outside the conversation
context. A newly installed or updated plugin may require opening a new Codex task before
its slash-menu entry appears.
