# CapSift User Guide

> This guide covers CapSift installation, menus, the full CLI, the Dashboard, and everyday workflows. You do not need to understand API, MCP, or RAG before starting.

> Upgrading from CapabilityHub? Read the [lossless migration guide](migration-capabilityhub-to-capsift.md). Legacy commands and project state remain compatible.

## 1. What CapSift does

Codex can gain extra capabilities through Skills, MCP, CLI, API, and RAG. As that catalog grows, loading every instruction into every conversation wastes tokens and makes tool selection less reliable.

CapSift:

1. keeps a compact capability inventory;
2. searches for capabilities that match the current task;
3. loads only the capability that is actually needed;
4. applies shared execution, permission, budget, approval, and audit controls.

Think of it as a capability menu and tool manager around Codex.

### CapSift versus native Codex conversation control

CapSift is a capability-management layer around Codex. It is not another conversation system and does not take over the current task.

| Area | Native Codex conversation control | CapSift |
|---|---|---|
| Conversations and tasks | Manages message history, system instructions, the current task, and responses | Does not rewrite or delete conversation messages |
| Model and context window | Selects the model and maintains the actual conversation context | Reduces how much Skill, MCP, CLI, API, and RAG documentation is disclosed |
| Native commands | Provides `/help`, `/skills`, `/status`, `/mcp`, and other built-in features | Adds `/helpme` and `/myskills` without replacing native commands |
| Capability management | Provides the native Skill, MCP, and tool mechanisms | Unifies inventory, search, selection, and progressive loading across five capability kinds |
| Execution safety | Provides native approvals, permissions, and sandboxing | Adds Provider-level permissions, budgets, approvals, and auditing |
| Context removal | Is controlled by the Codex client and model context mechanism | Can remove or compact CapSift-managed disclosures, but cannot delete chat history |

You still talk directly to Codex. When a task needs an external capability, CapSift searches compact metadata, loads the selected instructions, and hands control back to Codex.

Codex and CapSift safety rules apply together. If either layer denies an operation, it stops. CapSift cannot bypass Codex confirmation, permissions, or sandboxing, and it does not automatically change the Codex model or reasoning effort.

## 2. Choose how you want to use it

| Mode | Best for | What it provides |
|---|---|---|
| Codex plugin (recommended) | Quickly finding Skills and using the compact menu | `/helpme`, `/myskills`, lightweight search, and Skill descriptions |
| Full Python core | Dashboard, complete five-kind Inventory, Providers, budgets, audit, or controlled execution | The full `capsift` CLI and local management Dashboard |

Install the plugin first if you only need the menus. Install the Python core when a menu item says that it requires the full CLI.

## 3. Start in three minutes

### 3.1 If the plugin is already installed

1. Create a new task in Codex.
2. Enter `/helpme`.
3. To use English explicitly, enter `/helpme language set en`.
4. Enter `/myskills` to open the Skill menu.
5. Describe what you need, for example:

```text
/myskills find read and organize a PDF
```

### 3.2 Install the plugin

Run:

```powershell
codex plugin marketplace add 1Milkdeliver/capsift --ref main
codex plugin add capsift@capsift
```

Close the current Codex task, create a new one, and enter:

```text
/helpme
```

Check the installation:

```powershell
codex plugin list
```

The list should contain `capsift@capsift` with the state `installed, enabled`.

The plugin bundles a read-only MCP runtime. It does not require a globally installed `capsift` command or a separate `codex mcp add` step.

## 4. Everyday operations

### 4.1 Open the main menu

```text
/helpme
```

The main menu displays compact descriptions. Opening it does not load every Skill body.

### 4.2 Open the Skill menu

```text
/myskills
```

You can reply with a visible menu number, enter a complete command, or describe the task in natural language.

### 4.3 Find a Skill for a task

```text
/myskills find analyze an Excel sales workbook
```

You can also say:

```text
Find a Skill that can analyze an Excel sales workbook.
```

CapSift returns compact candidates first. It reads detailed Skill instructions only after a candidate is selected.

### 4.4 View the current Inventory

```text
/helpme inventory
```

Inventory reports Skill, MCP, CLI, API, and RAG counts. The bundled plugin runtime counts only capabilities packaged with the plugin. Install the full Python core to inspect the project and host catalog.

### 4.5 Search all capability kinds

