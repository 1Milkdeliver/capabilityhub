---
name: capabilityhub-dashboard
description: Help open and interpret the local read-only CapabilityHub dashboard.
license: MIT
---

# CapabilityHub dashboard

Use this skill only when a user asks for CapabilityHub help or the local dashboard.

1. Explain that the dashboard is local and read-only.
2. Start it with `capabilityhub dashboard` when no dashboard URL is already available.
3. Open the reported `http://127.0.0.1:<port>` URL.
4. Use the plugin's three MCP operations only when the user explicitly asks to search,
   load, or execute a capability.

Do not discover, preload, execute, or describe capabilities unless the user explicitly requests them through the running application.
