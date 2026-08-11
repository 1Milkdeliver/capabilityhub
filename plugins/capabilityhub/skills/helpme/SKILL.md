---
name: helpme
description: Open CapabilityHub help and its local read-only dashboard. Trigger when the user enters /helpme or explicitly asks for CapabilityHub help, status, configuration, budgets, providers, or dashboard access.
license: MIT
---

# CapabilityHub `/helpme`

When triggered by `/helpme`, return a compact CapabilityHub help menu without loading
the registered capability catalog into the conversation.

1. Offer `dashboard`, `status`, `budgets`, `providers`, and `MCP setup` as help topics.
2. Explain that the dashboard is local and read-only.
3. Start it with `capabilityhub dashboard` only when the user asks to open it and no
   dashboard URL is already available.
4. Open the reported `http://127.0.0.1:<port>` URL.
5. If MCP access is requested, explain that the Python package must first be installed
   with its `mcp` extra and configured separately; the help plugin does not silently
   install or launch a runtime.

Do not discover, preload, execute, or describe capabilities unless the user explicitly requests them through the running application.