```text
/helpme search read a webpage and summarize the key points
```

Search returns compact cards and does not execute a capability.

### 4.6 Go back, return home, or change language

```text
/helpme back
/helpme home
/myskills back
/helpme language
/helpme language set en
```

- `back` returns to the previous CapSift menu.
- `home` returns to the CapSift root menu.
- `language` views or changes the menu language.

These commands do not replace native Codex `/help`, `/skills`, `/status`, or `/mcp` commands.

## 5. Professional terms in the menu

| Term | Meaning |
|---|---|
| Skill | Instructions that tell Codex how to handle a category of tasks |
| MCP | A standard protocol for connecting Codex to tools or services |
| CLI | A local command-line program |
| API | A service reached through a defined local or network interface |
| RAG | Retrieval of relevant passages from approved material for model use |
| Inventory | The capabilities currently discovered, their counts, and status |
| Provider | The real source or adapter behind a capability |
| Routing | Why CapSift selected or excluded a capability |
| Loaded | Capability instructions that were loaded successfully and recorded |
| Lifecycle | Enable, disable, or quarantine a capability without deleting its source |
| Budget | Hard limits for tokens, bytes, loads, and executions |
| Approval | Explicit authorization required before a high-risk or irreversible action |
| Audit | Minimal security records for search, load, execute, and management operations |

## 6. Install the full Python core

Install the core only when you need the Dashboard, complete Inventory, Provider configuration, budgets, audit, or controlled execution.

### 6.1 Windows

Python 3.11 or newer is required. In PowerShell, enter the directory where you want the project and run:

```powershell
git clone https://github.com/1Milkdeliver/capsift.git
cd capsift
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[mcp]"
```

