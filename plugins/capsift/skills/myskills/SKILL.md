---
name: myskills
description: Open and control the localized My Skills menu. Trigger for /myskills, explicit requests to find or list the user's Skills, questions about loaded Skills, Skill providers or routing, and explicit requests to enable, pause, update, quarantine, or check a named Skill. Do not trigger for generic uses of the word skill or native Codex commands.
license: MIT
---

# `/myskills`

Use the language-selection rules from the sibling `helpme` skill, then render the matching
static catalog in `references/locales/`. Never translate stable menu text at runtime.
Resolve commands through the plugin-root `menu-map.json`; an absent command is explicitly
Unavailable via its `*` mapping. Never infer that the optional full CLI is installed merely
because the bundled MCP search/load runtime is available.

## Interaction

- Bare `/myskills`: render both visible groups and all items. Provider, Routing,
  Inventory, Lifecycle, Risks, and Conflicts are never hidden. Append the catalog's
  `navigation` items so language settings and the CapSift main menu stay visible.
- `list`/`inventory`: call `capability.search` with an empty query and a bounded result.
  Set `include_inventory: true`, `include_cards: true`, and `kinds: ["skill"]`. Use
  `inventory.active_by_kind.skill` for the global active Skill total and query-level
  `total_matches` for the current filter instead of counting cards. Return at most five
  Skill cards unless the user asks for more. Show the generation/status compactly and
  report only non-zero safe diagnostic counts.
- Accept the visible number only while the My Skills menu is the active interaction.
- Accept exact commands and aliases: `find`, `list`/`ls`, `loaded`/`using`, `show`/`info`,
  `providers`, `routing`/`why`, `lifecycle`, `risks`, `conflicts`/`check`.
- `/myskills back` returns to the My Skills menu. `/helpme home` always returns to the
  CapSift main menu, and `/helpme language` opens language settings. Recognize
  localized `返回上一级`, `返回主菜单`, `back`, and `home` only while this menu is active.
- Append the catalog's `navigation` items exactly once after every My Skills result.
- Recognize explicit natural-language intents such as “帮我找一个处理 PDF 的 Skill”,
  “我加载了哪些 Skill”, or “为什么选择 documents”. Use low reasoning for status,
  listing, and direct commands; use medium reasoning only when task-to-Skill matching is
  ambiguous.
- Never intercept native `/help`, `/skills`, `/status`, `/model`, or `/mcp` commands.
- Do not trigger on source code, documentation, or casual text that merely contains the
  word “skill”.

## Safety and context

- Search only compact Skill metadata first. Return at most five candidates unless the
  user requests more.
- Load a Skill body only after an explicit selection.
- Skills are load-only in CapSift; do not offer a `run` command.
- Read-only actions execute directly. For a discovered exact Skill coordinate, use
  `capsift lifecycle set <coordinate> <enabled|disabled|quarantined> --pretty`.
  Confirm quarantine before applying it. These states only control catalog activation;
  they do not delete or execute Skill files. `update` remains unavailable, and `pause`
  is presented as the reversible `disabled` state rather than a separate hidden state.
- For `loaded`, use `capsift loaded --limit 20 --pretty`, filter returned entries
  to kind `skill`, and describe them as recent successful loads from redacted audit.
- For `providers`, use `capsift providers --pretty`, then show Provider rows whose
  `kinds` contain `skill`. Keep the real Provider name visible.
- For `routing <task>`, use `capsift routing <task> --kind skill --limit 5 --pretty`
  and show only rank, revision, and `match_reason`. This is deterministic lexical
  explanation, not hidden chain-of-thought.
- If one of these CLI calls is unavailable because the local runtime is not connected,
  say it is unavailable. Never infer Loaded or Provider state from a search card.
- Never delete a Skill. Use pause or quarantine instead.
- Label unavailable management actions honestly when the runtime is not connected.
- Never reveal credentials, hidden reasoning, or full unselected Skill content.
