# Local dashboard and Codex plugin

`capabilityhub.webui.DashboardServer` is a loopback-only standard-library HTTP server. It serves local bundled assets, safe status, and optional callbacks for bounded metadata search, project language, activation lifecycle, approval decisions, and Context metadata. The application injects each callback; the UI never receives registry objects or full capability content through chat. Mutation requests require the random token returned to same-origin JavaScript plus an origin check, accept at most 16 KiB JSON, and are disabled when their callback is absent.

The built-in live snapshot contains five-kind active counts, generation and freshness,
inactive and safe exclusion counts, local wiring checks, and configuration-only
connection state. It does not dial providers and omits credentials,
commands, URLs, full manifests, Skill bodies, scripts, and provider output.
Search returns no more than ten compact cards and never loads a capability body. Dashboard
lifecycle supports only enabled, disabled, and quarantined catalog states. Approval controls
can approve or deny an already-created exact-intent request; Context controls can pin, unpin,
or forget residency metadata. The page cannot delete source files, install, update, run a
provider, create arbitrary approvals, or change provider credentials.
It also displays staged update pointers/pins and whether the optional secure audit key
setting is present, without exposing the key or adding browser-side update actions.
The page also shows the latest ten redacted project audit events. It omits arguments,
credentials, provider output, raw task identifiers, and absolute audit paths.

The repository-local Codex plugin provides two minimal menu skills. They can be
installed as an always-available help entry without loading the catalog into each chat.
Its `.mcp.json` starts the dependency-free Node stdio runtime bundled in the plugin, so
the menu does not depend on a global `capsift` executable or a separate MCP
registration. Install the Python package only when the full CLI, Dashboard, configured
Providers, or remote reference service is required:

```bash
python -m pip install '.[mcp]'
```

The plugin supplies the `capsift-local` stdio server automatically, so no separate
`codex mcp add` step is needed. Open a new Codex task after install or update. The bundled
server reports only the packaged menu Skills and its own MCP surface; it does not pretend
to be the host's full five-kind catalog. The optional Python runtime builds the complete
read-only catalog generation from approved Skill roots, enabled plugin Skill roots,
configured MCP names, the installed CapSift CLI, and project manifests. It never
executes discovered code or reports credential/connection values. Its search checks a
compact filesystem fingerprint and publishes a new atomic generation only after inputs
change. A 250 ms window coalesces burst requests, so same-task Skill and manifest updates
remain visible without a restart or repeated scans. The local Dashboard uses that same
monitor and polls its safe status endpoint every three seconds. Inventory responses
identify fresh, partial, or stale state and expose only safe diagnostic codes/counts.

For installation and daily workflows, see the
[Simplified Chinese user guide](user-guide-zh-CN.md).

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