If PowerShell blocks virtual-environment activation, use the executables directly:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[mcp]"
.\.venv\Scripts\capsift.exe health --pretty
```

### 6.2 macOS or Linux

```bash
git clone https://github.com/1Milkdeliver/capsift.git
cd capsift
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[mcp]'
capsift health --pretty
```

### 6.3 Verify the installation

```powershell
capsift health --pretty
capsift inventory --pretty
capsift providers --pretty
```

- `health` checks the project path, Dashboard assets, configuration parsing, and version.
- `inventory` scans and counts the five capability kinds.
- `providers` groups capabilities by their real source.

`health` does not load the complete capability catalog, so it is the quickest installation check.

## 7. Open the Dashboard

Run this from the project directory:

```powershell
capsift dashboard --project-root .
```

The terminal prints a local URL. Copy it into a browser.

The Dashboard can:

- navigate between Conversations, Capability library, Manage, System details, and a local guide;
- refresh and filter current or archived local Codex tasks from the first page;
- show five-kind counts and Inventory freshness;
- filter immediately by kind, automatic category, activation state, or Provider without pressing Search;
- use fuzzy search only for a specific name, then sort by estimated tokens or alphabetically;
- show category-colored cards with an introduction, Provider, state, and prominent estimated Token impact;
- open a card's details and use a switch to allow or block future full loading through CapSift;
- switch between English, Simplified Chinese, and system language from the upper-right corner;
- show Provider, Routing, Loaded, and connection-configuration state;
- enable, disable, or quarantine capabilities, restoring the switch if the save fails;
- decide existing approval requests;
- show redacted audit, context, reasoning, and update state;
- read the built-in local guide without a network connection.

The Dashboard binds to `127.0.0.1` by default. Refreshing conversations merges Codex's lightweight `session_index.jsonl` with bounded file discovery under the `sessions` and `archived_sessions` directories, so older and archived tasks omitted from the index can still appear. Only after you select a task does CapSift stream through up to 128 MiB of its tool-call envelopes. It never reads or displays message, response, or reasoning bodies. The page reports bytes scanned, tool envelopes, and whether coverage was complete.

The Dashboard does not probe external services automatically and does not expose credentials, complete commands, Skill bodies, or Provider output. Enabling a capability costs `0 Token`; the card estimate applies to a later full instruction load. Disabling a capability prevents future disclosure through CapSift but cannot remove content already present in Codex conversation history. CapSift now discovers more old tasks, but silent native Codex injection without a tool-call envelope still cannot be proven reliably. The conversation view reports verifiable evidence and coverage instead of guessing missing activity.

## 8. Ten useful CLI commands

| Command | Purpose |
|---|---|
| `capsift health --pretty` | Check installation and local wiring |
| `capsift inventory --pretty` | View the five-kind Inventory |
| `capsift search "task" --pretty` | Search capabilities for a task |
| `capsift loaded --pretty` | View recent successful loads |
| `capsift providers --pretty` | View capability sources |
| `capsift routing "task" --pretty` | Explain deterministic selection |
| `capsift connections --pretty` | View configured state without probing the network |
| `capsift budget-report --pretty` | View budget limits and usage |
| `capsift audit --pretty` | View redacted audit events |
| `capsift dashboard --project-root .` | Open the local Dashboard |

Inspect command options with:

```powershell
capsift --help
capsift search --help
```

## 9. A complete safe workflow

Suppose you want Codex to use a capability:

1. Search compact candidates without executing anything.

   ```powershell
   capsift search "organize project documentation" --pretty
   ```

2. Inspect Routing to understand the selection.

   ```powershell
   capsift routing "organize project documentation" --pretty
   ```

3. Load only the selected instructions using the exact revision from the search result.

   ```powershell
   capsift load REVISION --pretty
   ```

4. Execute only when a CLI, API, RAG, or MCP Provider is explicitly configured. A normal Skill is read-only content; CapSift does not run scripts beside `SKILL.md`.

5. Inspect the redacted audit trail.

   ```powershell
   capsift audit --pretty
   ```

Replace `REVISION` with the complete revision returned by search. Do not execute the placeholder literally.

## 10. Status values

### `complete`

The current Inventory refresh completed successfully.

### `partial`

Some capabilities were excluded because of duplicates, conflicts, invalid formats, or path rules. Inspect non-zero `excluded_by_reason` counts rather than treating excluded records as available.

### `stale`

The latest refresh failed and CapSift is serving the previous complete snapshot. Run:

```powershell
capsift health --pretty
capsift inventory --pretty
```

### `configured_not_probed`

The connection is configured but has not been tested. It does not mean that the service is reachable or authenticated.

### `unavailable`

The current plugin or runtime is not connected to the feature. A common cause is using the lightweight plugin without the full Python core. This does not indicate a Codex conversation failure.

## 11. Troubleshooting

### `/helpme` does not respond

1. Confirm that `codex plugin list` shows `capsift@capsift` as `installed, enabled`.
2. Create a new Codex task after installing or updating the plugin.
3. Confirm that you entered `/helpme`, not the native `/help` command.

### `/helpme` works, but Dashboard or Provider features are unavailable

This is expected in lightweight mode. Install the full Python core and confirm that `capsift health --pretty` succeeds.

### The `capsift` command is not found

The virtual environment may not be active. On Windows, run:

```powershell
.\.venv\Scripts\capsift.exe health --pretty
```

### MCP is configured, but why is it unavailable?

Configured does not mean connected or authenticated. Start with:

```powershell
capsift connections --pretty
```

Run a bounded probe only when you explicitly need one:

```powershell
capsift connections --probe --pretty
```

A successful probe proves only DNS, TCP, or TLS reachability. It does not prove that MCP tool calls are healthy.

### Does CapSift automatically run scripts from Skills?

No. Discovered Skills are loadable instructions only. An operation runs only through a Provider that the project explicitly configured and that passed permission, budget, policy, and approval checks.

### Can credentials appear in the conversation or Dashboard?

CapSift is designed not to display credential values. You should still avoid pasting passwords, tokens, or API keys directly into conversation or CLI arguments.

## 12. Update or remove the plugin

Update the marketplace and plugin:

```powershell
codex plugin marketplace upgrade capsift
codex plugin add capsift@capsift
```

Create a new Codex task after updating.

Remove the plugin:

```powershell
codex plugin remove capsift@capsift
codex plugin marketplace remove capsift
```

Removing the plugin does not delete Skills, project files, or local CapSift state. Removing those files is a separate operation; confirm backups and exact target paths first.

## 13. Continue reading

- [Plugin and Dashboard technical boundaries](ui-plugin.md)
- [Provider project configuration](provider-configuration.md)
- [Release scope and security boundaries](release-readiness.md)
- [GitHub repository](https://github.com/1Milkdeliver/capsift)
- [Published releases](https://github.com/1Milkdeliver/capsift/releases)

If you remember only three menu commands, remember:

```text
/helpme
/myskills
/helpme home
```
